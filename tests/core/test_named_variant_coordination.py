import json
import uuid
import zipfile
from pathlib import Path

import pytest

from src.core import migoto_user_ini
from src.core.mod_package import build_local_manifest, read_override, write_sidecar
from src.core.mod_variant_store import ModVariantStore
from src.core.user_ini_watcher import UserIniWatcher
from src.models.mod import Mod
from tests.core.test_named_variant_manager import enable, save
from tests.core.test_variant_preset_switch import _KEY, _override_value, _user_keys, _write_user_ini
from tests.core.test_variant_preset_switch import env as env
from tests.helpers import apply_preset, execute_preset_plan


def event_for(mm, mod, sequence=1):
    service = mm.variants
    return {
        "game_id": mod.game_id,
        "deployment_generation": service._epoch,
        "capture_generation": sequence,
        "mods": (
            {
                "mod_id": mod.mod_id,
                "game_id": mod.game_id,
                "deployment_generation": service._deployment_generation(mod),
            },
        ),
    }


def add_mod(env, name, ini):
    mm = env["mm"]
    container = mm._get_character_library(env["char"].id) / name
    container.mkdir()
    body = container / (name + ".zip")
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr(name + "/merged.ini", ini)
    identity = uuid.uuid4().hex
    (container / ".d3dxmod").write_text(
        json.dumps(
            {
                "schema": 1,
                "kind": "d3dxSkinManage-Plus.mod-container",
                "body": body.name,
                "body_type": "archive",
                "mod_id": identity,
                "content_hash": Mod.compute_sha1(body),
                "imported_at": "2026-01-01T00:00:00",
            }
        ),
        encoding="utf-8",
    )
    source = Mod(name=name, path=str(body), folder_name=name, mod_id=identity)
    assert write_sidecar(body, build_local_manifest(source.to_dict()))
    mm.rebuild_index_from_filesystem("g1")
    return mm._index.get_mod(identity)


def test_event_with_stale_mod_deployment_is_discarded(env):
    mm, mod, _ = enable(env)
    event = event_for(mm, mod)
    event["mods"][0]["deployment_generation"] = "old-deployment"
    _write_user_ini(env["user_ini"], "9")
    before = mm.variants.prefs_file().read_bytes()
    assert mm.variants.collect(trigger="watch", event=event) is None
    assert mm.variants.prefs_file().read_bytes() == before


def test_apply_invalidates_queued_capture_event(env):
    mm, mod, _ = enable(env)
    variant = save(env)
    event = event_for(mm, mod)
    mm.variants.apply_variant(mod.mod_id, variant.variant_id)
    _write_user_ini(env["user_ini"], "9")
    assert mm.variants.collect(trigger="watch", event=event) is None
    assert mm.variants.load_prefs().snapshot_for(mod.mod_id) == {_KEY: "2"}


def test_unstable_capture_event_can_retry_after_stable_write(env, monkeypatch):
    mm, mod, _ = enable(env)
    event = event_for(mm, mod)
    _write_user_ini(env["user_ini"], "9")
    read = migoto_user_ini.read_stable

    def unstable(*args, **kwargs):
        raise migoto_user_ini.UserIniUnstableError("still writing")

    monkeypatch.setattr(migoto_user_ini, "read_stable", unstable)
    assert mm.variants.collect(trigger="watch", event=event) is None
    monkeypatch.setattr(migoto_user_ini, "read_stable", read)
    assert mm.variants.collect(trigger="watch", event=event) is not None
    assert mm.variants.load_prefs().snapshot_for(mod.mod_id) == {_KEY: "9"}


def test_library_invalidation_keeps_watcher_context_live(env):
    mm, mod, _ = enable(env)
    service = mm.variants
    events = []
    service._watcher = UserIniWatcher(service._on_user_ini_changed, enable_watch=False)
    service.set_change_notify(events.append)
    service.refresh_watch()
    service._on_library_event("mod_enabled", mod_id=mod.mod_id)
    service._on_user_ini_changed()
    assert len(events) == 1
    assert events[0]["mods"][0]["mod_id"] == mod.mod_id


