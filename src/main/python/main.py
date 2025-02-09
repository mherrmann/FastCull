from PySide6 import QtCore
from PySide6.QtCore import Qt, Slot
from PySide6 import QtGui
from PySide6 import QtWidgets

from fbs_runtime.application_context.PySide6 import ApplicationContext
from typing import List, Optional, cast

import file_ops
import os
import sys
import time
import timer

PHOTO_EXTENSIONS = ["jpg", "JPG", "jpeg", "JPEG"]
PRELOAD_COUNT = 10

class Wrapper(QtCore.QRunnable):

    def __init__(self, function, *args, **kwargs):
        self.function = function
        self.args = args
        self.kwargs = kwargs
        super().__init__()

    @Slot()
    def run(self):
        self.function(*self.args, **self.kwargs)

class Overlay(QtWidgets.QWidget):

    _height = 64

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.setMaximumHeight(Overlay._height)
        self.layout = QtWidgets.QBoxLayout(
            QtWidgets.QBoxLayout.Direction.TopToBottom, parent=self)
        self.filename = QtWidgets.QLabel(self)
        self.filename.setStyleSheet("color: #FEFEFE; font: 16px; font-family: Impact")
        self.layout.addWidget(self.filename)
        self.flags = QtWidgets.QLabel(self)
        self.flags.setStyleSheet("color: #FEFEFE; font: bold 32px; font-family: Impact")
        self.layout.addWidget(self.flags)
        self.outlines = []
        self.addOutline(self.flags, 40)
        self.addOutline(self.filename, 20)
    
    def addOutline(self, label: QtWidgets.QLabel, radius: float):
        outline = QtWidgets.QGraphicsDropShadowEffect(self)
        outline.setOffset(0, 0)
        outline.setBlurRadius(radius)
        outline.setColor(Qt.black)
        label.setGraphicsEffect(outline)
        self.outlines.append(outline)
    
    def updateContent(self, filename:Optional[str] = None, protected: bool = False):
        if filename is not None:
            self.filename.setText(filename)
        text = ""
        if(protected):
            text += "  P"
        self.flags.setText(text)

