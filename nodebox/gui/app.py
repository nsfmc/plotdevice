# encoding: utf-8
import sys
import os
import objc
from glob import glob
import nodebox
from Foundation import *
from AppKit import *
from nodebox.util.fsevents import Observer, Stream
from nodebox.gui.preferences import get_default
from nodebox.run import CommandListener
from nodebox import util, bundle_path

LIB_DIR_README = """"You can put NodeBox libraries In this directory to make them available to your scripts.
If your script has used the `colors.theme()` function, the `colors` folder in this directory
contains a set of json files with color schemes derived from various keywords.
"""

def set_timeout(target, sel, delay, info=None, repeat=False):
    return NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(delay, target, sel, info, repeat)

class NodeBoxAppDelegate(NSObject):
    examplesMenu = None

    def awakeFromNib(self):
        self._prefsController = None
        self._docsController = NodeBoxDocumentController.sharedDocumentController()
        self._listener = CommandListener(port=get_default('remote-port'))
        libDir = os.path.join(os.getenv("HOME"), "Library", "Application Support", "NodeBox")
        try:
            if not os.path.exists(libDir):
                os.mkdir(libDir)
                f = open(os.path.join(libDir, "README.txt"), "w")
                f.write(LIB_DIR_README)
                f.close()
            self._listener.start()
        except OSError: pass
        except IOError: pass
        self.examplesMenu = NSApp().mainMenu().itemWithTitle_('Examples')

    def applicationDidFinishLaunching_(self, note):
        mm=NSApp().mainMenu()

        # disable the start-dictation item in the edit menu
        edmenu = mm.itemAtIndex_(2).submenu()
        for it in edmenu.itemArray():
            action = it.action()
            if action in (NSSelectorFromString("startDictation:"), ):
                edmenu.removeItem_(it)

        # add a hidden item to the menus that can be triggered internally by the editor
        for menu in mm.itemArray()[2:5]:
            flicker = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_('Flash This Menu', None, '')
            flicker.setEnabled_(True)
            flicker.setHidden_(True)
            menu.submenu().insertItem_atIndex_(flicker,0)

    def listenOnPort_(self, port):
        if self._listener and self._listener.port == port:
            return
        newlistener = CommandListener(port=port)
        if self._listener:
            self._listener.join()
        self._listener = newlistener
        newlistener.start()
        return newlistener.active

    def updateExamples(self):
        examples_folder = bundle_path("Contents/Resources/examples")
        pyfiles = glob('%s/*/*.nb'%examples_folder)
        categories = self.examplesMenu.submenu()
        folders = {}
        for item in categories.itemArray():
            item.submenu().removeAllItems()
            folders[item.title()] = item.submenu()
        for fn in sorted(pyfiles):
            cat = os.path.basename(os.path.dirname(fn))
            example = os.path.basename(fn)
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(example[:-3], "openExample:", "")
            item.setRepresentedObject_(fn)
            folders[cat].addItem_(item)
        self.examplesMenu.setHidden_(not pyfiles)

    def openExample_(self, sender):
        pth = sender.representedObject()
        # doc, err = self._docsController.makeUntitledDocumentOfType_error_("public.python-script", None)
        doc, err = self._docsController.makeUntitledDocumentOfType_error_("net.nodebox.NodeBox.document", None)
        doc.stationery = pth
        self._docsController.addDocument_(doc)
        doc.makeWindowControllers()
        doc.showWindows()

    def applicationWillBecomeActive_(self, note):
        # rescan the examples dir every time?
        self.updateExamples()

    @objc.IBAction
    def showPreferencesPanel_(self, sender):
        if self._prefsController is None:
            from nodebox.gui.preferences import NodeBoxPreferencesController
            self._prefsController = NodeBoxPreferencesController.alloc().init()
        self._prefsController.showWindow_(sender)

    @objc.IBAction
    def newSketch_(self, sender):
        from nodebox.util.ottobot import genTemplate
        kind = ['sketch','anim','ottobot'][sender.tag()]        
        doc = self._docsController.newDocument_(sender)
        doc = self._docsController.currentDocument()
        doc.setSource_(genTemplate(kind))
        if kind=='ottobot':
            doc.runScript()

    @objc.IBAction
    def showHelp_(self, sender):
        url = NSURL.fileURLWithPath_(bundle_path('Contents/Resources/doc/manual.html'))
        opened = NSWorkspace.sharedWorkspace().openURL_(url)

    @objc.IBAction
    def showSite_(self, sender):
        url = NSURL.URLWithString_("http://nodebox.net/")
        NSWorkspace.sharedWorkspace().openURL_(url)

    def applicationWillTerminate_(self, note):
        self._listener.join()
        import atexit
        atexit._run_exitfuncs()


class NodeBoxDocumentController(NSDocumentController):
    _observer = None # fsevents thread
    _stream = None   # current fsevents session
    _resume = None   # timer (to update the observer's path list)
    _update = None   # timer (to stat the files in checklist)
    watching = {}    # keys are dirs, vals are lists of file paths within the dir
    checklist = set()# files within dirs that had change notifications

    def init(self):
        nc = NSNotificationCenter.defaultCenter()
        nc.addObserver_selector_name_object_(self, 'updateWatchList:', 'watch', None)
        self._observer = Observer()
        self._observer.start()
        return super(NodeBoxDocumentController, self).init()

    def addDocument_(self, doc):
        # print "add", doc
        super(NodeBoxDocumentController, self).addDocument_(doc)
        self.updateWatchList_(None)

    def removeDocument_(self, doc):
        super(NodeBoxDocumentController, self).removeDocument_(doc)
        self.updateWatchList_(None)

    def updateWatchList_(self, note):
        return False # bail


        if self._resume:
            self._resume.invalidate()
        self._resume = set_timeout(self, "keepWatching:", 0.2)

    def keepWatching_(self, timer):
        self._resume = None
        if self._stream:
            self._observer.unschedule(self._stream)
            self._stream = None

        urls = [doc.fileURL() for doc in self.documents()]
        paths = [u.fileSystemRepresentation() for u in urls if u]
        self.watching = {}
        for p in paths:
            dirname = os.path.dirname(p)
            files = self.watching.get(dirname, [])
            self.watching[dirname] = files + [p]
        # print "watching", self.watching.values()
        if self.watching:
            self._stream = Stream(self.fileEvent, *set(self.watching.keys()))
            self._observer.schedule(self._stream)

    def fileEvent(self, path, event):
        path = path.rstrip('/')
        if path in self.watching:
            changed = self.watching[path]
            if not all(p in self.checklist for p in changed):
                self.checklist.update(changed)
                if self._update:
                    self._update.invalidate()
                self._update = set_timeout(self, "checkFiles:", 0.25)

    def checkFiles_(self, timer):
        self.performSelectorOnMainThread_withObject_waitUntilDone_("updateCheckList", None, True)
        self._update = None

    def updateCheckList(self):
        for doc in self.documents():
            url = doc.fileURL()
            if not url: continue
            
            pth = url.fileSystemRepresentation()
            if pth in self.checklist:

                # note that the file might have disappeared and pth is dangling...
                mtime = os.path.getmtime(pth)
                if mtime != doc.mtime:
                    doc.refresh()
        self.checklist = set()

