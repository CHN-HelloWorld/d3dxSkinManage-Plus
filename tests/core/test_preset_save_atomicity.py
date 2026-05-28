import copy
import json
import shutil
import uuid

import pytest

from src.core.preset_manager import PresetManager
from src.models.preset import Preset
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def test_large_reference_bucket_is_linear_and_preserves_order(temp_data_dir):
    class Identity(str):
        comparisons = 0
        __hash__ = str.__hash__

        def __eq__(self, other):
            type(self).comparisons += 1
            return str.__eq__(self, other)

    identities = [Identity(uuid.uuid4().hex) for _ in range(5000)]
    pm = PresetManager()
    preset = Preset(id="large", game_id="game_a", character_mods={"node": identities + identities[:5]})
    pm._capture_references(preset)
    assert Identity.comparisons < len(identities) * 4
    assert pm._durable_refs[preset.id]["node"] == identities


def test_save_serializes_durable_mapping_without_copying_runtime_mapping(temp_data_dir, monkeypatch):
    pm = PresetManager()
    preset = Preset(id="saved", game_id="game_a", name="Saved", character_mods={"node": [uuid.uuid4().hex]})
    pm._presets[preset.id] = preset
    expected = preset.to_dict()
    monkeypatch.setattr(Preset, "to_dict", lambda self: pytest.fail("runtime mapping serialized only to be discarded"))
    pm._save()
    assert json.loads(pm._presets_file.read_text(encoding="utf8"))["presets"] == [expected]


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_failed_cascade_preserves_file_and_retryable_memory(temp_data_dir, monkeypatch, failure):
    from src.core import preset_manager as module

    pm = PresetManager()
    identity = uuid.uuid4().hex
    preset = Preset(id="saved", game_id="game_a", name="Saved", character_mods={"node": [identity]})
    pm._presets[preset.id] = preset
    pm._save()
    original = pm._presets_file.read_bytes()
    before = copy.deepcopy((preset.character_mods, preset.updated_at, pm._durable_refs, pm._resolved_refs))

    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(module.os, failure, fail)
        with pytest.raises(OSError, match="disk unavailable"):
            pm.cascade_delete_mod(identity, "game_a")
    assert pm._presets_file.read_bytes() == original
    assert (preset.character_mods, preset.updated_at, pm._durable_refs, pm._resolved_refs) == before
    pm.cascade_delete_mod(identity, "game_a")
    assert preset.character_mods == {}
    assert json.loads(pm._presets_file.read_text(encoding="utf8"))["presets"][0]["character_mods"] == {}


def test_source_purge_keeps_index_when_preset_save_fails(mm, tmp_path, monkeypatch):
    from src.core import preset_manager as module

    node = mm.add_character("Source")
    mod = _import_mod(mm, node.id, tmp_path, "source")
    pm = mm.preset_manager
    preset = pm.get_current("game_a")
    preset.character_mods = {node.id: [mod.mod_id]}
    pm._save()
    shutil.rmtree(mm._index.paths.container_path(mod.mod_id))

    def fail(*args):
        raise OSError("preset locked")

    monkeypatch.setattr(module.os, "replace", fail)
    assert not mm._purge_missing_source_mod(mod, "game_a", locations={})
    assert mm._index.get_mod(mod.mod_id) is not None
    assert preset.character_mods == {node.id: [mod.mod_id]}


def test_cached_cascade_does_not_revalidate_unrelated_large_bucket(temp_data_dir, monkeypatch):
    from src.core import preset_manager as module

    pm = PresetManager()
    identities = [uuid.uuid4().hex for _ in range(5001)]
    preset = Preset(id="large", game_id="game_a", name="Large", character_mods={"node": identities})
    pm._presets[preset.id] = preset
    pm._save()
    monkeypatch.setattr(module, "validate_mod_id", lambda mid: pytest.fail("unchanged identity revalidated"))
    pm.cascade_delete_mod(identities[0], "game_a")
    assert preset.character_mods == {"node": identities[1:]}
    assert pm._resolved_refs[preset.id] == preset.character_mods
    pm._save()


def test_cascade_captures_unsaved_runtime_edits_and_keeps_unresolved_refs(temp_data_dir):
    pm = PresetManager()
    removed, retained, added, unresolved = [uuid.uuid4().hex for _ in range(4)]
    preset = Preset(id="edited", game_id="game_a", name="Edited", character_mods={"node": [removed, retained]})
    pm._presets[preset.id] = preset
    pm._save()
    pm._durable_refs[preset.id]["offline"] = [unresolved]
    preset.character_mods["node"].append(added)
    pm.cascade_delete_mod(removed, "game_a")
    saved = json.loads(pm._presets_file.read_text(encoding="utf8"))["presets"][0]
    assert saved["character_mods"] == {"node": [retained, added], "offline": [unresolved]}


def test_save_bytes_match_existing_json_dump_protocol(temp_data_dir):
    pm = PresetManager()
    preset = Preset(id="unicode", game_id="game_a", name="中文预设", character_mods={"node": [uuid.uuid4().hex]})
    pm._presets[preset.id] = preset
    expected = temp_data_dir / "expected.json"
    with expected.open("w", encoding="utf8") as stream:
        json.dump({"version": 2, "presets": [preset.to_dict()]}, stream, indent=2, ensure_ascii=False)
    pm._save()
    assert pm._presets_file.read_bytes() == expected.read_bytes()