@pytest.mark.parametrize("action", ["apply", "delete"])
def test_configuration_preserves_unsynced_deployment_hotkey_edit(env, action):
    mm, mod, _ = enable(env)
    variant = save(env)
    deployed = Path(mm._index.get_mod(mod.mod_id).deployed_path) / "merged.ini"
    content = deployed.read_bytes()
    # 即使平台最初使用链接，也强制创建独立副本。
    replacement = deployed.with_suffix(".copy")
    replacement.write_bytes(content + b"\n[KeyToggle]\nkey = F9\n")
    replacement.replace(deployed)
    if action == "delete":
        mm.variants.delete_variant(mod.mod_id, variant.variant_id)
    else:
        mm.variants.apply_variant(mod.mod_id, variant.variant_id)
    assert "key = F9" in read_override(mod.path, "merged.ini")
    assert "key = F9" in deployed.read_text(encoding="utf-8")


def test_worker_rechecks_unchanged_configuration_after_journal_handshake(env):
    mm, mod, current = enable(env)
    variant = save(env)
    mm.variants.apply_variant(mod.mod_id, variant.variant_id)
    target = mm.preset_manager.create_preset("g1", "Target", snapshot=current.character_mods, copied_from=current.id)
    preparation = mm.create_deployment_preparation(trigger="preset")
    preparation.prepare()
    plan = mm.preset_manager.prepare_preset_apply(target.id, preparation=preparation)
    preparation.before_io(plan)
    execute_preset_plan(mm.preset_manager, plan)

    def persist(op, payload):
        mm._index.update_journal_runtime_patch(op, payload)
        from src.core.mod_package import write_override

        assert write_override(mod.path, "merged.ini", "[Constants]\nglobal persist $swapvar = 7\n")

    preparation.patch_sink = persist
    with pytest.raises(RuntimeError, match="外部修改"):
        preparation.before_commit(plan)
    assert _override_value(mod) == "7"
    assert mm._index.get_current_preset_id("g1") == current.id


def test_preset_excludes_mod_move_until_configuration_is_committed(env):
    mm, mod, current = enable(env)
    save(env)
    destination = mm.add_character(name="Other", node_type="character")
    target = mm.preset_manager.create_preset("g1", "Target", snapshot=current.character_mods, copied_from=current.id)
    preparation = mm.create_deployment_preparation(trigger="preset")
    preparation.prepare()
    plan = mm.preset_manager.prepare_preset_apply(target.id, preparation=preparation)
    preparation.patch_sink = mm._index.update_journal_runtime_patch
    preparation.before_io(plan)
    completed = execute_preset_plan(mm.preset_manager, plan)
    preparation.before_commit(plan)
    assert mm.move_mod(mod.mod_id, destination.id) is False
    assert mm._index.get_mod(mod.mod_id).path == mod.path
    mm.preset_manager.commit_preset_apply(plan, completed, preparation=preparation)
    preparation.after_commit(plan)
    preparation.publish_ini_refs(mm, plan)
    assert mm.move_mod(mod.mod_id, destination.id) is True


def test_owner_commit_rechecks_configuration_after_worker_completion(env):
    mm, mod, current = enable(env)
    save(env)
    target = mm.preset_manager.create_preset("g1", "Target", snapshot=current.character_mods, copied_from=current.id)
    preparation = mm.create_deployment_preparation(trigger="preset")
    preparation.prepare()
    plan = mm.preset_manager.prepare_preset_apply(target.id, preparation=preparation)
    preparation.patch_sink = mm._index.update_journal_runtime_patch
    preparation.before_io(plan)
    completed = execute_preset_plan(mm.preset_manager, plan)
    preparation.before_commit(plan)
    _write_user_ini(env["user_ini"], "99")
    with pytest.raises(RuntimeError, match="外部修改"):
        mm.preset_manager.commit_preset_apply(plan, completed, preparation=preparation)
    assert mm._index.get_current_preset_id("g1") == current.id
    assert "99" in env["user_ini"].read_text(encoding="utf-8")


