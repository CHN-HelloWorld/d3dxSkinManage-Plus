import copy
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from src.core.preset_delete import StalePresetDelete, discard_prepared_preset_delete, write_prepared_preset_delete
from src.core.preset_manager import PresetManager
from src.models.preset import Preset


def prepared(temp_data_dir):
    manager = PresetManager()
    first, second = uuid.uuid4().hex, uuid.uuid4().hex
    preset = Preset(id="saved", name="Saved", game_id="game_a", character_mods={"node": [first, second]})
    manager._presets[preset.id] = preset
    manager._save()
    candidate = manager.prepare_cascade_delete(first, "game_a")
    return manager, preset, candidate, first, second


def test_staging_is_detached_and_commit_reloads_durable_result(temp_data_dir):
    manager, preset, candidate, first, second = prepared(temp_data_dir)
    original = manager._presets_file.read_bytes()
    baseline = manager._cascade_delete_token()
    assert not candidate.temporary.exists()
    with ThreadPoolExecutor(1) as pool:
        assert pool.submit(write_prepared_preset_delete, candidate).result() is candidate
    assert manager._presets_file.read_bytes() == original
    assert manager._cascade_delete_token() == baseline
    assert preset.character_mods == {"node": [first, second]}
    manager.commit_cascade_delete(candidate)
    assert preset.character_mods == {"node": [second]}
    assert not candidate.temporary.exists()
    reloaded = PresetManager()
    assert reloaded._durable_refs[preset.id] == {"node": [second]}
    assert json.loads(manager._presets_file.read_text(encoding="utf8"))["presets"][0] == preset.to_dict()


@pytest.mark.parametrize("edit", ["runtime", "metadata", "durable", "file"])
def test_stale_candidate_keeps_newer_edits_and_can_be_reprepared(temp_data_dir, edit):
    manager, preset, candidate, first, second = prepared(temp_data_dir)
    write_prepared_preset_delete(candidate)
    if edit == "runtime":
        preset.character_mods["node"].append(uuid.uuid4().hex)
    elif edit == "metadata":
        preset.description = "Unsaved edit"
    elif edit == "durable":
        manager._durable_refs[preset.id]["offline"] = [uuid.uuid4().hex]
    else:
        manager._presets_file.write_text('{"external": true}', encoding="utf8")
    token, file_bytes = manager._cascade_delete_token(), manager._presets_file.read_bytes()
    with pytest.raises(StalePresetDelete):
        manager.commit_cascade_delete(candidate)
    assert manager._cascade_delete_token() == token
    assert manager._presets_file.read_bytes() == file_bytes
    assert not candidate.temporary.exists()
    retry = manager.prepare_cascade_delete(first, "game_a")
    assert retry.temporary != candidate.temporary
    write_prepared_preset_delete(retry)
    manager.commit_cascade_delete(retry)
    assert first not in preset.enabled_mod_ids


@pytest.mark.parametrize("stage", ["fsync", "replace"])
def test_failure_preserves_live_intent_and_allows_retry(temp_data_dir, monkeypatch, stage):
    from src.core import preset_delete as module

    manager, preset, candidate, first, second = prepared(temp_data_dir)
    before = manager._presets_file.read_bytes(), manager._cascade_delete_token()

    def fail(*args):
        raise OSError("disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(module.os, stage, fail)
        with pytest.raises(OSError, match="disk failure"):
            write_prepared_preset_delete(candidate)
            manager.commit_cascade_delete(candidate)
    assert (manager._presets_file.read_bytes(), manager._cascade_delete_token()) == before
    assert not candidate.temporary.exists()
    retry = manager.prepare_cascade_delete(first, "game_a")
    write_prepared_preset_delete(retry)
    manager.commit_cascade_delete(retry)
    assert preset.character_mods == {"node": [second]}


def test_cancellation_while_worker_fsyncs_never_publishes(temp_data_dir, monkeypatch):
    from src.core import preset_delete as module

    manager, preset, candidate, first, second = prepared(temp_data_dir)
    before = manager._presets_file.read_bytes(), manager._cascade_delete_token()
    entered, release = Event(), Event()
    fsync = module.os.fsync

    def blocked(descriptor):
        entered.set()
        assert release.wait(5)
        fsync(descriptor)

    monkeypatch.setattr(module.os, "fsync", blocked)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(write_prepared_preset_delete, candidate)
        try:
            assert entered.wait(5)
            discard_prepared_preset_delete(candidate)
        finally:
            release.set()
        with pytest.raises(InterruptedError):
            future.result()
    assert not candidate.temporary.exists()
    assert (manager._presets_file.read_bytes(), manager._cascade_delete_token()) == before


def test_prepare_preserves_unsaved_additions_in_candidate_only(temp_data_dir):
    manager, preset, old_candidate, first, second = prepared(temp_data_dir)
    discard_prepared_preset_delete(old_candidate)
    added = uuid.uuid4().hex
    preset.character_mods["node"].append(added)
    before = copy.deepcopy(preset.character_mods)
    candidate = manager.prepare_cascade_delete(first, "game_a")
    assert preset.character_mods == before
    write_prepared_preset_delete(candidate)
    manager.commit_cascade_delete(candidate)
    assert preset.character_mods == {"node": [second, added]}
