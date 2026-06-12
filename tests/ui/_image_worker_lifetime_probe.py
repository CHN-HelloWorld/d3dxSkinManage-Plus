from __future__ import annotations

import gc
import os
import sys
import time
import weakref
from pathlib import Path
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QtMsgType, qInstallMessageHandler
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from src.utils import paths


def run(kind, close_first, directory):
    root = Path(directory)
    paths.get_data_dir = lambda: root / "data"
    paths.get_config_dir = lambda: root / "config"
    paths.get_app_dir = lambda: root

    def messages(level, _context, message):
        if level == QtMsgType.QtFatalMsg:
            print(message, flush=True)
            os._exit(17)

    qInstallMessageHandler(messages)
    app = QApplication([])
    from src.ui import anim
    from src.ui.pages import gallery, overview
    from src.ui.widgets import local_preview_cache

    anim.set_enabled(False)
    module = overview if kind == "overview" else gallery
    started, release = Event(), Event()

    def slow_decode(*_args, **_kwargs):
        started.set()
        assert release.wait(8)
        image = QImage(32, 24, QImage.Format.Format_RGB32)
        image.fill(0xFF123456)
        return image

    module.load_scaled_qimage_file = slow_decode
    page = overview.OverviewPage() if kind == "overview" else gallery.GalleryPage()
    worker = page._pixmap_worker
    source = root / "synthetic.png"
    original = QImage(32, 24, QImage.Format.Format_RGB32)
    original.fill(0xFF123456)
    assert original.save(str(source))
    worker.enqueue("slow", str(source), 32, 24, 1)
    assert started.wait(3)
    page_ref = weakref.ref(page)
    worker_ref = weakref.ref(worker)
    if close_first:
        page.close()
    page.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not shiboken6.isValid(page)
    del page
    release.set()
    deadline = time.monotonic() + 5
    while shiboken6.isValid(worker) and time.monotonic() < deadline:
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        time.sleep(0.005)
    assert not shiboken6.isValid(worker), "detached thread must retire after its work finishes"
    del worker
    gc.collect()
    # 发送者销毁后 Qt 可能继续延迟释放 Python 信号代理，待其稳定后再采样。
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()
    assert page_ref() is None
    assert worker_ref() is None
    assert local_preview_cache._prefetched_images.bytes_used == 0
    print("page and worker released; stopped decode did not refill cache", flush=True)


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2] == "close", sys.argv[3])