def test_loader_write_after_durable_commit_does_not_leave_preset_locked(env):
    mm, mod, current = enable(env)
    save(env)
    destination = mm.add_character(name="AfterCommit", node_type="character")
    target = mm.preset_manager.create_preset("g1", "Target", snapshot=current.character_mods, copied_from=current.id)
    preparation = mm.create_deployment_preparation(trigger="preset")
    preparation.prepare()
    plan = mm.preset_manager.prepare_preset_apply(target.id, preparation=preparation)
    preparation.patch_sink = mm._index.update_journal_runtime_patch
    preparation.before_io(plan)
    completed = execute_preset_plan(mm.preset_manager, plan)
    preparation.before_commit(plan)
    mm.preset_manager.commit_preset_apply(plan, completed, preparation=preparation)
    _write_user_ini(env["user_ini"], "99")
    preparation.after_commit(plan)
    preparation.publish_ini_refs(mm, plan)
    assert mm._index.get_current_preset_id("g1") == target.id
    assert "99" in env["user_ini"].read_text(encoding="utf-8")
    assert not list((mm.variants.prefs_file().parent / "variant_operations").glob("*.json"))
    assert mm.move_mod(mod.mod_id, destination.id)


def test_single_mod_apply_rejects_conflict_with_other_enabled_mod(env):
    mm = env["mm"]
    ini = "namespace = shared\n[Constants]\nglobal persist $choice = 0\n"
    a, b = add_mod(env, "First", ini), add_mod(env, "Second", ini)
    for mod in (a, b):
        target = mm._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name)
        mm._remember_deployment(mod, target)
        mod.enable()
        mm._persist_mod_deployment_state(mod)
    before = read_override(a.path, "merged.ini")
    with pytest.raises(ValueError, match="冲突|重复|唯一"):
        mm.variants.apply_variant(a.mod_id, None)
    assert read_override(a.path, "merged.ini") == before


def test_detached_preset_preparation_rejects_corrupt_variant(env):
    mm, mod, current = enable(env)
    variant = save(env)
    target = mm.preset_manager.create_preset("g1", "Target", snapshot=current.character_mods, copied_from=current.id)
    ModVariantStore(mod.path).path_for(variant.variant_id).write_text("broken", encoding="utf-8")
    preparation = mm.create_deployment_preparation(trigger="preset")
    preparation.prepare()
    plan = mm.preset_manager.prepare_preset_apply(target.id, preparation=preparation)
    try:
        with pytest.raises(ValueError):
            preparation.before_io(plan)
    finally:
        mm.preset_manager.fail_preset_apply(plan, RuntimeError("cancel"), preparation=preparation)
    assert mm._index.get_current_preset_id("g1") == current.id


def test_detached_preset_apply_commits_configuration_and_selection(env):
    mm, mod, current = enable(env)
    variant = save(env)
    target = mm.preset_manager.create_preset("g1", "Target", snapshot=current.character_mods, copied_from=current.id)
    _write_user_ini(env["user_ini"], "9")
    preparation = mm.create_deployment_preparation(trigger="preset")
    preparation.prepare()
    plan = mm.preset_manager.prepare_preset_apply(target.id, preparation=preparation)
    preparation.patch_sink = mm._index.update_journal_runtime_patch
    preparation.before_io(plan)
    completed = execute_preset_plan(mm.preset_manager, plan)
    preparation.before_commit(plan)
    mm.preset_manager.commit_preset_apply(plan, completed, preparation=preparation)
    preparation.after_commit(plan)
    preparation.publish_ini_refs(mm, plan)
    assert _override_value(mod) == "0"
    assert _user_keys(env["user_ini"])[_KEY] == "2"
    assert mm.variants.load_prefs().state_for(mod.mod_id).selected_variant_id == variant.variant_id
    assert mm._index.get_current_preset_id("g1") == target.id
    assert not list((mm.variants.prefs_file().parent / "variant_operations").glob("*.json"))


