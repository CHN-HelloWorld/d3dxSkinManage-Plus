import shutil
from pathlib import Path

import pytest

from src.core.preset_delete import discard_prepared_preset_delete, write_prepared_preset_delete
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def missing_case(mm, tmp_path):
    node = mm.add_character("Stages")
    mod = _import_mod(mm, node.id, tmp_path, "staged")
    preset = mm.preset_manager.get_current("game_a")
    preset.character_mods = {node.id: [mod.mod_id]}
    mm.preset_manager._save()
    candidate = mm.preset_manager.prepare_cascade_delete(mod.mod_id, "game_a")
    shutil.rmtree(mm._index.paths.container_path(mod.mod_id))
    return node, mod, preset, candidate


def test_physical_stage_preserves_preset_and_index_until_candidate_commit(mm, tmp_path):
    node, mod, preset, candidate = missing_case(mm, tmp_path)
    before = candidate.target.read_bytes()
    assert mm._cleanup_missing_source_artifacts(mod, "game_a", locations={})
    assert mm.get_mod(mod.mod_id) is not None
    assert preset.character_mods == {node.id: [mod.mod_id]}
    assert candidate.target.read_bytes() == before
    write_prepared_preset_delete(candidate)
    assert mm._finalize_missing_source_mod(mod, "game_a", preset_candidate=candidate)
    assert mm.get_mod(mod.mod_id) is None
    assert preset.character_mods == {} and candidate.committed


@pytest.mark.parametrize("failure", ["source_restored", "not_staged", "wrong_mod"])
def test_final_stage_rejects_restored_source_or_invalid_candidate(mm, tmp_path, failure):
    node, mod, preset, candidate = missing_case(mm, tmp_path)
    assert mm._cleanup_missing_source_artifacts(mod, "game_a", locations={})
    if failure != "not_staged":
        write_prepared_preset_delete(candidate)
    if failure == "source_restored":
        Path(mod.path).parent.mkdir()
        Path(mod.path).write_bytes(b"restored source")
    elif failure == "wrong_mod":
        candidate.mod_id = "another-owner"
    try:
        assert not mm._finalize_missing_source_mod(mod, "game_a", preset_candidate=candidate)
        assert mm.get_mod(mod.mod_id) is not None
        assert preset.character_mods == {node.id: [mod.mod_id]}
    finally:
        discard_prepared_preset_delete(candidate)


def test_sync_wrapper_does_not_finalize_failed_physical_cleanup(mm, tmp_path, monkeypatch):
    node, mod, preset, candidate = missing_case(mm, tmp_path)
    monkeypatch.setattr(mm, "_remove_known_deployments", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        mm, "_finalize_missing_source_mod", lambda *args, **kwargs: pytest.fail("failed cleanup finalized")
    )
    try:
        assert not mm._purge_missing_source_mod(mod, "game_a", locations={})
        assert mm.get_mod(mod.mod_id) is not None
        assert preset.character_mods == {node.id: [mod.mod_id]}
    finally:
        discard_prepared_preset_delete(candidate)
