import os
import threading
from dataclasses import FrozenInstanceError

import pytest
import shiboken6
from PySide6.QtCore import QEvent, QObject

from src.core.mod_operations import ModOperationService
from src.plugin_sdk.errors import PermissionDenied
from tests.core.test_named_variant_manager import enable
from tests.core.test_variant_preset_switch import _KEY, _override_value, _user_keys, _write_user_ini
from tests.core.test_variant_preset_switch import env as env
from tests.helpers import apply_preset


@pytest.fixture
def api(qapp, env):
    from src.plugin_sdk.presets import PresetFacade
    from src.plugin_sdk.variants import VariantFacade

    manager = env["mm"]
    owner = QObject(qapp)
    service = ModOperationService(manager, owner)
    permissions = frozenset({"mods:read", "mods:write", "mods:manage"})
    yield (
        PresetFacade(manager, permissions, operations=service, owner="test"),
        VariantFacade(manager, permissions, operations=service, owner="test"),
        service,
    )
    assert service.shutdown()
    owner.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    assert not shiboken6.isValid(owner)


def finish(qtbot, task):
    qtbot.waitUntil(lambda: task.done, timeout=10000)
    assert task.result.ok, task.result.error
    return task.result


def test_preset_create_copy_rename_apply_delete_and_immutable_views(env, api, qtbot):
    manager, mod, current = enable(env)
    presets, variants, _ = api
    _write_user_ini(env["user_ini"], "2")
    vid = finish(qtbot, variants.save(mod.mod_id, "Morning")).variant_id
    original = presets.get_current()
    assert original.preset_id == current.id
    assert original.node_mods[mod.node_id] == (mod.mod_id,)
    assert original.mod_variants[mod.mod_id] == vid
    with pytest.raises(TypeError):
        original.node_mods[mod.node_id] = ()
    with pytest.raises(TypeError):
        original.mod_variants[mod.mod_id] = None
    with pytest.raises(FrozenInstanceError):
        original.name = "changed"
    empty = finish(qtbot, presets.create("Empty", description="description"))
    assert empty.succeeded_ids == ()
    assert presets.get_preset(empty.preset_id).enabled_mod_ids == ()
    assert presets.get_preset(empty.preset_id).description == "description"
    copied = finish(qtbot, presets.duplicate(current.id, name="Copy"))
    assert copied.preset_id != current.id and copied.succeeded_ids == ()
    assert presets.get_preset(copied.preset_id).mod_variants[mod.mod_id] == vid
    finish(qtbot, presets.rename(copied.preset_id, "Renamed"))
    assert presets.get_preset(copied.preset_id).name == "Renamed"
    finish(qtbot, presets.apply(empty.preset_id))
    assert presets.get_current().preset_id == empty.preset_id
    assert not manager.get_mod(mod.mod_id).is_enabled
    finish(qtbot, presets.apply(copied.preset_id))
    assert manager.get_mod(mod.mod_id).is_enabled
    assert _override_value(mod) == "0"
    assert _user_keys(env["user_ini"])[_KEY] == "2"
    assert original.mod_variants[mod.mod_id] == vid
    for pid in (current.id, copied.preset_id):
        task = presets.delete(pid)
        qtbot.waitUntil(lambda: task.done)
        assert not task.result.ok and task.result.error
    finish(qtbot, presets.delete(empty.preset_id))
    assert presets.get_preset(empty.preset_id) is None


def test_variant_round_trip_keeps_identity_and_copies_snapshots(env, api, qtbot, monkeypatch):
    manager, mod, current = enable(env)
    _, variants, _ = api
    from src.core.variant_task import VariantTask

    owner = threading.get_ident()
    workers, publications = [], []
    original_run, original_publish = VariantTask.run, VariantTask.publish

    def run(task):
        workers.append(threading.get_ident())
        return original_run(task)

    def publish(task, manager):
        publications.append(threading.get_ident())
        return original_publish(task, manager)

    monkeypatch.setattr(VariantTask, "run", run)
    monkeypatch.setattr(VariantTask, "publish", publish)
    _write_user_ini(env["user_ini"], "2")
    result = finish(qtbot, variants.save(mod.mod_id, "Morning"))
    vid = result.variant_id
    assert result.succeeded_ids == (mod.mod_id,)
    (before,) = variants.get_variants(mod.mod_id)
    assert before.variant_id == vid and before.variables[0].value == "2"
    with pytest.raises(FrozenInstanceError):
        before.variables[0].value = "9"
    state = variants.get_state(mod.mod_id)
    assert state.selected_variant_id == vid
    with pytest.raises(FrozenInstanceError):
        state.status = "invalid"
    _write_user_ini(env["user_ini"], "4")
    assert finish(qtbot, variants.update(mod.mod_id, vid)).variant_id == vid
    (after,) = variants.get_variants(mod.mod_id)
    assert after.name == "Morning" and after.variables[0].value == "4"
    assert before.variables[0].value == "2"
    finish(qtbot, variants.rename(mod.mod_id, vid, "Evening"))
    copied = finish(qtbot, variants.duplicate(mod.mod_id, vid)).variant_id
    assert copied != vid
    finish(qtbot, variants.apply(mod.mod_id, copied))
    assert current.mod_variants[mod.mod_id] == copied
    finish(qtbot, variants.apply(mod.mod_id))
    assert current.mod_variants[mod.mod_id] is None
    finish(qtbot, variants.delete(mod.mod_id, copied))
    assert [v.variant_id for v in variants.get_variants(mod.mod_id)] == [vid]
    assert workers and all(t != owner for t in workers)
    assert publications and all(t == owner for t in publications)
    assert manager._library_protection.snapshot() == (set(), set())


