import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.core.mod_package import read_override, write_override
from src.core.mod_variant_store import ModVariantStore
from tests.core.test_variant_preset_switch import _KEY, _override_value, _user_keys, _write_user_ini
from tests.core.test_variant_preset_switch import env as env
from tests.helpers import apply_preset


def enable(env):
    mm, mod = env["mm"], env["mod"]
    current = mm.preset_manager.get_current("g1")
    current.add_mod_for_character(env["char"].id, mod.mod_id)
    mm.preset_manager._save()
    apply_preset(mm.preset_manager, current.id)
    assert mm._index.get_mod(mod.mod_id).is_enabled
    return mm, mod, current


def save(env, name="白色", value="2"):
    _write_user_ini(env["user_ini"], value)
    return env["mm"].variants.save_variant(env["mod"].mod_id, name)


def test_save_creates_independent_variant_and_current_reference(env):
    mm, mod, current = enable(env)
    info_file = Path(mod.path).parent / ".d3dxmeta/info.json"
    info = json.loads(info_file.read_text(encoding="utf-8"))
    info["info"] = {}
    info_file.write_text(json.dumps(info), encoding="utf-8")
    descriptive_metadata = info_file.read_bytes()
    variant = save(env)
    assert ModVariantStore(mod.path).get(variant.variant_id).variables[0].value == "2"
    assert current.mod_variants[mod.mod_id] == variant.variant_id
    _write_user_ini(env["user_ini"], "3")
    mm.variants.collect(trigger="test")
    assert ModVariantStore(mod.path).get(variant.variant_id).variables[0].value == "2"
    assert current.mod_variants[mod.mod_id] == variant.variant_id
    assert mm.variants.get_state(mod.mod_id)["status"] == "modified"
    assert info_file.read_bytes() == descriptive_metadata


def test_disabled_mod_cannot_save_or_update(env):
    mm, mod = env["mm"], env["mod"]
    _write_user_ini(env["user_ini"], "2")
    with pytest.raises(ValueError, match="启用"):
        mm.variants.save_variant(mod.mod_id, "白色")
    assert ModVariantStore(mod.path).list() == []


def test_source_identity_change_blocks_variant_write(env):
    mm, mod, _ = enable(env)
    marker = Path(mod.path).parent / ".d3dxmod"
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["mod_id"] = "a-different-mod"
    marker.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(OSError, match="身份"):
        save(env)
    assert ModVariantStore(mod.path).list() == []


def test_save_rejects_loader_write_after_its_stable_capture(env, monkeypatch):
    mm, mod, current = enable(env)
    _write_user_ini(env["user_ini"], "2")
    original = mm.variants._execute

    def newer_loader_write(*args, **kwargs):
        _write_user_ini(env["user_ini"], "9")
        return original(*args, **kwargs)

    monkeypatch.setattr(mm.variants, "_execute", newer_loader_write)
    with pytest.raises(RuntimeError, match="外部修改"):
        mm.variants.save_variant(mod.mod_id, "Stale capture")
    assert ModVariantStore(mod.path).list() == []
    assert current.mod_variants.get(mod.mod_id) is None
    assert "9" in env["user_ini"].read_text(encoding="utf-8")


def test_single_apply_rejects_variant_edit_after_planning(env, monkeypatch):
    mm, mod, current = enable(env)
    first = save(env, name="First", value="2")
    second = save(env, name="Second", value="3")
    original = mm.variants._execute

    def edit_variant(*args, **kwargs):
        changed = replace(first, variables=(replace(first.variables[0], value="7"),))
        ModVariantStore(mod.path).save(changed)
        return original(*args, **kwargs)

    monkeypatch.setattr(mm.variants, "_execute", edit_variant)
    with pytest.raises(RuntimeError, match="外部修改"):
        mm.variants.apply_variant(mod.mod_id, first.variant_id)
    assert current.mod_variants[mod.mod_id] == second.variant_id
    assert ModVariantStore(mod.path).get(first.variant_id).variables[0].value == "7"


