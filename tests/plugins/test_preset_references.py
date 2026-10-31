"""验证预设 SDK 对暂时无法解析的持久 Mod 引用保留独立快照。"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from src.core.preset_manager import PresetManager
from src.plugin_sdk.facades import ModFacade
from src.plugin_sdk.presets import PresetFacade, PresetInfo
from tests.plugins.test_examples import example_module
from tests.plugins.test_operations import library as library

MISSING_ID = "00000000000040008000000000000001"


@pytest.mark.parametrize("keep_variant_mapping", [True, False])
def test_reloaded_preset_exposes_missing_references_without_rewriting_storage(
    library, temp_data_dir, monkeypatch, keep_variant_mapping
):
    manager, mod = library
    preset = manager.preset_manager.create_preset("plugin-test", "暂缺资源", {mod.node_id: [mod.mod_id, MISSING_ID]})
    manager.preset_manager._game_store.set_current_preset_id("plugin-test", preset.id)
    path = temp_data_dir / "data/presets.json"
    if not keep_variant_mapping:
        document = json.loads(path.read_text(encoding="utf-8"))
        for record in document["presets"]:
            record.pop("mod_variants", None)
        path.write_text(json.dumps(document), encoding="utf-8")
    persisted = path.read_bytes()
    restored = PresetManager(mod_index=manager._index, mod_storage=manager._storage)
    monkeypatch.setattr(manager, "_preset_manager", restored)
    facade = PresetFacade(manager, frozenset({"mods:read"}))
    views = [
        facade.get_preset(preset.id),
        facade.get_current(),
        next(view for view in facade.get_presets() if view.preset_id == preset.id),
    ]
    for view in views:
        assert view.enabled_mod_ids == (mod.mod_id,)
        assert getattr(view, "unresolved_mod_ids", ()) == (MISSING_ID,)
    issues = example_module("example.preset-audit")["audit_presets"](
        [views[0]], ModFacade(manager, frozenset({"mods:read"})).get_mods(), {}
    )
    assert [(issue.mod_id, issue.reason) for issue in issues] == [(MISSING_ID, "missing_mod")]
    assert path.read_bytes() == persisted


def test_unsaved_member_removal_is_not_reported_as_an_unresolved_reference(library):
    manager, mod = library
    preset = manager.preset_manager.create_preset("plugin-test", "编辑中", {mod.node_id: [mod.mod_id]})
    preset.remove_mod(mod.mod_id)
    view = PresetFacade(manager, frozenset({"mods:read"})).get_preset(preset.id)
    assert view.enabled_mod_ids == ()
    assert view.unresolved_mod_ids == ()


def test_unresolved_reference_snapshot_copies_input_and_is_immutable():
    pending = [MISSING_ID]
    view = PresetInfo("p", "g", "待处理", unresolved_mod_ids=pending)
    pending.clear()
    assert view.unresolved_mod_ids == (MISSING_ID,)
    with pytest.raises(FrozenInstanceError):
        view.unresolved_mod_ids = ()