def test_permissions_cancel_stale_context_and_game_ownership(env, api, qtbot, monkeypatch):
    from src.plugin_sdk.presets import PresetFacade
    from src.plugin_sdk.variants import VariantFacade

    presets, variants, service = api
    manager, mod = env["mm"], env["mod"]
    read_presets = PresetFacade(manager, frozenset())
    read_variants = VariantFacade(manager, frozenset())
    for call in (
        lambda: read_presets.create("Forbidden"),
        lambda: read_presets.delete("x"),
        lambda: read_variants.save(mod.mod_id, "Forbidden"),
        lambda: read_variants.delete(mod.mod_id, "x"),
    ):
        with pytest.raises(PermissionDenied):
            call()
    task = presets.create("Cancelled")
    task.cancel()
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok
    assert not any(p.name == "Cancelled" for p in presets.get_presets())
    task = presets.create("Wrong game")
    monkeypatch.setattr(manager._game_service, "current_game_id", "other")
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok
    assert task.result.game_id == "g1"
    monkeypatch.setattr(manager._game_service, "current_game_id", "g1")
    queued = variants.apply(mod.mod_id)
    callbacks = []
    queued.on_finished(callbacks.append)
    service.cancel_owner("test")
    qtbot.waitUntil(lambda: queued.done)
    assert not queued.result.ok and callbacks == []
    presets._close()
    variants._close()
    for task in (presets.create("Closed"), variants.apply(mod.mod_id)):
        assert task.done and not task.result.ok
    assert not service.busy


def test_shutdown_drains_completed_variant_io_without_callbacks(env, api):
    manager, mod, _ = enable(env)
    _, variants, service = api
    _write_user_ini(env["user_ini"], "2")
    task = variants.save(mod.mod_id, "Saved during close")
    callbacks = []
    task.on_finished(callbacks.append)
    service._drain()
    assert service._active.worker.wait(5000)
    assert service.shutdown()
    assert task.done and task.result.ok and task.result.variant_id
    assert not callbacks
    assert manager._library_protection.snapshot() == (set(), set())


@pytest.mark.parametrize("phase", ["prepare", "execute", "committed"])
def test_shutdown_finishes_every_preset_phase(env, api, qtbot, phase):
    manager, mod, current = enable(env)
    presets, _, service = api
    target = finish(qtbot, presets.create("Empty")).preset_id
    task = presets.apply(target)
    service._drain()
    assert service._active.worker.wait(5000)
    if phase in {"execute", "committed"}:
        service._settle(service._active)
        assert service._active.kind == "preset_execute"
    if phase == "committed":
        qtbot.waitUntil(lambda: service._active is None or service._active.kind == "preset_publish")
    called = []
    task.on_finished(called.append)
    delivered_before_shutdown = list(called)
    assert service.shutdown()
    assert task.done and called == delivered_before_shutdown
    assert not manager.preset_manager.has_pending_apply("g1")
    assert manager._library_protection.snapshot() == (set(), set())
    if phase == "committed":
        assert task.result.ok
        assert not manager.get_mod(mod.mod_id).is_enabled
        assert presets.get_current().preset_id == target
    else:
        assert not task.result.ok
        assert manager.get_mod(mod.mod_id).is_enabled
        assert presets.get_current().preset_id == current.id


