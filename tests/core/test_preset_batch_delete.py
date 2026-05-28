import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.core.preset_delete import StalePresetDelete, write_prepared_preset_delete
from src.core.preset_manager import PresetManager
from tests.helpers import registered_games as registered_games


def test_batch_delete_stages_once_and_preserves_default(temp_data_dir):
    manager = PresetManager()
    default = manager.get_current("game_a")
    first = manager.create_preset("game_a", "First")
    second = manager.create_preset("game_a", "Second")
    before = manager._presets_file.read_bytes()
    candidate = manager.prepare_delete_presets([first.id, second.id], "game_a")
    with ThreadPoolExecutor(1) as pool:
        pool.submit(write_prepared_preset_delete, candidate).result(timeout=5)
    assert manager._presets_file.read_bytes() == before
    assert manager.get_preset(first.id) is first
    manager.commit_delete_presets(candidate)
    assert manager.get_preset(first.id) is None and manager.get_preset(second.id) is None
    assert manager.get_current("game_a").id == default.id
    assert [p["id"] for p in json.loads(manager._presets_file.read_text(encoding="utf-8"))["presets"]] == [default.id]


def test_delete_candidate_rejects_newer_preset_edits(temp_data_dir):
    manager = PresetManager()
    manager.get_current("game_a")
    target = manager.create_preset("game_a", "Target")
    candidate = manager.prepare_delete_presets([target.id], "game_a")
    write_prepared_preset_delete(candidate)
    manager.update_preset_meta(target.id, name="New name")
    before = manager._presets_file.read_bytes()
    with pytest.raises(StalePresetDelete):
        manager.commit_delete_presets(candidate)
    assert manager.get_preset(target.id).name == "New name"
    assert manager._presets_file.read_bytes() == before


def test_default_and_current_presets_cannot_be_deleted_before_switch(temp_data_dir):
    manager = PresetManager()
    default = manager.get_current("game_a")
    with pytest.raises(ValueError):
        manager.prepare_delete_presets([default.id], "game_a")
    target = manager.create_preset("game_a", "Current")
    manager._game_store.set_current_preset_id("game_a", target.id)
    with pytest.raises(ValueError):
        manager.prepare_delete_presets([target.id], "game_a")
