import threading

import pytest
import shiboken6
from PySide6.QtCore import QEvent, QObject

from tests.helpers import register_games, reset_game_service


@pytest.fixture
def library(qapp, temp_data_dir, tmp_path):
    from src.core.config import Config
    from src.core.mod_manager import ModManager

    register_games("plugin-test", "other")
    Config().set("import.format", "zip")
    manager = ModManager()
    manager.set_game("plugin-test")
    character = manager.add_character(name="Character", node_type="character")
    source = tmp_path / "skin"
    source.mkdir()
    (source / "mod.ini").write_text("[Constants]\nx=1\n", encoding="utf-8")
    mod = manager.import_mod(source_path=str(source), node_id=character.id)
    manager.set_deploy_dir(str(tmp_path / "deploy"))
    yield manager, mod
    manager.stop_ini_sync()
    reset_game_service()


@pytest.fixture
def service_for(library, qapp):
    """按主窗口的所有权保持服务存活，并在资源库清理前释放。"""
    from src.core.mod_operations import ModOperationService

    owner = QObject(qapp)
    services = []

    def create(manager, **kwargs):
        service = ModOperationService(manager, owner, **kwargs)
        services.append(service)
        return service

    yield create
    for service in services:
        assert service.shutdown()
    owner.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    assert not shiboken6.isValid(owner)


def test_enable_is_background_and_commit_and_callback_are_main_thread(library, service_for, qtbot, monkeypatch):
    manager, mod = library
    service = service_for(manager)
    main = threading.get_ident()
    io_threads, commit_threads, callbacks = [], [], []
    materialize = manager._storage.materialize_deployment
    finalize = manager.finalize_enable

    def io(*args, **kwargs):
        io_threads.append(threading.get_ident())
        return materialize(*args, **kwargs)

    def commit(*args, **kwargs):
        commit_threads.append(threading.get_ident())
        return finalize(*args, **kwargs)

    monkeypatch.setattr(manager._storage, "materialize_deployment", io)
    monkeypatch.setattr(manager, "finalize_enable", commit)
    task = service.submit("test", "set_enabled", mod_id=mod.mod_id, enabled=True)
    task.on_finished(lambda result: callbacks.append((threading.get_ident(), result)))
    assert not task.done
    qtbot.waitUntil(lambda: task.done)
    assert task.result.ok and task.result.succeeded_ids == (mod.mod_id,)
    assert io_threads and all(t != main for t in io_threads)
    assert commit_threads == [main]
    assert callbacks == [(main, task.result)]
    assert manager.get_mod(mod.mod_id).is_enabled
    assert service.shutdown()


def test_disable_failure_retains_preset_and_reports_failure(library, service_for, qtbot, monkeypatch):
    manager, mod = library
    assert manager.toggle_mod(mod.mod_id).is_enabled
    monkeypatch.setattr(manager._storage, "undeploy_mod", lambda *a, **k: False)
    task = service_for(manager).submit("test", "disable_all")
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok and task.result.failed_ids == (mod.mod_id,)
    assert manager.get_mod(mod.mod_id).is_enabled
    assert mod.mod_id in manager.preset_manager.get_current("plugin-test").enabled_mod_ids


def test_disable_updates_deployment_record_and_emits_once(library, service_for, qtbot):
    manager, mod = library
    assert manager.toggle_mod(mod.mod_id).is_enabled
    events = []
    manager.add_event_listener(lambda name, **data: events.append((name, data)))
    task = service_for(manager).submit("test", "disable_all")
    qtbot.waitUntil(lambda: task.done)
    assert task.result.ok
    assert not manager.get_mod(mod.mod_id).is_enabled
    assert manager._index.get_deployment(mod.mod_id).state != "active"
    assert [name for name, _ in events].count("mod_disabled") == 1


def test_missing_deploy_root_is_failure(library, service_for, qtbot):
    manager, mod = library
    manager.set_deploy_dir("")
    task = service_for(manager).submit("test", "set_enabled", mod_id=mod.mod_id, enabled=True)
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok
    assert not manager.get_mod(mod.mod_id).is_enabled


def test_busy_host_and_stale_game_are_rejected(library, service_for, qtbot):
    manager, mod = library
    service = service_for(manager, host_busy=lambda: True)
    task = service.submit("test", "set_enabled", mod_id=mod.mod_id, enabled=True)
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok
    service = service_for(manager)
    task = service.submit("test", "set_enabled", mod_id=mod.mod_id, enabled=True)
    manager.set_game("other")
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok


def test_deactivation_cancels_queue_and_detaches_callbacks(library, service_for, qtbot):
    manager, mod = library
    service = service_for(manager)
    called = []
    task = service.submit("test", "set_enabled", mod_id=mod.mod_id, enabled=True)
    task.on_finished(called.append)
    service.cancel_owner("test")
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok and task.result.cancelled_ids == (mod.mod_id,)
    assert not called and not manager.get_mod(mod.mod_id).is_enabled


