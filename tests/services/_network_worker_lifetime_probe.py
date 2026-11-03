"""隔离检查服务先销毁而下载线程尚未退出的原生对象安全。"""

from __future__ import annotations

import gc
import sys
import threading
import weakref
from pathlib import Path

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent

from src.services import network


def _flush_deletes() -> None:
    """先投递结果，再销毁 Qt 对象和回收包装，观察最终保活状态。"""
    QCoreApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()
    gc.collect()


def main() -> None:
    """让服务在下载阻塞期间销毁，再验证取消、线程保活及最终释放。"""
    app = QCoreApplication([])
    app.setQuitLockEnabled(False)
    entered = threading.Event()
    release = threading.Event()

    class Worker(network.DownloadWorker):
        """通过事件延迟线程结束，复现服务先于网络读取结束的情况。"""

        def run(self):
            """等待测试释放，取消场景不发送业务完成信号。"""
            entered.set()
            assert release.wait(5)

    network.DownloadWorker = Worker
    service = network.NetworkService()
    worker = service.download_file("https://example.invalid/pending.zip", Path(sys.argv[1]) / "pending.zip")
    reference = weakref.ref(worker)
    try:
        assert entered.wait(3)
        service.deleteLater()
        _flush_deletes()
        assert not shiboken6.isValid(service)
        assert shiboken6.isValid(worker) and worker.isRunning()
        assert worker._cancelled
        del worker
        gc.collect()
        assert reference() is not None
    finally:
        release.set()
        live = reference()
        if live is not None and shiboken6.isValid(live):
            assert live.wait(3000)
        del live
    _flush_deletes()
    assert reference() is None


if __name__ == "__main__":
    main()
