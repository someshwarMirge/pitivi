#!/usr/bin/python
# PiTiVi , Non-linear video editor
#
#       discoverer.py
#
# Copyright (c) 2005, Edward Hervey <bilboed@bilboed.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place - Suite 330,
# Boston, MA 02111-1307, USA.

"""
Discover file multimedia information.
"""

import gobject
import gst
import gtk
import objectfactory
import gc

import os.path

class Discoverer(gobject.GObject):
    """
    Queues requests to discover information about given files.
    The discovery is done in a very fragmented way, so that it appears to be
    running in a separate thread.
    The "new_sourcefilefactory" signal is triggered when a file is established
    to be a media_file and the FileSourceFactory() is included in the signal.
    The "not_media_file" signal is triggered if a file is not a media_file.
    """

    __gsignals__ = {
        "new_sourcefilefactory" : (gobject.SIGNAL_RUN_LAST,
                                   gobject.TYPE_NONE,
                                   (gobject.TYPE_PYOBJECT, )),
        "not_media_file" : (gobject.SIGNAL_RUN_LAST,
                            gobject.TYPE_NONE,
                            (gobject.TYPE_STRING, gobject.TYPE_STRING)),
        "finished_analyzing" : ( gobject.SIGNAL_RUN_LAST,
                                 gobject.TYPE_NONE,
                                 (gobject.TYPE_PYOBJECT, )),
        "ready" : ( gobject.SIGNAL_RUN_LAST,
                    gobject.TYPE_NONE,
                    ( ))
        }

    def __init__(self, project):
        gst.log("new discoverer for project %s" % project)
        gobject.GObject.__init__(self)
        self.project = project
        self.queue = []
        self.working = False
        self.analyzing = False
        self.currentfactory = None
        self.current = None
        self.pipeline = None
        self.thumbnailing = False
        self.thisdone = False

    def addFile(self, filename):
        """ queue a filename to be discovered """
        gst.info("filename: %s" % filename)
        self.queue.append(filename)
        if not self.working:
            self._startAnalysis()

    def addFiles(self, filenames):
        """ queue a list of filenames to be discovered """
        gst.info("filenames : %s" % filenames)
        self.queue.extend(filenames)
        if not self.working:
            self._startAnalysis()

    def _startAnalysis(self):
        """
        Call this method to start analyzing the uris
        """
        if self.working:
            gst.warning("called when still working!")
            return False
        
        if not self.queue:
            gst.warning("Nothing to analyze!!")
            return False
        
        self.working = True
        gobject.idle_add(self._analyze)
        return False

    def _finishAnalysis(self):
        """
        Call this method when the current file is analyzed
        This method will wrap-up the analyzis and call the next analysis if needed
        """
        if not self.analyzing:
            gst.warning("called when not analyzing!!")
            return False

        self.thisdone = True

        gst.info("Cleaning up after finished analyzing %s" % self.current)
        # finish current, cleanup
        self.bus.remove_signal_watch()
        self.bus = None
        gst.info("before setting to NULL")
        res = self.pipeline.set_state(gst.STATE_NULL)
        gst.info("after setting to NULL : %s" % res)
        if self.currentfactory:
            self.emit('finished-analyzing', self.currentfactory)
        self.analyzing = False
        self.current = None
        self.currentfactory = None
        self.pipeline = None
        
        # restart an analysis if there's more...
        if self.queue:
            gobject.idle_add(self._analyze)
        else:
            self.working = False
            gst.info("discoverer is now ready again")
            self.emit("ready")
        return False


    def _analyze(self):
        """
        Sets up a pipeline to analyze the given uri
        """
        self.analyzing = True
        self.thisdone = False
        self.current = self.queue.pop(0)
        gst.info("Analyzing %s" % self.current)
        self.currentfactory = None
        
        # setup graph and start analyzing
        self.pipeline = gst.parse_launch("gnomevfssrc name=src location=\"%s\" ! decodebin name=dbin" % self.current)
        if not self.pipeline:
            gst.warning("This is not a media file : %s" % self.current)
            self.emit("not_media_file", self.current, "Couldn't construct pipeline.")
            gobject.idle_add(self._finishAnalysis)
            return
        gst.info("analysis pipeline created")
        dbin = self.pipeline.get_by_name("dbin")
        dbin.connect("new-decoded-pad", self._newDecodedPadCb)
        dbin.connect("unknown-type", self._unknownTypeCb)
        self.bus = self.pipeline.get_bus()
        self.bus.connect("message", self._busMessageCb)
        self.bus.add_signal_watch()
        gst.info("setting pipeline to PAUSED")
        if self.pipeline.set_state(gst.STATE_PAUSED) == gst.STATE_CHANGE_FAILURE:
            self.emit("not_media_file", self.current, "Pipeline didn't want to go to PAUSED")
            gst.info("pipeline didn't want to go to PAUSED")
            gobject.idle_add(self._finishAnalysis)

        # return False so we don't get called again
        return False
        
    def _busMessageCb(self, bus, message):
        if self.thisdone:
            return
        if message.type == gst.MESSAGE_STATE_CHANGED:
            gst.log("%s:%s" % ( message.src, message.parse_state_changed()))
            if message.src == self.pipeline:
                prev, new, pending = message.parse_state_changed()
                if prev == gst.STATE_READY and new == gst.STATE_PAUSED:
                    # Let's get the information from all the pads
                    self._getPadsInfo()
                    gst.log("pipeline has gone to PAUSED, now pushing to PLAYING")
                    self.pipeline.set_state(gst.STATE_PLAYING)
        elif message.type == gst.MESSAGE_EOS:
            gst.log("got EOS")
            self.thisdone = True
            filename = "/tmp/" + self.currentfactory.name.encode('base64').replace('\n','') + ".png"
            if os.path.isfile(filename):
                self.currentfactory.setThumbnail(filename)
            gobject.idle_add(self._finishAnalysis)
        elif message.type in [gst.MESSAGE_ERROR, gst.MESSAGE_WARNING]:
            gst.warning("got an ERROR/WARNING")
            self.thisdone = True
            if not self.currentfactory:
                self.emit("not_media_file", self.current, "Couldn't figure out file type")
            gobject.idle_add(self._finishAnalysis)
        elif message.type == gst.MESSAGE_ELEMENT:
            gst.debug("Element message %s" % message.structure.to_string())
            if message.structure.get_name() == "redirect":
                gst.warning("We don't implement redirections currently, ignoring file")
                gobject.idle_add(self._finishAnalysis)
        else:
            gst.log("%s:%s" % ( message.type, message.src))

    def _getPadsInfo(self):
        # iterate all src pads and check their informatiosn
        gst.info("Getting pads info on decodebin")
        for pad in list(self.pipeline.get_by_name("dbin").pads()):
            if pad.get_direction() == gst.PAD_SINK:
                continue
            caps = pad.get_caps()
            if not caps.is_fixed():
                caps = pad.get_negotiated_caps()
            gst.info("testing pad %s : %s" % (pad, caps))
            
            if caps and caps.is_fixed():
                if caps.to_string().startswith("audio/x-raw") and not self.currentfactory.audio_info:
                    self.currentfactory.setAudioInfo(caps)
                elif caps.to_string().startswith("video/x-raw") and not self.currentfactory.video_info:
                    self.currentfactory.setVideoInfo(caps)
            if not self.currentfactory.length:
                try:
                    length, format = pad.query_duration(gst.FORMAT_TIME)
                except:
                    pad.warning("duration query failed")
                else:
                    if format == gst.FORMAT_TIME:
                        self.currentfactory.set_property("length", length)

    def _vcapsNotifyCb(self, pad, property):
        if pad.get_caps().is_fixed():
            self.currentfactory.setVideoInfo(pad.get_caps())

    def _newVideoPadCb(self, element, pad):
        """ a new video pad was found """
        self.currentfactory.setVideo(True)
        if pad.get_caps().is_fixed():
            self.currentfactory.setVideoInfo(pad.get_caps())

        # replacing queue-fakesink by ffmpegcolorspace-queue-pngenc
        csp = gst.element_factory_make("ffmpegcolorspace")
        queue = gst.element_factory_make("queue")
        pngenc = gst.element_factory_make("pngenc")
        pngsink = gst.element_factory_make("filesink")
        pngsink.set_property("location", "/tmp/" + self.currentfactory.name.encode('base64').replace('\n','') + ".png")
        self.pipeline.add(csp, queue, pngenc, pngsink)
        pngenc.link(pngsink)
        queue.link(pngenc)
        csp.link(queue)
        pad.link(csp.get_pad("sink"))
        if not self.currentfactory.video_info:
            pad.connect("notify::caps", self._vcapsNotifyCb)
        for element in [csp, queue, pngenc, pngsink]:
            element.set_state(gst.STATE_PAUSED)
        
    def _newAudioPadCb(self, element, pad):
        """ a new audio pad was found """
        self.currentfactory.setAudio(True)

        if pad.get_caps().is_fixed():
            self.currentfactory.setAudioInfo(pad.get_caps())
            
    def _unknownTypeCb(self, dbin, pad, caps):
        gst.info(caps.to_string())
        if not self.currentfactory or not self.currentfactory.is_audio or not self.currentfactory.is_video:
            gst.warning("got unknown pad without anything else")
            self.emit("not_media_file", self.current, "Got unknown stream type : %s" % caps.to_string())
            gobject.idle_add(self._finishAnalysis)

    def _newDecodedPadCb(self, element, pad, is_last):
        # check out the type (audio/video)
        # if we don't already have self.currentfactory
        #   create one, emit "new_sourcefile_factory"
        capsstr = pad.get_caps().to_string()
        gst.info("pad:%s caps:%s" % (pad, capsstr))
        if capsstr.startswith("video/x-raw"):
            if not self.currentfactory:
                self.currentfactory = objectfactory.FileSourceFactory(self.current, self.project)
                self.emit("new_sourcefilefactory", self.currentfactory)
            self._newVideoPadCb(element, pad)
        elif capsstr.startswith("audio/x-raw"):
            if not self.currentfactory:
                self.currentfactory = objectfactory.FileSourceFactory(self.current, self.project)
                self.emit("new_sourcefilefactory", self.currentfactory)
            self._newAudioPadCb(element, pad)
        else:
            if is_last:
                if not self.currentfactory or not self.currentfactory.is_audio or not self.currentfactory.is_video:
                    gst.warning("couldn't find a usable pad")
                    self.emit("not_media_file", self.current, "Got unknown stream type : %s" % capsstr)
                    gobject.idle_add(self._finishAnalysis)