def test_delete_uses_configured_policy(library, service_for, qtbot, monkeypatch):
    from src.core.config import Config

    manager, mod = library
    Config().set("mods.delete_method", "recycle_bin")
    seen = []
    monkeypatch.setattr(manager, "delete_mod", lambda mid, **kw: seen.append(kw) or True)
    task = service_for(manager).submit("test", "delete_mod", mod_id=mod.mod_id)
    qtbot.waitUntil(lambda: task.done)
    assert task.result.ok and seen == [{"delete_method": "recycle_bin"}]


def test_shutdown_commits_completed_io_without_queued_callback(library, service_for, qtbot):
    manager, mod = library
    service = service_for(manager)
    called = []
    task = service.submit("test", "set_enabled", mod_id=mod.mod_id, enabled=True)
    task.on_finished(called.append)
    # 手动启动后台任务，等待期间不分发 Qt 完成信号。
    service._drain()
    assert service._active.worker.wait(5000)
    assert not task.done
    assert service.shutdown()
    assert task.done and task.result.ok and manager.get_mod(mod.mod_id).is_enabled
    assert not called
    qtbot.wait(1)  # 延迟到达的完成信号不应改变最终状态。


def test_completed_task_callbacks_are_detached_on_deactivation(library, service_for, qtbot):
    manager, mod = library
    service = service_for(manager)
    task = service.submit("test", "set_enabled", mod_id=mod.mod_id, enabled=False)
    qtbot.waitUntil(lambda: task.done)
    service.cancel_owner("test")
    called = []
    task.on_finished(called.append)
    assert not called


def test_closed_facade_cannot_submit_again(library, service_for, qtbot):
    from src.plugin_sdk.facades import ModFacade

    manager, mod = library
    service = service_for(manager)
    facade = ModFacade(manager, frozenset({"mods:write"}), operations=service, owner="test")
    facade._close()
    task = facade.set_enabled(mod.mod_id, True)
    assert task.done and not task.result.ok and not service.busy


def test_requests_from_worker_thread_fail_before_touching_sqlite(library, service_for, qtbot):
    manager, mod = library
    service = service_for(manager)
    errors = []

    def request():
        try:
            service.submit("test", "set_enabled", mod_id=mod.mod_id, enabled=True)
        except RuntimeError as error:
            errors.append(str(error))

    worker = threading.Thread(target=request)
    worker.start()
    worker.join(2)
    assert errors and not service.busy


def test_set_enabled_true_does_not_adopt_an_unrecorded_copy(library, service_for, qtbot):
    manager, mod = library
    # 文件操作已完成，但索引尚未提交。
    target = manager._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name)
    assert not manager.get_mod(mod.mod_id).is_enabled
    service = service_for(manager)
    task = service.submit("test", "set_enabled", mod_id=mod.mod_id, enabled=True)
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok
    assert not manager.get_mod(mod.mod_id).is_enabled
    assert target.is_dir()


def test_set_enabled_true_blocks_missing_deployment(library, service_for, qtbot):
    manager, mod = library
    enabled = manager.toggle_mod(mod.mod_id)
    assert enabled.is_enabled
    assert manager._storage.undeploy_mod(
        enabled.path, deploy_name=enabled.folder_name, deploy_path=enabled.deployed_path, mod_id=enabled.mod_id
    )
    assert manager.get_mod(mod.mod_id).is_enabled
    service = service_for(manager)
    task = service.submit("test", "set_enabled", mod_id=mod.mod_id, enabled=True)
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok
    persisted = manager.get_mod(mod.mod_id)
    from pathlib import Path

    assert persisted.is_enabled and not Path(persisted.deployed_path).exists()


def test_disable_all_does_not_discover_or_adopt_unrecorded_deployments(library, service_for, qtbot):
    manager, mod = library
    target = manager._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name)
    service = service_for(manager)
    task = service.submit("test", "disable_all")
    qtbot.waitUntil(lambda: task.done)
    assert task.result.ok and task.result.succeeded_ids == ()
    assert not manager.get_mod(mod.mod_id).is_enabled
    assert target.exists()


