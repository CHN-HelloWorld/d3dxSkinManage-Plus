"""验证停用清理和嵌套调用中的旧插件回调不会越过实例生命周期。"""

from threading import Event

import pytest
from PySide6.QtCore import QEvent

from tests.plugins.test_activation import lazy_plugin, prepare
from tests.plugins.test_dependencies import dependency_pair
from tests.plugins.test_operations import library as library
from tests.plugins.test_operations import service_for as service_for
from tests.plugins.test_runtime import env as env


def test_nested_dependency_failure_keeps_consumer_recoverable(env):
    """依赖先熔断后，消费者旧调用栈的异常不能覆盖可恢复的受阻状态。"""
    manager, directory, _ = env
    dependency_pair(directory)
    prepare(manager, "test.provider", "test.consumer")
    assert manager.execute_command("test.consumer.run") == 42
    consumer = manager.get("test.consumer").context

    def provider_failure():
        """模拟依赖命令中的未处理异常。"""
        raise RuntimeError("provider failed")

    def consumer_command():
        """依赖调用返回后继续访问已因故障传播而关闭的上下文。"""
        consumer.commands.execute("test.provider.run")
        consumer.commands.execute("test.provider.run")

    manager.get("test.provider").context.commands.register("test.provider.run", provider_failure)
    consumer.commands.register("test.consumer.run", consumer_command)
    manager.execute_command("test.consumer.run")
    assert manager.get("test.consumer").state.value == "blocked"
    assert manager.enable("test.provider")
    assert manager.execute_command("test.consumer.run") == 42


@pytest.mark.parametrize("source", ["command", "event", "activation", "view"])
@pytest.mark.parametrize("action", ["disable", "reload"])
def test_old_callback_exception_cannot_change_stopped_or_replaced_instance(env, source, action):
    """插件回调中发生停用或重载时，随后抛出的旧异常不归属于当前实例。"""
    manager, directory, _ = env
    lazy_plugin(
        directory,
        permissions=["ui:views"],
        contributes={"commands": [{"id": "test.lazy.run"}], "views": [{"id": "details"}]},
    )
    prepare(manager, "test.lazy")
    record = manager.get("test.lazy")
    if source == "activation":
        assert manager._load(record)
    else:
        assert manager.activate("test.lazy")
    context = record.context

    def replace_then_fail(*_args, **_kwargs):
        """使用宿主生命周期入口替换当前实例，再让旧调用栈报错。"""
        if action == "disable":
            assert manager.disable("test.lazy")
        else:
            assert manager.reload("test.lazy")
            assert manager.activate("test.lazy")
        raise RuntimeError("old callback failed")

    if source == "command":
        context.commands.register("test.lazy.run", replace_then_fail)
        manager.execute_command("test.lazy.run")
    elif source == "event":
        context.events.on("game_changed", replace_then_fail)
        manager.emit("game_changed", game_id="game-a")
    elif source == "activation":
        record.instance.activate = replace_then_fail
        assert not manager.activate("test.lazy")
    else:
        context.ui.register_view("details", replace_then_fail)
        assert manager.create_view("test.lazy", "details") is None

    current = manager.get("test.lazy")
    assert current.state.value == ("disabled" if action == "disable" else "active")
    assert not current.error
    if action == "reload":
        assert current.context is not context
        assert manager.execute_command("test.lazy.run") == 1


