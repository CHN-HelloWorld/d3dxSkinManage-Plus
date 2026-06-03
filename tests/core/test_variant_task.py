import json
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.core.library_protection import LibraryOperationBusy
from src.core.operation_preflight import OperationPreflightError
from tests.core.test_named_variant_manager import enable, save
from tests.core.test_variant_preset_switch import _KEY, _user_keys, _write_user_ini
from tests.core.test_variant_preset_switch import env as env


def test_detached_switch_writes_files_then_publishes_references_on_owner(env):
    manager, mod, current = enable(env)
    first = save(env, "晨光", "2")
    second = save(env, "夜色", "4")
    task = manager.variants.prepare_task(mod.mod_id, command="apply", variant_id=first.variant_id)
    try:
        with pytest.raises(LibraryOperationBusy):
            with manager._library_protection.variant_operation("other", mod_ids=(mod.mod_id,)):
                pass
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(task.run).result(timeout=5)
        assert _user_keys(env["user_ini"])[_KEY] == "2"
        assert current.mod_variants[mod.mod_id] == second.variant_id
        task.publish(manager)
        assert current.mod_variants[mod.mod_id] == first.variant_id
        assert task.state["selected_variant_id"] == first.variant_id
    finally:
        task.release()
    assert manager._library_protection.snapshot() == (set(), set())


def test_detached_switch_keeps_original_game_and_deployment_root(env, monkeypatch, tmp_path):
    manager, mod, current = enable(env)
    first = save(env, "晨光", "2")
    save(env, "夜色", "4")
    task = manager.variants.prepare_task(mod.mod_id, command="apply", variant_id=first.variant_id)
    monkeypatch.setattr(manager._storage, "get_deploy_dir", lambda: tmp_path / "foreign")
    monkeypatch.setattr(manager._game_service, "current_game_id", "foreign")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(task.run).result(timeout=5)
        task.publish(manager)
        assert _user_keys(env["user_ini"])[_KEY] == "2"
        assert current.mod_variants[mod.mod_id] == first.variant_id
        assert manager.current_game_id == "foreign"
        assert not (tmp_path / "foreign").exists()
    finally:
        task.release()


def test_detached_switch_still_checks_source_identity_before_writing(env):
    manager, mod, current = enable(env)
    first = save(env, "晨光", "2")
    second = save(env, "夜色", "4")
    task = manager.variants.prepare_task(mod.mod_id, command="apply", variant_id=first.variant_id)
    before = env["user_ini"].read_bytes()
    marker = Path(mod.path).parent / ".d3dxmod"
    marker.write_text("{}", encoding="utf-8")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with pytest.raises(OperationPreflightError):
                executor.submit(task.run).result(timeout=5)
        assert env["user_ini"].read_bytes() == before
        assert current.mod_variants[mod.mod_id] == second.variant_id
    finally:
        task.release()


def test_editing_disabled_variant_preserves_capture_for_enabled_mods(env):
    manager, mod, _ = enable(env)
    saved = save(env, "晨光", "2")
    source = Path(mod.path).parent
    disabled = source.parent / "DisabledSkin"
    shutil.copytree(source, disabled)
    marker = disabled / ".d3dxmod"
    record = json.loads(marker.read_text(encoding="utf-8"))
    record["mod_id"] = uuid.uuid4().hex
    marker.write_text(json.dumps(record), encoding="utf-8")
    manager.rebuild_index_from_filesystem("g1")
    manager.variants.stop_watch()
    manager.variants._refresh_watch_context()
    task = manager.variants.prepare_task(record["mod_id"], command="rename", variant_id=saved.variant_id, name="清晨")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(task.run).result(timeout=5)
        task.publish(manager)
    finally:
        task.release()
    _write_user_ini(env["user_ini"], "7")
    events = []
    manager.variants.set_change_notify(events.append)
    manager.variants._on_user_ini_changed()
    report = manager.variants.collect(trigger="test", only_if_changed=True, event=events[-1])

    assert report is not None and report.changed
    assert manager.variants.load_prefs().mods[mod.mod_id].values[_KEY] == "7"


def test_cancel_before_worker_start_keeps_existing_capture_context(env):
    manager, mod, _ = enable(env)
    saved = save(env, "晨光", "2")
    manager.variants.stop_watch()
    manager.variants._refresh_watch_context()
    task = manager.variants.prepare_task(mod.mod_id, command="apply", variant_id=saved.variant_id)
    try:
        # 关闭请求可能在 QThread 进入任务主体前打断执行。
        task.publish(manager)
    finally:
        task.release()
    _write_user_ini(env["user_ini"], "7")
    events = []
    manager.variants.set_change_notify(events.append)
    manager.variants._on_user_ini_changed()
    assert events, "cancelling an unstarted command disabled the file watcher"
    report = manager.variants.collect(trigger="test", only_if_changed=True, event=events[-1])
    assert report is not None and report.changed
    assert manager.variants.load_prefs().mods[mod.mod_id].values[_KEY] == "7"