def test_set_enabled_restores_parked_copy_without_materializing_again(library, service_for, qtbot, monkeypatch):
    manager, mod = library
    assert manager.toggle_mod(mod.mod_id).is_enabled
    manager.preset_manager.create_preset("plugin-test", "saved", {mod.node_id: [mod.mod_id]})
    assert manager.disable_all_mods() == 1
    assert manager._index.get_deployment(mod.mod_id).state.value == "disabled"

    def forbidden(*args, **kwargs):
        pytest.fail("A parked deployment must be restored without extracting the archive")

    monkeypatch.setattr(manager._storage, "materialize_deployment", forbidden)
    service = service_for(manager)
    task = service.submit("test", "set_enabled", mod_id=mod.mod_id, enabled=True)
    qtbot.waitUntil(lambda: task.done)
    assert task.result.ok and manager.get_mod(mod.mod_id).is_enabled
    assert manager._index.get_deployment(mod.mod_id).state.value == "active"


def test_shutdown_timeout_keeps_active_lock_and_allows_later_requests(library, service_for, qtbot, monkeypatch):
    manager, mod = library
    service = service_for(manager)
    entered, release = threading.Event(), threading.Event()
    original = manager._storage.materialize_deployment

    def slow_materialize(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(manager._storage, "materialize_deployment", slow_materialize)
    task = service.submit("test", "set_enabled", mod_id=mod.mod_id, enabled=True)
    try:
        service._drain()
        assert entered.wait(2)
        assert not service.shutdown(timeout_ms=1)
        assert service.busy and not task.done
        later = service.submit("test", "set_enabled", mod_id=mod.mod_id, enabled=False)
        assert not later.done
        release.set()
        qtbot.waitUntil(lambda: task.done and later.done)
        assert later.result.ok and not service.busy
        assert not manager.get_mod(mod.mod_id).is_enabled
    finally:
        release.set()
        assert service.shutdown()


def test_host_finished_precedes_reentrant_callback_starting_next_task(library, service_for, qtbot):
    from PySide6.QtCore import QEventLoop, QTimer

    manager, mod = library
    service = service_for(manager)
    order = []
    service.finished.connect(lambda result: order.append("host_finished"))
    first = service.submit("test", "set_enabled", mod_id=mod.mod_id, enabled=False)

    def first_finished(result):
        order.append("first_callback")
        nested = QEventLoop()
        second = service.submit("test", "set_enabled", mod_id=mod.mod_id, enabled=False)
        second.on_finished(lambda result: nested.quit())
        QTimer.singleShot(1000, nested.quit)
        nested.exec()
        assert second.done

    first.on_finished(first_finished)
    qtbot.waitUntil(lambda: first.done and not service.busy)
    assert order == ["host_finished", "first_callback", "host_finished"]


@pytest.mark.parametrize("reconcile_phase", ["initial", "disable_prepare"])
def test_user_operation_never_emits_automatic_reconcile_events(
    library, service_for, qtbot, monkeypatch, tmp_path, reconcile_phase
):
    from pathlib import Path

    from src.plugin_sdk import PluginBase
    from src.plugins.runtime.manager import PluginManager
    from src.plugins.runtime.manifest import Manifest
    from src.plugins.runtime.registry import PluginRecord, PluginState

    manager, mod = library
    service = service_for(manager)
    runtime = PluginManager(manager, operations=service)
    if reconcile_phase == "initial":
        target = manager._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name)
    else:
        target = Path(manager.toggle_mod(mod.mod_id).deployed_path)
        source = tmp_path / "second"
        source.mkdir()
        (source / "mod.ini").write_text("[Constants]\nx=2\n", encoding="utf-8")
        second = manager.import_mod(source_path=str(source), node_id=mod.node_id)
        # 请求期间出现的外部部署应保留并交由用户处理。
        service.started.connect(
            lambda *_: manager._storage.materialize_deployment(second.path, second.mod_id, second.folder_name)
        )

    class EventFailure(PluginBase):
        def activate(self, context):
            self.context = context
            context.events.on("mod_enabled", self.fail)

        def fail(self, **payload):
            raise RuntimeError("reconcile event failure")

    manifest = Manifest(
        "event.failure", "Failing event", "1.0.0", "2.0", "entry.py", directory=tmp_path, permissions=("mods:write",)
    )
    record = PluginRecord(manifest, instance=EventFailure(), context=runtime._build_context(manifest), trusted=True)
    runtime._records[record.id] = record
    assert runtime._activate(record)
    io_calls = []
    step = manager.disable_batch_step

    def tracked_step(entry):
        io_calls.append(entry.mod_id)
        return step(entry)

    monkeypatch.setattr(manager, "disable_batch_step", tracked_step)
    task = record.context.mods.set_enabled(mod.mod_id, False)
    try:
        qtbot.waitUntil(lambda: task.done)
        assert record.state is PluginState.ACTIVE
        assert task.result.ok
        assert io_calls == ([] if reconcile_phase == "initial" else [mod.mod_id])
        assert target.is_dir() == (reconcile_phase == "initial")
        assert not manager.get_mod(mod.mod_id).is_enabled
    finally:
        runtime.shutdown()
        assert service.shutdown()