def test_teardown_detaches_background_and_events_before_nested_qt_delivery(env, qapp):
    """清理函数处理 Qt 队列和宿主事件时，不得再交付旧任务或事件回调。"""
    from src.plugins.runtime.tasks import BackgroundTaskRunner
    from src.ui.plugin_tasks import PluginTaskDispatcher

    manager, directory, _ = env
    dispatcher = PluginTaskDispatcher()
    posted = Event()
    post_count = 0

    def post(callback):
        """等待真实工作线程把进度和完成都交给 Qt 队列，主线程暂不分发。"""
        nonlocal post_count
        dispatcher.post(callback)
        post_count += 1
        if post_count == 2:
            posted.set()

    runner = BackgroundTaskRunner(post, max_workers=1, error_sink=manager._on_plugin_error)
    manager.set_task_runner(runner)
    lazy_plugin(directory, permissions=["background"])
    prepare(manager, "test.lazy")
    assert manager.activate("test.lazy")
    context = manager.get("test.lazy").context
    delivered = []

    def work(task_context):
        """产生两个待投递的真实后台通知。"""
        task_context.report_progress(0.5)
        return 42

    def nested_delivery():
        """模拟 UI 资源关闭期间处理排队通知及宿主事件。"""
        qapp.processEvents()
        manager.emit("game_changed", game_id="game-a")

    try:
        task = context.tasks.run(work)
        task.on_finished(lambda result: delivered.append(("finished", result.value)))
        task.on_progress(lambda progress: delivered.append(("progress", progress.fraction)))
        context.events.on("game_changed", lambda **_: delivered.append(("event", None)))
        context.subscriptions.add(nested_delivery)
        assert posted.wait(3)
        manager.disable("test.lazy")
        assert delivered == []
        assert task.done and task.result.cancelled
        assert context.subscriptions.disposed
    finally:
        manager.shutdown()
        dispatcher.close()
        dispatcher.deleteLater()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()


@pytest.fixture
def operation_runtime(library, service_for, tmp_path, monkeypatch):
    """把真实业务操作队列绑定到隔离插件目录，并先于队列释放插件运行时。"""
    from src.plugins.runtime import manager as runtime

    mod_manager, mod = library
    directory = tmp_path / "runtime-plugins"
    directory.mkdir()
    monkeypatch.setattr(runtime, "get_plugins_dir", lambda: directory)
    service = service_for(mod_manager)
    manager = runtime.PluginManager(mod_manager, operations=service)
    service._error_sink = lambda owner, error: manager._on_plugin_error(owner, "operation", error)
    lazy_plugin(directory, permissions=["mods:write"])
    prepare(manager, "test.lazy")
    assert manager.activate("test.lazy")
    yield manager, service, mod
    manager.shutdown()


def test_teardown_detaches_operation_before_resource_cleanup(operation_runtime):
    """资源释放时排空真实业务队列，不得交付已停用插件的操作结果。"""
    manager, service, mod = operation_runtime
    context = manager.get("test.lazy").context
    delivered = []
    task = context.mods.set_enabled(mod.mod_id, False)
    task.on_finished(delivered.append)
    context.subscriptions.add(service._drain)
    manager.disable("test.lazy")
    assert task.done and task.result.cancelled_ids == (mod.mod_id,)
    assert delivered == []


def test_old_operation_callback_exception_cannot_stop_replacement(operation_runtime, qtbot):
    """业务完成回调重载自身后抛错，不得熔断同 ID 的新实例。"""
    manager, _service, mod = operation_runtime
    context = manager.get("test.lazy").context

    def reload_then_fail(_result):
        """重建插件后模拟旧业务回调的未处理异常。"""
        assert manager.reload("test.lazy")
        raise RuntimeError("old operation callback failed")

    task = context.mods.set_enabled(mod.mod_id, False)
    task.on_finished(reload_then_fail)
    qtbot.waitUntil(lambda: task.done)
    assert manager.get("test.lazy").state.value == "active"
    assert manager.get("test.lazy").context is not context
    assert manager.execute_command("test.lazy.run") == 1


def test_current_operation_callback_exception_still_stops_plugin(operation_runtime, qtbot):
    """仍属于活动实例的业务回调异常必须继续触发插件熔断。"""
    manager, _service, mod = operation_runtime

    def fail(_result):
        """模拟尚未失效的业务任务观察者异常。"""
        raise RuntimeError("current operation callback failed")

    task = manager.get("test.lazy").context.mods.set_enabled(mod.mod_id, False)
    task.on_finished(fail)
    qtbot.waitUntil(lambda: task.done)
    assert manager.get("test.lazy").state.value == "error"
    assert "current operation callback failed" in manager.get("test.lazy").error
