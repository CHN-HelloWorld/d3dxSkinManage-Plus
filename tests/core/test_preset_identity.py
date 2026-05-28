from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.core.config import Config
from src.core.mod_manager import ModManager
from src.core.preset_manager import PresetManager
from tests.core.test_durable_deployment_identity import import_skin
from tests.helpers import apply_preset
from tests.helpers import registered_games as registered_games


@pytest.fixture
def manager(temp_data_dir, tmp_path):
    Config().set("import.format", "zip")
    mm = ModManager()
    mm.set_game("game_a")
    mm.set_deploy_dir(str(tmp_path / "deploy"))
    yield mm
    mm.stop_ini_sync()


def saved_refs(pm, pid):
    data = json.loads(pm._presets_file.read_text(encoding="utf-8"))
    return next(p["character_mods"] for p in data["presets"] if p["id"] == pid)


def test_preset_saves_durable_uuid_but_exposes_runtime_row_ids(manager, tmp_path):
    mod = import_skin(manager, tmp_path)
    manager.toggle_mod(mod.mod_id)
    preset = manager.preset_manager.get_current("game_a")
    assert preset.character_mods == {mod.node_id: [mod.mod_id]}
    assert saved_refs(manager.preset_manager, preset.id) == {mod.node_id: [mod.mod_id]}


def test_reload_empty_database_preserves_and_resolves_intent(manager, tmp_path):
    mod = import_skin(manager, tmp_path)
    manager.toggle_mod(mod.mod_id)
    original = manager.preset_manager.get_current("game_a")
    manager.stop_ini_sync()
    manager._index.clear_mods_for_game("game_a")
    pm = PresetManager(mod_index=manager._index, mod_storage=manager._storage)
    manager._preset_manager = pm
    assert pm.get_current("game_a").enabled_mod_ids == []
    pm.update_preset_meta(original.id, description="Retain desired Mods")
    assert saved_refs(pm, original.id) == {mod.node_id: [mod.mod_id]}
    manager.rebuild_index_from_filesystem("game_a")
    restored = manager._index.get_mod(mod.mod_id)
    assert pm.get_current("game_a").enabled_mod_ids == [restored.mod_id]
    assert saved_refs(pm, original.id) == {mod.node_id: [mod.mod_id]}


def test_same_name_mods_can_be_selected_together_in_one_preset(manager, tmp_path):
    first = import_skin(manager, tmp_path, character="First")
    second = import_skin(manager, tmp_path, character="Second", value=2)
    preset = manager.preset_manager.create_preset(
        "game_a", "Both", snapshot={first.node_id: [first.mod_id], second.node_id: [second.mod_id]}
    )
    apply_preset(manager.preset_manager, preset.id)
    one, two = manager.get_mod(first.mod_id), manager.get_mod(second.mod_id)
    assert one.is_enabled and two.is_enabled
    assert one.deployed_path != two.deployed_path
    assert "x=1" in (Path(one.deployed_path) / "mod.ini").read_text()
    assert "x=2" in (Path(two.deployed_path) / "mod.ini").read_text()


def test_preset_switch_preserves_external_rename_when_parking_and_restoring(manager, tmp_path, monkeypatch):
    mod = import_skin(manager, tmp_path)
    enabled = manager.toggle_mod(mod.mod_id)
    active_preset = manager.preset_manager.get_current("game_a")
    renamed = Path(enabled.deployed_path).with_name("ChosenByUser")
    Path(enabled.deployed_path).rename(renamed)
    manager.reconcile_missing_deployments("game_a")
    empty = manager.preset_manager.create_preset("game_a", "Empty")
    apply_preset(manager.preset_manager, empty.id)
    parked = manager.get_mod(mod.mod_id)
    assert not parked.is_enabled
    assert Path(parked.deployed_path).name.endswith("ChosenByUser")
    assert Path(parked.deployed_path).is_dir()

    def no_extract(*args, **kwargs):
        pytest.fail("A parked deployment should be restored without extraction")

    monkeypatch.setattr(manager._storage, "materialize_deployment", no_extract)
    apply_preset(manager.preset_manager, active_preset.id)
    restored = manager.get_mod(mod.mod_id)
    assert restored.is_enabled
    assert Path(restored.deployed_path) == renamed


def test_external_deletion_keeps_preset_intent_and_can_redeploy(manager, tmp_path):
    mod = import_skin(manager, tmp_path)
    enabled = manager.toggle_mod(mod.mod_id)
    preset = manager.preset_manager.get_current("game_a")
    shutil.rmtree(enabled.deployed_path)
    manager.reconcile_missing_deployments("game_a")
    apply_preset(manager.preset_manager, preset.id)
    current = manager.get_mod(mod.mod_id)
    assert current.is_enabled
    assert Path(current.deployed_path).is_dir()
    assert preset.enabled_mod_ids == [current.mod_id]


def test_sync_apply_commit_failure_restores_deployment(manager, tmp_path, monkeypatch):
    mod = import_skin(manager, tmp_path)
    enabled = manager.toggle_mod(mod.mod_id)
    original = Path(enabled.deployed_path)
    pm = manager.preset_manager
    previous = pm.get_current("game_a").id
    empty = pm.create_preset("game_a", "Empty")

    def fail_commit(*args):
        raise RuntimeError("injected database commit failure")

    monkeypatch.setattr(pm, "commit_preset_apply", fail_commit)
    assert apply_preset(pm, empty.id) == []
    assert original.is_dir()
    assert manager.get_mod(mod.mod_id).is_enabled
    assert pm.get_current("game_a").id == previous
    assert manager._index.get_journals("game_a") == []