def test_apply_preserves_working_state_edit_made_after_snapshot(env, monkeypatch):
    mm, mod, current = enable(env)
    first = save(env, name="First", value="2")
    second = save(env, name="Second", value="3")
    original = mm.variants._state_change
    prefs_file = mm.variants.prefs_file()

    def edit_working_state(*args, **kwargs):
        data = json.loads(prefs_file.read_text(encoding="utf-8"))
        data["mods"]["external-mod"] = {"values": {"$external": "7"}}
        prefs_file.write_text(json.dumps(data), encoding="utf-8")
        return original(*args, **kwargs)

    monkeypatch.setattr(mm.variants, "_state_change", edit_working_state)
    with pytest.raises(RuntimeError, match="外部修改"):
        mm.variants.apply_variant(mod.mod_id, first.variant_id)
    data = json.loads(prefs_file.read_text(encoding="utf-8"))
    assert data["mods"]["external-mod"]["values"] == {"$external": "7"}
    assert current.mod_variants[mod.mod_id] == second.variant_id


@pytest.mark.parametrize("command", ["rename", "copy", "delete", "update"])
def test_variant_management_preserves_edit_made_after_snapshot(env, monkeypatch, command):
    mm, mod, current = enable(env)
    variant = save(env)
    # 读取源快照后注入一次外部编辑。
    original = ModVariantStore.get_snapshot
    edited = False

    def read_then_edit(store, identity):
        nonlocal edited
        result = original(store, identity)
        if identity == variant.variant_id and not edited:
            edited = True
            changed = replace(variant, variables=(replace(variant.variables[0], value="7"),))
            store.save(changed)
        return result

    monkeypatch.setattr(ModVariantStore, "get_snapshot", read_then_edit)
    actions = {
        "rename": lambda: mm.variants.rename_variant(mod.mod_id, variant.variant_id, "Renamed"),
        "copy": lambda: mm.variants.copy_variant(mod.mod_id, variant.variant_id),
        "delete": lambda: mm.variants.delete_variant(mod.mod_id, variant.variant_id),
        "update": lambda: mm.variants.save_variant(mod.mod_id, variant.name, variant_id=variant.variant_id),
    }
    with pytest.raises(RuntimeError, match="外部修改"):
        actions[command]()
    records = ModVariantStore(mod.path).list()
    assert len(records) == 1 and records[0].variables[0].value == "7"
    assert records[0].name == variant.name
    assert current.mod_variants[mod.mod_id] == variant.variant_id


def test_apply_restores_snapshot_preserves_keys_and_syncs_deployed_copy(env):
    mm, mod, current = enable(env)
    variant = save(env)
    original_body = Path(mod.path).read_bytes()
    text = read_override(mod.path, "merged.ini")
    assert write_override(mod.path, "merged.ini", text + "\n[KeyToggle]\nkey = F8\n")
    _write_user_ini(env["user_ini"], "9")
    mm.variants.apply_variant(mod.mod_id, variant.variant_id)
    assert _override_value(mod) == "0"
    assert _user_keys(env["user_ini"])[_KEY] == "2"
    assert "key = F8" in read_override(mod.path, "merged.ini")
    assert "$swapvar = 0" in (env["migoto"] / "Mods/AlphaSkin/merged.ini").read_text(encoding="utf-8")
    assert Path(mod.path).read_bytes() == original_body
    assert mm.variants.load_prefs().snapshot_for(mod.mod_id) == {_KEY: "2"}
    assert current.mod_variants[mod.mod_id] == variant.variant_id


def test_default_removes_user_preference_without_mutating_override(env):
    mm, mod, current = enable(env)
    variant = save(env, value="7")
    mm.variants.apply_variant(mod.mod_id, variant.variant_id)
    mm.variants.apply_variant(mod.mod_id, None)
    assert _override_value(mod) == "0"
    assert _KEY not in _user_keys(env["user_ini"])
    assert not mm.variants.load_prefs().state_for(mod.mod_id).confirmed_keys
    assert current.mod_variants[mod.mod_id] is None


def test_disabled_selection_keeps_shared_user_file_unchanged(env):
    mm, mod, current = enable(env)
    variant = save(env)
    mm.toggle_mod(mod.mod_id)
    assert not mm._index.get_mod(mod.mod_id).is_enabled
    _write_user_ini(env["user_ini"], "99")
    before = env["user_ini"].read_bytes()
    mm.variants.apply_variant(mod.mod_id, variant.variant_id)
    assert env["user_ini"].read_bytes() == before
    assert not mm._index.get_mod(mod.mod_id).is_enabled
    assert mod.mod_id not in current.character_mods.get(env["char"].id, [])
    assert mm.variants.load_prefs().state_for(mod.mod_id).selected_variant_id == variant.variant_id