def test_preset_commit_failure_rolls_back_deployment_and_config(env, api, qtbot, monkeypatch):
    manager, mod, current = enable(env)
    presets, _, service = api
    target = finish(qtbot, presets.create("Empty")).preset_id
    before_path = manager.get_mod(mod.mod_id).deployed_path
    from pathlib import Path

    def fail_commit(*args, **kwargs):
        raise OSError("commit rejected")

    monkeypatch.setattr(manager.preset_manager, "commit_preset_apply", fail_commit)
    task = presets.apply(target)
    qtbot.waitUntil(lambda: task.done, timeout=10000)
    assert not task.result.ok and "commit rejected" in task.result.error
    assert Path(before_path).is_dir()
    assert manager.get_mod(mod.mod_id).is_enabled
    assert presets.get_current().preset_id == current.id
    assert not manager.preset_manager.has_pending_apply("g1")
    assert manager._library_protection.snapshot() == (set(), set())


def test_variant_deactivation_waits_for_atomic_command_and_suppresses_callback(env, api, qtbot, monkeypatch):
    manager, mod, _ = enable(env)
    _, variants, service = api
    _write_user_ini(env["user_ini"], "2")
    from src.core.variant_task import VariantTask

    original = VariantTask.run
    entered, release = threading.Event(), threading.Event()

    def blocked_run(task):
        entered.set()
        assert release.wait(5)
        return original(task)

    monkeypatch.setattr(VariantTask, "run", blocked_run)
    task = variants.save(mod.mod_id, "Atomic save")
    called = []
    task.on_finished(called.append)
    service._drain()
    try:
        assert entered.wait(2)
        service.cancel_owner("test")
    finally:
        release.set()
    qtbot.waitUntil(lambda: task.done)
    assert task.result.ok and task.result.variant_id
    assert not called
    assert manager._library_protection.snapshot() == (set(), set())


def test_preset_prepared_io_keeps_original_deployment_root(env, api, qtbot, monkeypatch, tmp_path):
    manager, mod = env["mm"], env["mod"]
    current = manager.preset_manager.get_current("g1")
    current.add_mod_for_character(mod.node_id, mod.mod_id)
    manager.preset_manager._save()
    presets, _, service = api
    target = current.id
    from src.core.deployment_preparation import DeploymentPreparation

    original = DeploymentPreparation.before_io
    entered, release = threading.Event(), threading.Event()

    def blocked_before_io(preparation, plan):
        entered.set()
        assert release.wait(5)
        return original(preparation, plan)

    monkeypatch.setattr(DeploymentPreparation, "before_io", blocked_before_io)
    task = presets.apply(target)
    qtbot.waitUntil(entered.is_set)
    try:
        monkeypatch.setattr(manager._storage, "_migoto_mods_dir", tmp_path / "foreign")
    finally:
        release.set()
    result = finish(qtbot, task)
    assert result.game_id == "g1"
    assert not (tmp_path / "foreign").exists()
    assert manager.get_mod(mod.mod_id).deployed_path.startswith(str(env["migoto"]))


def test_preset_metadata_writes_respect_active_variant_reservation(env, api, qtbot):
    manager, mod, _ = enable(env)
    presets, _, _ = api
    reservation = manager.variants.prepare_task(mod.mod_id, command="apply")
    try:
        task = presets.create("Blocked")
        qtbot.waitUntil(lambda: task.done)
        assert not task.result.ok
        assert not any(p.name == "Blocked" for p in presets.get_presets())
    finally:
        reservation.publish(manager)
        reservation.release()


def test_context_exposes_and_closes_preset_variant_writes(env, api, qtbot, tmp_path):
    from src.plugin_sdk.base import PluginMeta
    from src.plugin_sdk.context import PluginContext
    from src.plugins.runtime.commands import CommandTable
    from src.plugins.runtime.eventbus import EventBus
    from src.plugins.runtime.views import ViewRegistry

    ctx = PluginContext(
        meta=PluginMeta("test", "Test", "1.0.0", "2.0", permissions=("mods:read", "mods:write", "mods:manage")),
        app_version="1",
        api_version="2.0",
        plugin_dir=tmp_path,
        data_dir=tmp_path / "plugin-data",
        mod_manager=env["mm"],
        operations=api[2],
        event_bus=EventBus(),
        command_table=CommandTable(),
        view_registry=ViewRegistry(),
    )
    assert hasattr(ctx, "presets") and hasattr(ctx, "variants")
    pid = finish(qtbot, ctx.presets.create("Context preset")).preset_id
    assert ctx.presets.get_preset(pid).name == "Context preset"
    ctx._dispose()
    for task in (ctx.presets.create("Closed"), ctx.variants.apply(env["mod"].mod_id)):
        assert task.done and not task.result.ok


