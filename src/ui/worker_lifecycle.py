"""停止请求后保留页面工作线程，直到原生线程实际结束。"""

from __future__ import annotations

from functools import partial
from weakref import WeakSet

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QThread, Slot

_owned_workers: WeakSet[QThread] = WeakSet()
_retiring_workers: set[QThread] = set()
_connected_apps: WeakSet[QCoreApplication] = WeakSet()


class WorkerThread(QThread):
    """运行期由宿主持有，延迟销毁时避免回调释放宿主造成重入析构。"""

    def event(self, event: QEvent) -> bool:
        """释放连接和工作闭包前移出宿主子对象列表；调用方仍须先等待线程结束。"""
        if event.type() == QEvent.Type.DeferredDelete:
            self.setParent(None)
        return super().event(event)


class _WorkerOwner(QObject):
    """仅在工作线程有效期间保活，线程销毁后解除宿主上的信号连接。"""

    def __init__(self, owner: QObject, worker: QThread) -> None:
        """以宿主为父对象，分别连接宿主退出和线程销毁的清理入口。"""
        super().__init__(owner)
        self._worker: QThread | None = worker
        owner.destroyed.connect(self.retire)
        worker.destroyed.connect(self.release)

    @Slot()
    def retire(self) -> None:
        """宿主销毁时将仍有效的工作移交给独立退休集合。"""
        if self._worker is not None:
            _retire(self._worker)

    @Slot()
    def release(self) -> None:
        """原生线程对象销毁后释放结果及回调引用，并移除宿主清理接收者。"""
        worker, self._worker = self._worker, None
        if worker is not None:
            _owned_workers.discard(worker)
        self.deleteLater()


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
    线程销毁时解除宿主的保活关系，应用退出钩子不持有页面。
    """
    worker.setParent(None)
    _owned_workers.add(worker)
    _WorkerOwner(owner, worker)
    app = QCoreApplication.instance()
    if app is not None and app not in _connected_apps:
        _connected_apps.add(app)
        app.aboutToQuit.connect(finish_managed_workers)