def test_delete_current_variant_restores_default_and_clears_all_references(env):
    mm, mod, current = enable(env)
    variant = save(env)
    copied = mm.preset_manager.create_preset("g1", "副本", snapshot=current.character_mods, copied_from=current.id)
    assert copied.mod_variants[mod.mod_id] == variant.variant_id
    mm.variants.delete_variant(mod.mod_id, variant.variant_id)
    assert ModVariantStore(mod.path).list() == []
    assert _override_value(mod) == "0"
    for p in (current, copied):
        assert mod.mod_id in p.enabled_mod_ids
        assert p.mod_variants[mod.mod_id] is None
    assert mm.variants.load_prefs().state_for(mod.mod_id).selected_variant_id is None


def test_failed_preference_write_restores_configuration_and_selection(env, monkeypatch):
    from src.core import variant_operation

    mm, mod, current = enable(env)
    variant = save(env)
    original = read_override(mod.path, "merged.ini")
    before_prefs = mm.variants.prefs_file().read_bytes()
    before_user = env["user_ini"].read_bytes()
    real = variant_operation.atomic_write

    def fail(path, payload):
        if path == mm.variants.prefs_file() and payload != before_prefs:
            raise OSError("cannot persist preferences")
        return real(path, payload)

    monkeypatch.setattr(variant_operation, "atomic_write", fail)
    with pytest.raises((OSError, RuntimeError)):
        mm.variants.apply_variant(mod.mod_id, variant.variant_id)
    assert read_override(mod.path, "merged.ini") == original
    assert env["user_ini"].read_bytes() == before_user
    before_state = json.loads(before_prefs)["mods"][mod.mod_id]
    after_state = mm.variants.load_prefs().state_for(mod.mod_id)
    assert after_state.values == before_state["values"]
    assert after_state.selected_variant_id == before_state["selected_variant_id"]
    assert after_state.capture_generation >= before_state["capture_generation"]


def test_reapply_same_preset_discards_temporary_working_changes(env):
    mm, mod, current = enable(env)
    variant = save(env)
    _write_user_ini(env["user_ini"], "9")
    mm.variants.collect(trigger="test")
    deployed = env["migoto"] / "Mods/AlphaSkin"
    inode = deployed.stat().st_ino
    apply_preset(mm.preset_manager, current.id)
    assert deployed.stat().st_ino == inode
    assert _override_value(mod) == "0"
    assert _user_keys(env["user_ini"])[_KEY] == "2"
    assert current.mod_variants[mod.mod_id] == variant.variant_id


def test_missing_reference_falls_back_only_for_target_preset(env):
    mm, mod, current = enable(env)
    variant = save(env)
    copied = mm.preset_manager.create_preset("g1", "副本", snapshot=current.character_mods, copied_from=current.id)
    ModVariantStore(mod.path).path_for(variant.variant_id).unlink()
    apply_preset(mm.preset_manager, copied.id)
    assert _override_value(mod) == "0"
    assert copied.mod_variants[mod.mod_id] is None
    assert current.mod_variants[mod.mod_id] == variant.variant_id
    assert mm._index.get_mod(mod.mod_id).is_enabled


def test_corrupt_variant_aborts_preset_switch(env):
    mm, mod, current = enable(env)
    variant = save(env)
    copied = mm.preset_manager.create_preset("g1", "副本", snapshot=current.character_mods, copied_from=current.id)
    ModVariantStore(mod.path).path_for(variant.variant_id).write_text("broken", encoding="utf-8")
    apply_preset(mm.preset_manager, copied.id)
    assert mm._index.get_current_preset_id("g1") == current.id
    assert not mm.preset_manager.has_pending_apply("g1")


def test_missing_values_reject_save_but_explicit_defaults_can_fill(env):
    mm, mod, _ = enable(env)
    env["user_ini"].write_text("[Constants]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="完整|缺|确认"):
        mm.variants.save_variant(mod.mod_id, "未确认")
    variant = mm.variants.save_variant(mod.mod_id, "默认副本", use_defaults=True)
    assert variant.variables[0].value == "0"


@pytest.mark.parametrize("raw", [f"{_KEY} = 2\n{_KEY} = 2\n", f"{_KEY} = nan\n"])
def test_explicit_defaults_do_not_hide_duplicate_or_invalid_runtime_values(env, raw):
    mm, mod, _ = enable(env)
    env["user_ini"].write_text("[Constants]\n" + raw, encoding="utf-8")
    with pytest.raises(ValueError, match="重复|冲突|数值"):
        mm.variants.save_variant(mod.mod_id, "拒绝坏值", use_defaults=True)
    assert ModVariantStore(mod.path).list() == []
