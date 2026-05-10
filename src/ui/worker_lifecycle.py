"""停止请求后保留页面工作线程，直到原生线程实际结束。"""

from __future__ import annotations

from functools import partial
from weakref import WeakSet

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThread

_owned_workers: WeakSet[QThread] = WeakSet()
_retiring_workers: set[QThread] = set()
_connected_apps: WeakSet[QCoreApplication] = WeakSet()


def _finished(worker: QThread) -> None:
    """解除结束线程的全局保活引用，并安排 Qt 对象延迟删除。"""
    _retiring_workers.discard(worker)
    try:
        worker.deleteLater()
    except RuntimeError:
        pass


def _retire(worker: QThread, _owner=None) -> None:
    """请求后台线程停止并在结束前保持引用，避免所属页面销毁时连带销毁运行中的线程。"""
    try:
        worker.stop()
        if worker.isRunning():
            if worker not in _retiring_workers:
                _retiring_workers.add(worker)
                worker.finished.connect(partial(_finished, worker), Qt.ConnectionType.QueuedConnection)
            # 线程可能在状态检查和信号连接之间结束。
            if not worker.isRunning():
                _finished(worker)
        else:
            _finished(worker)
    except RuntimeError:
        _retiring_workers.discard(worker)


def finish_managed_workers() -> None:
    """Qt 销毁前完成原生工作线程的退出。"""
    workers = list(_owned_workers)
    for worker in workers:
        try:
            worker.stop()
        except RuntimeError:
            pass
    for worker in workers:
        try:
            # 等待超时不能授权销毁仍在运行的线程。
            worker.wait()
        except RuntimeError:
            pass


def manage_page_worker(owner: QObject, worker: QThread) -> None:
    """所有者销毁时停止工作，但不销毁运行中的线程。

    结束中的线程由独立集合保留，完成后释放引用并在界面线程删除对象。
    应用退出钩子不持有页面，可在事件循环停止后完成等待。
    """
    worker.setParent(None)
    _owned_workers.add(worker)
    owner.destroyed.connect(partial(_retire, worker))
    app = QCoreApplication.instance()
    if app is not None and app not in _connected_apps:
        _connected_apps.add(app)
        app.aboutToQuit.connect(finish_managed_workers)
