"""已结束任务须释放 Python 数据，关闭宿主仍须等后台工作安全退出。"""

from __future__ import annotations

import gc
import weakref
from threading import Event

import shiboken6
from PySide6.QtCore import QEvent, QObject, QThread

from src.ui.worker_lifecycle import manage_page_worker


class _Payload:
    """提供可弱引用的任务结果，代表工作线程保存的快照或图片数据。"""

    def __init__(self):
        """为每次任务分配独立结果，以便观察旧任务是否仍保留数据。"""
        self.data = bytearray(64 * 1024)


class _ControlledWorker(QThread):
    """使用事件控制实际线程结束，模拟无法立即中断的磁盘操作。"""

    def __init__(self):
        """保存独立结果及启动、停止和完成事件。"""
        super().__init__()
        self.payload = _Payload()
        self.started_work = Event()
        self.finish_work = Event()
        self.stop_requested = Event()

    def run(self):
        """通知测试工作已开始，等待允许结束的事件。"""
        self.started_work.set()
        self.finish_work.wait(5)

    def stop(self):
        """记录协作停止请求，正在执行的工作仍由完成事件控制。"""
        self.stop_requested.set()


def _flush_deletes(qapp):
    """完成级联延迟销毁和信号代理释放，再检查 Python 数据引用。"""
    for _ in range(3):
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()
    gc.collect()


def test_completed_workers_release_payloads_while_owner_stays_alive(qapp):
    owner = QObject()
    workers = []
    payloads = []
    try:
        for _ in range(12):
            worker = _ControlledWorker()
            workers.append(weakref.ref(worker))
            payloads.append(weakref.ref(worker.payload))
            manage_page_worker(owner, worker)
            worker.finish_work.set()
            worker.start()
            assert worker.wait(2000)
            worker.deleteLater()
            del worker
            _flush_deletes(qapp)
        assert shiboken6.isValid(owner)
        assert all(reference() is None for reference in payloads), (
            "completed task data must not accumulate on the owner"
        )
        assert all(reference() is None for reference in workers)
        assert not owner.children(), "finished task ownership helpers must also be released"
    finally:
        owner.deleteLater()
        _flush_deletes(qapp)


def test_owner_destruction_stops_worker_without_deleting_running_thread(qapp, qtbot):
    owner = QObject()
    worker = _ControlledWorker()
    manage_page_worker(owner, worker)
    worker.start()
    try:
        assert worker.started_work.wait(2)
        owner.deleteLater()
        _flush_deletes(qapp)
        assert not shiboken6.isValid(owner)
        assert worker.stop_requested.is_set()
        assert shiboken6.isValid(worker) and worker.isRunning()
        worker.finish_work.set()
        assert worker.wait(2000)
        qtbot.waitUntil(lambda: not shiboken6.isValid(worker))
    finally:
        if shiboken6.isValid(worker):
            worker.finish_work.set()
            assert worker.wait(2000)
            worker.deleteLater()
        if shiboken6.isValid(owner):
            owner.deleteLater()
        _flush_deletes(qapp)


def test_managed_owner_retains_unstarted_worker_until_owner_destruction(qapp):
    owner = QObject()
    worker = _ControlledWorker()
    stopped = worker.stop_requested
    reference = weakref.ref(worker)
    manage_page_worker(owner, worker)
    del worker
    try:
        _flush_deletes(qapp)
        assert reference() is not None and shiboken6.isValid(reference())
        owner.deleteLater()
        _flush_deletes(qapp)
        assert stopped.is_set()
        assert reference() is None
    finally:
        if shiboken6.isValid(owner):
            owner.deleteLater()
        _flush_deletes(qapp)
