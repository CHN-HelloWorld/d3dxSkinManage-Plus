"""通过真实工作线程验证插件任务的交付、取消和关闭边界。"""

from threading import Event, get_ident

import pytest

from src.plugin_sdk.errors import PermissionDenied, PluginError


@pytest.fixture
def runner(qtbot):
    """用真实 Qt 主线程交付任务回调，并保证退出时取消未完成任务。"""
    from src.plugins.runtime.tasks import BackgroundTaskRunner
    from src.ui.plugin_tasks import PluginTaskDispatcher

    dispatcher = PluginTaskDispatcher()
    errors = []
    service = BackgroundTaskRunner(
        dispatcher.post, error_sink=lambda *args: errors.append(args), max_workers=1, max_pending=2
    )
    yield service, errors
    service.shutdown()
    dispatcher.close()
    dispatcher.deleteLater()


def test_background_computation_and_callbacks_use_different_threads(runner, qtbot):
    service, _ = runner
    main_thread = get_ident()

    def work(context):
        """在工作线程报告进度并返回纯数据。"""
        context.report_progress(0.5, "处理中")
        return get_ident(), 42

    task = service.submit("test.worker", work)
    callbacks = []
    progress = []
    task.on_finished(lambda result: callbacks.append((get_ident(), result)))
    task.on_progress(lambda value: progress.append((get_ident(), value)))
    qtbot.waitUntil(lambda: task.done)
    assert task.result.ok and task.result.value[1] == 42
    assert task.result.value[0] != main_thread
    assert callbacks[0][0] == main_thread
    assert progress and all(thread == main_thread for thread, _ in progress)
    assert progress[-1][1].fraction == 0.5
    late = []
    task.on_finished(late.append)
    assert late == [task.result]


def test_queue_limit_pending_cancel_and_cooperative_running_cancel(runner, qtbot):
    service, _ = runner
    started, release = Event(), Event()

    def work(context):
        """等待测试释放后检查协作取消。"""
        started.set()
        release.wait(3)
        context.raise_if_cancelled()
        return "complete"

    first = service.submit("test.worker", work)
    qtbot.waitUntil(started.is_set)
    called = []
    second = service.submit("test.other", lambda context: called.append("queued"))
    with pytest.raises(PluginError):
        service.submit("test.third", lambda context: None)
    second.cancel()
    assert second.done and second.result.cancelled
    first.cancel()
    release.set()
    qtbot.waitUntil(lambda: first.done)
    assert first.result.cancelled
    assert called == []


def test_owner_stop_suppresses_queued_and_completed_callbacks_but_other_owner_runs(runner, qtbot):
    service, _ = runner
    callbacks = []
    old = service.submit("test.old", lambda context: 7)
    old.on_finished(callbacks.append)
    service.cancel_owner("test.old")
    old.on_finished(callbacks.append)
    replacement = service.submit("test.old", lambda context: 8)
    replacement.on_finished(lambda result: callbacks.append(result.value))
    qtbot.waitUntil(lambda: replacement.done)
    other = service.submit("test.other", lambda context: 9)
    qtbot.waitUntil(lambda: other.done)
    assert callbacks == [8]
    assert other.result.value == 9
    service.cancel_owner("test.old")
    replacement.on_finished(callbacks.append)
    assert callbacks == [8]


def test_worker_failure_is_a_result_and_callback_failure_has_owner(runner, qtbot):
    service, errors = runner

    def fail(_context):
        """模拟可恢复的后台计算失败。"""
        raise ValueError("bad input")

    task = service.submit("test.failed", fail)
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok and "bad input" in task.result.error
    assert errors == []

    def bad_callback(_result):
        """模拟插件完成处理器中的未捕获异常。"""
        raise RuntimeError("callback failed")

    task.on_finished(bad_callback)
    assert len(errors) == 1 and errors[0][0] == "test.failed"


def test_progress_is_coalesced_and_shutdown_does_not_wait_for_uncooperative_work(qtbot):
    from src.plugins.runtime.tasks import BackgroundTaskRunner

    posted = []
    service = BackgroundTaskRunner(posted.append, max_workers=1)
    started, release, finished = Event(), Event(), Event()

    def work(context):
        """大量报告进度后暂时忽略取消，模拟未能及时协作的插件。"""
        for index in range(1000):
            context.report_progress(index / 1000)
        started.set()
        release.wait(3)
        finished.set()
        return 1

    try:
        task = service.submit("test.worker", work)
        callbacks = []
        task.on_finished(callbacks.append)
        qtbot.waitUntil(started.is_set)
        assert len(posted) == 1
        service.shutdown()
        assert not finished.is_set()
        assert task.done and task.result.cancelled
        for callback in posted:
            callback()
        assert callbacks == []
        with pytest.raises(PluginError):
            service.submit("test.worker", work)
    finally:
        release.set()
        service.shutdown()
    qtbot.waitUntil(finished.is_set)


def test_task_facade_requires_background_permission_and_live_context(runner, qtbot):
    from src.plugin_sdk.tasks import TaskFacade

    service, _ = runner
    forbidden = TaskFacade("test.worker", service, frozenset())
    with pytest.raises(PermissionDenied):
        forbidden.run(lambda context: 1)
    allowed = TaskFacade("test.worker", service, frozenset({"background"}))
    task = allowed.run(lambda context: 2)
    qtbot.waitUntil(lambda: task.done)
    assert task.result.value == 2
    allowed._close()
    with pytest.raises(PluginError):
        allowed.run(lambda context: 3)