def test_failure_after_configuration_rolls_back_before_removing_new_deployment(env, monkeypatch):
    mm, mod, _ = enable(env)
    variant = save(env)
    empty = mm.preset_manager.create_preset("g1", "Empty", snapshot={})
    apply_preset(mm.preset_manager, empty.id)
    # 复制的预设引用活动目标，但其部署仍处于停放状态。
    target = mm.preset_manager.create_preset("g1", "Target", snapshot={env["char"].id: [mod.mod_id]})
    mm.preset_manager.set_mod_variant(target.id, mod.mod_id, variant.variant_id)
    before = read_override(mod.path, "merged.ini")
    hook = mm.preset_manager._before_plan_commit

    def fail_after_write(plan):
        hook(plan)
        raise OSError("database commit unavailable")

    monkeypatch.setattr(mm.preset_manager, "_before_plan_commit", fail_after_write)
    apply_preset(mm.preset_manager, target.id)
    assert mm._index.get_current_preset_id("g1") == empty.id
    assert read_override(mod.path, "merged.ini") == before
    assert not mm.preset_manager.has_pending_apply("g1")
    assert not list((mm.variants.prefs_file().parent / "variant_operations").glob("*.json"))


def test_notification_failure_cannot_reverse_committed_preset(env, monkeypatch):
    mm, mod, current = enable(env)
    save(env)
    target = mm.preset_manager.create_preset("g1", "Target", snapshot=current.character_mods, copied_from=current.id)

    def fail_notice(plan):
        raise RuntimeError("notification unavailable")

    monkeypatch.setattr(mm.preset_manager, "_after_plan_commit", fail_notice)
    apply_preset(mm.preset_manager, target.id)
    assert mm._index.get_current_preset_id("g1") == target.id
    assert _override_value(mod) == "0"
    assert _user_keys(env["user_ini"])[_KEY] == "2"
    assert not mm.preset_manager.has_pending_apply("g1")


def test_worker_journal_handshake_does_not_hold_configuration_locks(env):
    from concurrent.futures import ThreadPoolExecutor

    mm, mod, current = enable(env)
    save(env)
    target = mm.preset_manager.create_preset("g1", "Target", snapshot=current.character_mods, copied_from=current.id)
    preparation = mm.create_deployment_preparation(trigger="preset")
    preparation.prepare()
    plan = mm.preset_manager.prepare_preset_apply(target.id, preparation=preparation)
    preparation.before_io(plan)
    completed = execute_preset_plan(mm.preset_manager, plan)
    with ThreadPoolExecutor(max_workers=1) as owner:

        def persist(op, payload):
            # 模拟 patch_ready 到达前已排队的主线程界面刷新。
            def read_ui_state():
                ModVariantStore(mod.path).list()
                return mm.variants.load_prefs()

            assert owner.submit(read_ui_state).result(timeout=2) is not None
            mm._index.update_journal_runtime_patch(op, payload)

        preparation.patch_sink = persist
        preparation.before_commit(plan)
    mm.preset_manager.commit_preset_apply(plan, completed, preparation=preparation)
    preparation.after_commit(plan)
    assert mm._index.get_current_preset_id("g1") == target.id


@pytest.mark.parametrize("selection", ["named", "default"])
@pytest.mark.parametrize("location", ["override", "deployment", "both"])
def test_round1_owner_rejects_added_ini_after_worker_completion(env, selection, location):
    from src.core.mod_package import override_path_for

    mm, mod, current = enable(env)
    save(env)
    target = mm.preset_manager.create_preset(
        "g1", "Inventory target", snapshot=current.character_mods, copied_from=current.id
    )
    if selection == "default":
        mm.preset_manager.set_mod_variant(target.id, mod.mod_id, None)
    _write_user_ini(env["user_ini"], "9")
    preparation = mm.create_deployment_preparation(trigger="preset")
    preparation.prepare()
    plan = mm.preset_manager.prepare_preset_apply(target.id, preparation=preparation)
    preparation.patch_sink = mm._index.update_journal_runtime_patch
    preparation.before_io(plan)
    completed = execute_preset_plan(mm.preset_manager, plan)
    before_user = env["user_ini"].read_bytes()
    preparation.before_commit(plan)
    roots = {
        "override": override_path_for(mod.path, "merged.ini").parent,
        "deployment": env["migoto"] / "Mods/AlphaSkin",
    }
    added = []
    for place, root in roots.items():
        if location in (place, "both"):
            path = root / "new-conflict.ini"
            path.write_text(
                "namespace = mods\\alphaskin\\merged.ini\n[Constants]\nglobal persist $swapvar = 123\n",
                encoding="utf-8",
            )
            added.append(path)
    try:
        with pytest.raises(RuntimeError, match="INI|外部修改"):
            mm.preset_manager.commit_preset_apply(plan, completed, preparation=preparation)
        assert mm._index.get_current_preset_id("g1") == current.id
        assert preparation.rollback(plan, completed) == ()
        assert env["user_ini"].read_bytes() == before_user
        assert all(path.is_file() for path in added)
        assert mm.variants.recover_pending() == ()
    finally:
        mm.preset_manager.fail_preset_apply(plan, RuntimeError("inventory changed"), preparation=preparation)