class Viewer(QtWidgets.QWidget):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.layout = QtWidgets.QStackedLayout()
        self.layout.setStackingMode(
            QtWidgets.QStackedLayout.StackingMode.StackAll)
        self.overlay = Overlay(parent=self)
        self.layout.addWidget(self.overlay)
        self.overlay.updateContent()
        self.label = QtWidgets.QLabel(self)
        self.label.setAlignment(Qt.AlignCenter)
        self.layout.addWidget(self.label)
        self.path: str = None
        self.filenames: List[str] = []
        self.load_mutexes: List[QtCore.QMutex] = []
        self.images: List[Optional[QtGui.QImage]] = []
        self.scaled: List[Optional[QtGui.QImage]] = []
        self.current_index: Optional[int] = None
        self.thread_pool = QtCore.QThreadPool()
        self.loads = set()
        self.timer = timer.Timer()
        self.inner_timer = timer.Timer(quiet=True)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        print("Resizing %d %d" % (self.width(), self.height()))
        self.label.resize(self.width(), self.height())
        self.overlay.resize(self.width(), self.height())
        self.scaled = [None] * len(self.scaled)
        if self.current_index is not None:
            self.switch(self.current_index)
        return super().resizeEvent(event)

    def switch(self, new_index: int):
        print("Switching to", self.filenames[new_index])
        self.timer.start()
        self.current_index = new_index
        self.preload()
        self.timer.segment("launching preload")
        self.load(new_index, True)
        self.timer.segment("loading")
        self.label.setPixmap(QtGui.QPixmap.fromImage(
            cast(QtGui.QImage, self.scaled[new_index])))
        self.overlay.updateContent(
            filename=self.filenames[new_index], 
            protected=file_ops.is_protected(self.path, self.filenames[new_index]))
        self.timer.segment("displaying")
        self.timer.stop()

    def preload(self):
        # The current_index one is loaded in main thread if needed.
        start = self.current_index + 1
        stop = min(self.current_index + PRELOAD_COUNT, len(self.filenames))
        for index in range(start, stop):
            if self.images[index] is None or self.scaled[index] is None:
                self.thread_pool.start(Wrapper(self.load, index, False))

    def load(self, index: int, blocking: bool):
        self.inner_timer.start()
        got_lock = self.load_mutexes[index].tryLock(0 if blocking else 1)
        if got_lock:
            self.inner_timer.segment("lock acquired")
            if self.images[index] is None:
                if blocking:
                    print("Preload cache miss!")
                try:
                    full_path = os.path.join(self.path, self.filenames[index])
                    image = QtGui.QImage()
                    image.load(full_path)
                    assert(image.width())
                    self.images[index] = image
                    self.inner_timer.segment("successful load")
                except:
                    self.inner_timer.segment("failed load")
                    raise
            else:
                self.inner_timer.segment("skipped load")
            if self.scaled[index] is None:
                source = cast(QtGui.QImage, self.images[index])
                width = min(self.width(), source.width())
                height = min(self.height(), source.height())
                self.scaled[index] = source.scaled(
                    width, height,
                    aspectMode=Qt.AspectRatioMode.KeepAspectRatio,
                    mode=Qt.TransformationMode.SmoothTransformation)
        else:
            self.inner_timer.segment("lock not acquired")
        self.load_mutexes[index].unlock()
        self.inner_timer.stop()

    def openDir(self, path: str, start_file: str = None):
        print("Opening ", path)
        self.path = path
        self.filenames = []
        self.images = []
        self.load_mutexes = []
        start = time.time()
        for filename in sorted(os.listdir(path)):
            if filename.split('.')[-1] in PHOTO_EXTENSIONS:
                self.filenames.append(filename)
                self.images.append(None)
                self.scaled.append(None)
                self.load_mutexes.append(QtCore.QMutex())
        print("%.2fs %s" % (time.time() - start, "listing directory"))
        if start_file:
            self.switch(self.filenames.index(start_file))
        elif self.filenames:
            self.switch(0)
        else:
            self.current_index = None

    def flipProtected(self):
        fname = self.filenames[self.current_index]
        old = file_ops.is_protected(self.path, fname)
        if old:
            print("Unprotecting:", ",".join(
                file_ops.related_files(self.path, fname)))
            file_ops.unprotect(self.path, fname)
            self.overlay.updateContent(protected=False)
        else:
            print("Protecting:", ",".join(
                file_ops.related_files(self.path, fname)))
            file_ops.protect(self.path, fname)
            self.overlay.updateContent(protected=True)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        event.accept()
        if event.key() == Qt.Key_Escape:
            print("=== User observable times")
            self.timer.report()
            print("=== Internal loading times")
            self.inner_timer.report()
            self.close()
        if event.key() == Qt.Key.Key_O:
            path = QtWidgets.QFileDialog.getOpenFileName(parent=self, filter="Photos (*.%s)" % (" *.".join(PHOTO_EXTENSIONS)))[0]
            self.openDir(os.path.dirname(path), os.path.basename(path))
        if self.current_index is not None:
            if event.key() == Qt.Key.Key_Right:
                self.switch((self.current_index + 1) % len(self.filenames))
            if event.key() == Qt.Key.Key_Left:
                self.switch((self.current_index - 1) % len(self.filenames))
            if event.key() == Qt.Key.Key_P:
                self.flipProtected()
        return super().keyPressEvent(event)


if __name__ == '__main__':
    appctxt = ApplicationContext()
    window = Viewer()
    window.showMaximized()
    arg = sys.argv[-1]
    if os.path.isdir(arg):
        window.openDir(arg)
    elif os.path.isfile(arg) and any(arg.endswith(e) for e in PHOTO_EXTENSIONS):
        window.openDir(os.path.dirname(arg), os.path.basename(arg))
    else:
        print(arg, "does not seem to be a photo file or a directory")
    appctxt.app.exec()