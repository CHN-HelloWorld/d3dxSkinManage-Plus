import json
import shutil

import pytest

from src.core.mod_identity import write_deployment_identity
from src.core.watcher import FileChange
from src.models.deployment import DeploymentRecord, DeploymentState, PresetApplyJournal
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def source_event(mm, kind, path, destination=""):
    return FileChange(str(mm._index.paths.roots["game_a"]), 1, kind, str(path), str(destination), True)


@pytest.mark.parametrize("operation", ["rename", "delete"])
def test_protected_source_waits_but_unrelated_source_publishes_and_empty_batch_replays(mm, tmp_path, operation):
    node = mm.add_character("Operations")
    first, second = [_import_mod(mm, node.id, tmp_path, name) for name in ("first", "second")]
    a, b = [mm._index.paths.container_path(mod.mod_id) for mod in (first, second)]
    moved_a, moved_b = a.with_name("Moved A"), b.with_name("Moved B")
    from dataclasses import replace

    from src.core.library_publication import _service

    _service(mm, 1).missing_grace = 0
    root = mm._index.paths.roots["game_a"]
    info = root.stat()
    identity = (info.st_dev, info.st_ino)
    mm.protect_library_operation("operation-a", paths=[a], mod_ids=[first.mod_id])
    if operation == "rename":
        a.rename(moved_a)
        event_a = source_event(mm, "moved", a, moved_a)
    else:
        shutil.rmtree(a)
        event_a = replace(
            source_event(mm, "deleted", a),
            native_evidence="delete_pending",
            evidence_mod_id=first.mod_id,
            batch_id=1,
            root_identity=identity,
        )
    b.rename(moved_b)
    delta = mm.apply_local_source_events(
        [
            event_a,
            source_event(mm, "moved", b, moved_b),
            FileChange(str(root), 1, "batch_end", str(root), batch_id=1, root_identity=identity),
        ]
    )
    assert delta.changed_ids == {second.mod_id}
    assert not delta.pending_missing_ids and not delta.review_paths
    assert mm.get_mod(first.mod_id).path == first.path
    assert event_a in mm._deferred_source_events
    assert not mm.confirm_local_missing().removed_ids
    mm.release_library_operation("operation-a")
    delta = mm.apply_local_source_events([])
    assert not mm._deferred_source_events
    if operation == "rename":
        assert delta.changed_ids == {first.mod_id}
        assert mm._index.paths.container_path(first.mod_id) == moved_a
    else:
        assert delta.pending_missing_ids == {first.mod_id}
        mm._local_source_events.missing_grace = 0
        assert mm.confirm_local_missing().removed_ids == {first.mod_id}


def test_split_source_create_at_unindexed_path_is_retained_by_protected_identity(mm, tmp_path):
    mod = _import_mod(mm, mm.add_character("Operations").id, tmp_path, "split")
    old = mm._index.paths.container_path(mod.mod_id)
    new = old.with_name("Unknown destination")
    mm.protect_library_operation("split", mod_ids=[mod.mod_id])
    old.rename(new)
    incoming = source_event(mm, "created", new)
    delta = mm.apply_local_source_events([incoming])
    assert not delta.changed_ids and not delta.review_paths
    assert incoming in mm._deferred_source_events
    mm.release_library_operation("split")
    assert mm.apply_local_source_events([]).changed_ids == {mod.mod_id}


@pytest.mark.parametrize("path_key", ["source_path", "active_path", "disabled_path", "partial_path"])
def test_journal_protects_each_operation_path_until_cleared(mm, tmp_path, path_key):
    node = mm.add_character("Operations")
    mod = _import_mod(mm, node.id, tmp_path, "journal")
    path = mm._index.paths.node_path(node.id) / ("incoming-" + path_key)
    path.mkdir()
    journal = PresetApplyJournal(
        "journal-operation",
        "game_a",
        "",
        "",
        "planned",
        json.dumps({"actions": [{"mod_id": mod.mod_id, path_key: str(path)}]}),
    )
    mm._index.save_journal(journal)
    incoming = source_event(mm, "created", path)
    assert mm.is_library_path_protected(path / "child")
    assert mm.is_library_path_protected(path.parent)
    assert not mm.is_library_path_protected(path.with_name("unrelated"))
    delta = mm.apply_local_source_events([incoming])
    assert not delta.discoveries and incoming in mm._deferred_source_events
    mm._index.delete_journal(journal.operation_id)
    assert str(path) in mm.apply_local_source_events([]).discoveries
    assert not mm._deferred_source_events


def test_protected_deployment_delete_uses_remembered_identity_and_replays(mm, tmp_path):
    mod = _import_mod(mm, mm.add_character("Operations").id, tmp_path, "deployment")
    target = tmp_path / "Mods" / "Active"
    target.mkdir(parents=True)
    write_deployment_identity(target, mod.mod_id, mod.content_hash)
    mm._index.upsert_deployment(DeploymentRecord(mod.mod_id, DeploymentState.ACTIVE, str(target)))
    mm.protect_library_operation("deployment-operation", mod_ids=[mod.mod_id])
    shutil.rmtree(target)
    incoming = FileChange(str(target.parent), 1, "deleted", str(target), is_directory=True)
    assert not mm.apply_local_deployment_events([incoming]).changed_ids
    assert incoming in mm._deferred_deployment_events
    assert mm._index.get_deployment(mod.mod_id).state == DeploymentState.ACTIVE
    mm.release_library_operation("deployment-operation")
    assert mm.apply_local_deployment_events([]).changed_ids == {mod.mod_id}
    assert mm._index.get_deployment(mod.mod_id).state == DeploymentState.ABSENT


def test_switch_game_discards_both_deferred_channels_and_missing_claims(mm, tmp_path):
    mod = _import_mod(mm, mm.add_character("Operations").id, tmp_path, "switch")
    path = mm._index.paths.container_path(mod.mod_id)
    mm.protect_library_operation("old-game", paths=[path], mod_ids=[mod.mod_id])
    mm.apply_local_source_events([source_event(mm, "modified", path)])
    mm.apply_local_deployment_events([FileChange(str(path.parent), 1, "modified", str(path))])
    assert mm._deferred_source_events and mm._deferred_deployment_events
    mm.set_game("game_b")
    mm.release_library_operation("old-game")
    assert not mm._deferred_source_events and not mm._deferred_deployment_events
    assert mm._local_source_events is None
    assert not mm.apply_local_source_events([]).changed_ids
    assert not mm.apply_local_deployment_events([]).changed_ids


def test_releasing_one_operation_keeps_overlapping_other_operation_protected(mm, tmp_path):
    mod = _import_mod(mm, mm.add_character("Operations").id, tmp_path, "overlap")
    path = mm._index.paths.container_path(mod.mod_id)
    for token in ("one", "two"):
        mm.protect_library_operation(token, paths=[path])
    incoming = source_event(mm, "modified", path)
    mm.apply_local_source_events([incoming])
    mm.release_library_operation("one")
    assert not mm.apply_local_source_events([]).changed_ids
    mm.release_library_operation("two")
    assert mm.apply_local_source_events([]).changed_ids == {mod.mod_id}
