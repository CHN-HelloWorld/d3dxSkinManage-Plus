import asyncio
import gc
import threading
import weakref

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QWidget

from src.ui.pages.gamebanana_store import (
    GameBananaStorePage,
    _AsyncRunner,
    _AsyncWorker,
)


class _Client:
    async def aclose(self):
        pass


class _RunnerHost(QWidget):
    """使用实际所有权管理方法，不加载商店数据或配置。"""

    _make_runner = GameBananaStorePage._make_runner
    _cancel_inflight = GameBananaStorePage._cancel_inflight
    shutdown = GameBananaStorePage.shutdown

    def __init__(self):
        super().__init__()
        self._async_worker = _AsyncWorker()
        self._runners = []
        self._in_flight = 0
        self._client = _Client()


def _deferred_delete():
    QCoreApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()


@pytest.fixture
def runner_host(qtbot):
    host = _RunnerHost()
    worker = host._async_worker
    yield host
    worker.shutdown()
    if shiboken6.isValid(host):
        host.deleteLater()
    _deferred_delete()


def _start_pending(host):
    started = threading.Event()
    stopped = threading.Event()

    async def pending():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    runner = host._make_runner()
    runner.run(pending)
    assert started.wait(2)
    return runner, stopped


def test_navigation_cancellation_releases_runner_qobjects_and_wrappers(qtbot, runner_host):
    refs = []
    for _ in range(5):
        runner, stopped = _start_pending(runner_host)
        refs.append(weakref.ref(runner))
        runner_host._cancel_inflight()
        runner_host._cancel_inflight()
        del runner
        qtbot.waitUntil(stopped.is_set)
        _deferred_delete()
        gc.collect()
        assert runner_host._in_flight == 0
        assert runner_host._runners == []
        assert runner_host.findChildren(_AsyncRunner) == []
        assert all(ref() is None for ref in refs)


@pytest.mark.parametrize("fails", [False, True], ids=["success", "failure"])
def test_observed_future_does_not_retain_completed_runner(qtbot, runner_host, fails):
    received = []
    errors = []

    async def result():
        if fails:
            raise ValueError("synthetic failure")
        return "synthetic result"

    runner = runner_host._make_runner()
    ref = weakref.ref(runner)
    runner.finished.connect(received.append)
    runner.failed.connect(errors.append)
    runner.run(result)
    future = runner._future
    del runner
    qtbot.waitUntil(lambda: bool(errors if fails else received))
    _deferred_delete()
    gc.collect()
    assert future.done()
    assert len(received) == (0 if fails else 1)
    assert len(errors) == (1 if fails else 0)
    assert runner_host._in_flight == 0
    assert runner_host.findChildren(_AsyncRunner) == []
    assert ref() is None


def test_cancel_after_worker_completion_suppresses_queued_delivery_and_preserves_new_count(
    qtbot,
    runner_host,
):
    received = []
    completed = threading.Event()

    async def immediate():
        return "obsolete result"

    obsolete = runner_host._make_runner()
    obsolete.finished.connect(received.append)
    obsolete.run(immediate)
    obsolete._future.add_done_callback(lambda _: completed.set())
    # 等待任务提交结果后再分发 Qt 事件。
    assert completed.wait(2)
    runner_host._cancel_inflight()
    current, stopped = _start_pending(runner_host)
    _deferred_delete()
    assert received == []
    assert runner_host._in_flight == 1
    assert runner_host._runners == [current]
    runner_host._cancel_inflight()
    qtbot.waitUntil(stopped.is_set, timeout=2000)
    _deferred_delete()
    assert runner_host._in_flight == 0
    assert runner_host.findChildren(_AsyncRunner) == []


def test_parent_deletion_cancels_pending_runner_without_late_delivery(qtbot, runner_host):
    received = []
    runner, stopped = _start_pending(runner_host)
    ref = weakref.ref(runner)
    runner.finished.connect(received.append)
    runner.failed.connect(received.append)
    del runner
    runner_host.deleteLater()
    _deferred_delete()
    qtbot.waitUntil(stopped.is_set)
    gc.collect()
    assert received == []
    assert ref() is None


def test_shutdown_retires_pending_runners(qtbot, runner_host):
    runner, stopped = _start_pending(runner_host)
    ref = weakref.ref(runner)
    del runner
    runner_host.shutdown()
    qtbot.waitUntil(stopped.is_set)
    _deferred_delete()
    gc.collect()
    assert not runner_host._async_worker._thread.is_alive()
    assert runner_host._in_flight == 0
    assert runner_host._runners == []
    assert runner_host.findChildren(_AsyncRunner) == []
    assert ref() is None


def test_store_destruction_stops_owned_loop_with_pending_work(
    qtbot,
    temp_data_dir,
    fake_gb_client,
    fake_image_loader,
):
    page = GameBananaStorePage(client=fake_gb_client, image_loader=fake_image_loader)
    worker = page._async_worker
    manager = page._download_manager
    try:
        runner, stopped = _start_pending(page)
        ref = weakref.ref(runner)
        del runner
        page.deleteLater()
        _deferred_delete()
        qtbot.waitUntil(stopped.is_set, timeout=2000)
        assert not worker._thread.is_alive()
        assert page._in_flight == 0
        assert page._runners == []
        gc.collect()
        assert ref() is None
    finally:
        worker.shutdown()
        manager.shutdown()
        if shiboken6.isValid(page):
            page.deleteLater()
        _deferred_delete()