def test_preset_description_update_keeps_snapshot_and_uses_write_permission(env, api, qtbot):
    from src.plugin_sdk.presets import PresetFacade

    presets, _, service = api
    pid = finish(qtbot, presets.create("Description", description="Before")).preset_id
    before = presets.get_preset(pid)
    assert hasattr(presets, "update_description")
    writer = PresetFacade(env["mm"], frozenset({"mods:write"}), operations=service, owner="writer")
    result = finish(qtbot, writer.update_description(pid, "After"))
    assert result.preset_id == pid and result.succeeded_ids == ()
    assert before.description == "Before"
    assert presets.get_preset(pid).description == "After"
    with pytest.raises(PermissionDenied):
        PresetFacade(env["mm"], frozenset()).update_description(pid, "Forbidden")


@pytest.mark.parametrize("operation", ["create", "duplicate", "rename"])
def test_preset_metadata_io_runs_off_owner_and_failed_write_does_not_publish(env, api, qtbot, monkeypatch, operation):
    manager = env["mm"]
    presets, _, _ = api
    pid = finish(qtbot, presets.create("Before")).preset_id
    before = presets.get_presets()
    saved = manager.preset_manager._presets_file.read_bytes()
    owner, writes, notifications = threading.get_ident(), [], []
    manager.preset_manager.preset_list_changed.connect(lambda: notifications.append(threading.get_ident()))

    def fail_fsync(fd):
        writes.append(threading.get_ident())
        raise OSError("staging rejected")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    task = {
        "create": lambda: presets.create("After"),
        "duplicate": lambda: presets.duplicate(pid, name="After"),
        "rename": lambda: presets.rename(pid, "After"),
    }[operation]()
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok and "staging rejected" in task.result.error
    assert presets.get_presets() == before
    assert manager.preset_manager._presets_file.read_bytes() == saved
    assert writes and all(t != owner for t in writes)
    assert not notifications
    assert manager._library_protection.snapshot() == (set(), set())


@pytest.mark.parametrize("background", [False, True])
def test_missing_variant_event_has_persistent_ids_after_commit(env, api, qtbot, monkeypatch, background):
    manager, mod, current = enable(env)
    presets, _, _ = api
    target = manager.preset_manager.create_preset("g1", "Missing", current.character_mods)
    target.mod_variants[mod.mod_id] = "00000000000000000000000000000001"
    manager.preset_manager._save()
    owner, events = threading.get_ident(), []

    def emit(event, **payload):
        if event == "variant_references_missing":
            events.append((payload, threading.get_ident(), manager.preset_manager.get_current("g1").id))

    monkeypatch.setattr(manager, "_emit", emit)
    if background:
        finish(qtbot, presets.apply(target.id))
    else:
        apply_preset(manager.preset_manager, target.id)
    assert len(events) == 1
    payload, thread, committed = events[0]
    assert payload == {"game_id": "g1", "preset_id": target.id, "mod_ids": (mod.mod_id,), "names": (mod.name,)}
    assert thread == owner and committed == target.id


def test_cancelled_staged_preset_delete_reports_no_success(env, api, qtbot):
    presets, _, service = api
    pid = finish(qtbot, presets.create("Keep")).preset_id
    task = presets.delete(pid)
    service._drain()
    assert service._active.worker.wait(5000)
    task.cancel()
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok and task.result.succeeded_ids == ()
    assert presets.get_preset(pid) is not None
    assert env["mm"]._library_protection.snapshot() == (set(), set())


@pytest.mark.parametrize("operation", ["create", "rename", "delete"])
def test_preset_commit_rejection_preserves_disk_and_live_metadata(env, api, qtbot, monkeypatch, operation):
    presets, _, service = api
    manager = env["mm"]
    presets.get_current()
    pid = finish(qtbot, presets.create("Keep")).preset_id
    before, saved = presets.get_presets(), manager.preset_manager._presets_file.read_bytes()
    task = {
        "create": lambda: presets.create("New"),
        "rename": lambda: presets.rename(pid, "New"),
        "delete": lambda: presets.delete(pid),
    }[operation]()
    service._drain()
    assert service._active.worker.wait(5000)
    real_replace = os.replace

    def reject(src, dst):
        if str(dst) == str(manager.preset_manager._presets_file):
            raise OSError("commit rejected")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", reject)
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok and "commit rejected" in task.result.error
    assert presets.get_presets() == before
    assert manager.preset_manager._presets_file.read_bytes() == saved
    assert manager._library_protection.snapshot() == (set(), set())
    assert not list(manager.preset_manager._presets_file.parent.glob(".presets.json.*.tmp"))


