"""在子进程中检查预扫描最后引用释放时的真实 Qt 原生生命周期。"""

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
from PySide6.QtWidgets import QApplication

from src.utils import paths


def run(action, directory):
    """读操作保持未完成时改变宿主，之后允许读操作结束并验证线程释放。"""
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
    from src.ui.dialogs import mod_import

    anim.set_enabled(False)
    started, release = Event(), Event()

    def slow_preview(*_args, **_kwargs):
        started.set()
        assert release.wait(5)
        return []

    mod_import._detect_keys_from_archive = lambda _path: []
    mod_import._detect_previews_from_archive = slow_preview
    dialog = mod_import.ModImportDialog(initial_paths=[str(root / "variants.7z")])
    reference = weakref.ref(dialog._scan_worker)
    assert started.wait(3)
    if action == "reject":
        dialog.reject()
    elif action == "refresh":
        dialog._entries.clear()
        dialog._refresh_table()
    elif action == "destroy":
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        del dialog
        gc.collect()
    else:
        raise ValueError(action)
    release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        gc.collect()
        worker = reference()
        if worker is None or not shiboken6.isValid(worker):
            break
        del worker
        time.sleep(0.005)
    else:
        raise AssertionError("finished scan was not released")
    if action != "destroy":
        dialog.reject()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    print("scan released safely", flush=True)


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
