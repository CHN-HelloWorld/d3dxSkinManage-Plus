import json
from dataclasses import replace
from pathlib import Path

from src.core.library_snapshot import ProbeToken, probe_library
from src.core.watcher import FileChange
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def audit(manager):
    request = replace(
        manager.create_library_probe_request(ProbeToken("game_a", 1, 1)), mode="full", force_source_audit=True
    )
    result = probe_library(request)
    assert not result.error
    return list(manager.apply_library_probe_result(result))


def test_body_rename_repairs_persisted_marker(mm, tmp_path):
    mod = _import_mod(mm, mm.add_character("Source").id, tmp_path, "Lambda")
    original = Path(mod.path)
    renamed = original.with_name("Lambda1.zip")
    original.rename(renamed)
    assert any(step.changed for step in audit(mm))
    assert Path(mm.get_mod(mod.mod_id).path) == renamed
    marker = json.loads((renamed.parent / ".d3dxmod").read_text(encoding="utf8"))
    assert marker["body"] == renamed.name
    assert marker["mod_id"] == mod.mod_id


def test_audit_claim_clears_pending_missing_after_container_rename(mm, tmp_path):
    mod = _import_mod(mm, mm.add_character("Source").id, tmp_path, "Lambda")
    root = mm._index.paths.roots["game_a"]
    current = Path(mod.path).parent
    for name in ("Lambda333", current.name):
        target = current.with_name(name)
        current.rename(target)
        publication = mm.apply_local_source_events(
            [FileChange(str(root), 1, "deleted", str(current), is_directory=True)]
        )
        assert publication.pending_missing_ids == {mod.mod_id}
        service = mm._local_source_events
        assert mod.mod_id in service._missing
        assert any(step.item_id == mod.mod_id and step.changed for step in audit(mm))
        assert Path(mm.get_mod(mod.mod_id).path).parent == target
        assert mod.mod_id not in mm.confirm_local_missing().pending_missing_ids
        assert mod.mod_id not in service._missing
        assert not service.locations.locations(mod.mod_id) or all(
            Path(location) == target for location in service.locations.locations(mod.mod_id)
        )
        current = target


def test_split_rename_in_one_batch_does_not_republish_resolved_missing(mm, tmp_path):
    mod = _import_mod(mm, mm.add_character("Source").id, tmp_path, "Lambda")
    root = mm._index.paths.roots["game_a"]
    original = Path(mod.path).parent
    target = original.with_name("Lambda333")
    original.rename(target)
    publication = mm.apply_local_source_events(
        [
            FileChange(str(root), 1, "deleted", str(original), is_directory=True),
            FileChange(str(root), 1, "created", str(target), is_directory=True),
        ]
    )
    assert publication.changed_ids == {mod.mod_id}
    assert not publication.pending_missing_ids