def test_shutdown_inside_preset_publication_does_not_settle_twice(env, api, qtbot):
    presets, _, service = api
    shutdown_results, finished = [], []
    env["mm"].preset_manager.preset_list_changed.connect(lambda: shutdown_results.append(service.shutdown()))
    service.finished.connect(finished.append)
    task = presets.create("Committed once")
    qtbot.waitUntil(lambda: task.done)
    assert task.result.ok
    assert len(finished) == 1
    assert presets.get_preset(task.result.preset_id).name == "Committed once"
    assert service.shutdown()


def test_staging_cleanup_failure_does_not_leave_queue_locked(env, api, qtbot, monkeypatch):
    from src.core import preset_delete

    presets, _, service = api
    pid = finish(qtbot, presets.create("Keep")).preset_id
    task = presets.delete(pid)
    service._drain()
    assert service._active.worker.wait(5000)

    def reject(candidate):
        raise PermissionError("temporary file is locked")

    monkeypatch.setattr(preset_delete, "discard_prepared_preset_delete", reject)
    task.cancel()
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok and not service.busy
    assert env["mm"]._library_protection.snapshot() == (set(), set())


def test_metadata_shutdown_cancels_before_publication(env, api):
    presets, _, service = api
    task = presets.create("Cancelled by shutdown")
    service._drain()
    assert service._active.worker.wait(5000)
    assert service.shutdown()
    assert task.done and not task.result.ok
    assert not any(p.name == "Cancelled by shutdown" for p in presets.get_presets())


def test_preset_stale_candidate_does_not_overwrite_newer_metadata(env, api, qtbot):
    presets, _, service = api
    pid = finish(qtbot, presets.create("Before")).preset_id
    task = presets.rename(pid, "Stale")
    service._drain()
    assert service._active.worker.wait(5000)
    env["mm"].preset_manager.update_preset_meta(pid, description="Newer")
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok
    assert presets.get_preset(pid).name == "Before"
    assert presets.get_preset(pid).description == "Newer"


def test_preset_plan_keeps_root_frozen_before_preparation(env, api, qtbot, monkeypatch, tmp_path):
    manager, mod = env["mm"], env["mod"]
    target = manager.preset_manager.get_current("g1")
    target.add_mod_for_character(mod.node_id, mod.mod_id)
    manager.preset_manager._save()
    presets, _, service = api
    task = presets.apply(target.id)
    service._drain()
    assert service._active.worker.wait(5000)
    monkeypatch.setattr(manager._storage, "_migoto_mods_dir", tmp_path / "foreign")
    finish(qtbot, task)
    assert not (tmp_path / "foreign").exists()
    assert manager.get_mod(mod.mod_id).deployed_path.startswith(str(env["migoto"]))


def test_preset_apply_rechecks_target_game_after_worker_preparation(env, api, qtbot):
    presets, _, service = api
    pid = finish(qtbot, presets.create("Target")).preset_id
    task = presets.apply(pid)
    service._drain()
    assert service._active.worker.wait(5000)
    env["mm"].preset_manager.get_preset(pid).game_id = "other"
    qtbot.waitUntil(lambda: task.done)
    assert not task.result.ok
    assert task.result.game_id == "g1"
    assert not env["mm"].preset_manager.has_pending_apply()
    assert env["mm"]._library_protection.snapshot() == (set(), set())


@pytest.mark.parametrize("operation", ["apply", "delete"])
@pytest.mark.parametrize("cancel_after_commit", [False, True])
def test_committed_variant_receipt_survives_readback_failure(env, api, qtbot, operation, cancel_after_commit):
    from src.core.mod_variant_store import ModVariantStore

    manager, mod, current = enable(env)
    _, variants, service = api
    _write_user_ini(env["user_ini"], "2")
    vid = finish(qtbot, variants.save(mod.mod_id, "Good")).variant_id
    saved = ModVariantStore(mod.path).path_for(vid)
    (saved.parent / ("f" * 32 + ".json")).write_text("{broken", encoding="utf-8")
    task = variants.apply(mod.mod_id) if operation == "apply" else variants.delete(mod.mod_id, vid)
    service._drain()
    assert service._active.worker.wait(5000)
    if cancel_after_commit:
        task.cancel()
    qtbot.waitUntil(lambda: task.done)

    assert current.mod_variants[mod.mod_id] is None
    assert task.result.succeeded_ids == (mod.mod_id,)
    assert task.result.failed_ids == task.result.cancelled_ids == ()
    assert task.result.variant_id == (vid if operation == "delete" else None)
    assert task.result.error and not task.result.ok
    if operation == "delete":
        assert not saved.exists()
    assert manager._library_protection.snapshot() == (set(), set())