@pytest.mark.parametrize("selection", ["named", "default"])
@pytest.mark.parametrize("location", ["override", "deployment"])
def test_round1_handshake_addition_rejects_apply_without_pending_recovery(env, selection, location):
    from src.core.mod_package import override_path_for

    mm, mod, current = enable(env)
    save(env)
    target = mm.preset_manager.create_preset("g1", "Handshake", snapshot=current.character_mods, copied_from=current.id)
    if selection == "default":
        mm.preset_manager.set_mod_variant(target.id, mod.mod_id, None)
    _write_user_ini(env["user_ini"], "9")
    preparation = mm.create_deployment_preparation(trigger="preset")
    preparation.prepare()
    plan = mm.preset_manager.prepare_preset_apply(target.id, preparation=preparation)
    preparation.before_io(plan)
    completed = execute_preset_plan(mm.preset_manager, plan)
    before = env["user_ini"].read_bytes()
    root = (
        override_path_for(mod.path, "merged.ini").parent if location == "override" else env["migoto"] / "Mods/AlphaSkin"
    )
    added = root / "new.ini"

    def add_before_acknowledgement(operation_id, patch):
        mm._index.update_journal_runtime_patch(operation_id, patch)
        added.write_text("[Constants]\nglobal persist $new_variable = 1\n", encoding="utf-8")

    preparation.patch_sink = add_before_acknowledgement
    try:
        with pytest.raises(RuntimeError, match="INI.*外部修改"):
            preparation.before_commit(plan)
        assert env["user_ini"].read_bytes() == before
        assert added.is_file()
        assert preparation.rollback(plan, completed) == ()
        assert not list((mm.variants.prefs_file().parent / "variant_operations").glob("*.json"))
    finally:
        mm.preset_manager.fail_preset_apply(plan, RuntimeError("inventory changed"), preparation=preparation)


def test_round1_stale_source_guard_recovery_allows_fresh_apply(env):
    from src.core.mod_package import write_override

    mm, mod, current = enable(env)
    variant = save(env)
    target = mm.preset_manager.create_preset(
        "g1", "Guard retry", snapshot=current.character_mods, copied_from=current.id
    )
    _write_user_ini(env["user_ini"], "9")
    preparation = mm.create_deployment_preparation(trigger="preset")
    preparation.prepare()
    plan = mm.preset_manager.prepare_preset_apply(target.id, preparation=preparation)
    preparation.before_io(plan)
    completed = execute_preset_plan(mm.preset_manager, plan)
    before = env["user_ini"].read_bytes()
    edited = "[Constants]\nglobal persist $swapvar = 0\n[KeyToggle]\nkey = z\n"

    def edit_during_acknowledgement(operation_id, patch):
        mm._index.update_journal_runtime_patch(operation_id, patch)
        assert write_override(mod.path, "merged.ini", edited)

    preparation.patch_sink = edit_during_acknowledgement
    try:
        with pytest.raises(RuntimeError, match="外部修改"):
            preparation.before_commit(plan)
        assert env["user_ini"].read_bytes() == before
        assert read_override(mod.path, "merged.ini") == edited
        assert preparation.rollback(plan, completed) == ()
    finally:
        mm.preset_manager.fail_preset_apply(plan, RuntimeError("source changed"), preparation=preparation)
    assert mm.variants.recover_pending() == ()
    assert not list((mm.variants.prefs_file().parent / "variant_operations").glob("*.json"))
    mm.variants.apply_variant(mod.mod_id, variant.variant_id)
    assert _user_keys(env["user_ini"])[_KEY] == "2"
    assert read_override(mod.path, "merged.ini") == edited
