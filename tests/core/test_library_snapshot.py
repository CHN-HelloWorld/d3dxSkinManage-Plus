from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def test_unchanged_poll_does_not_materialize_entire_library(mm, monkeypatch, tmp_path):
    char = mm.add_character(name="Snapshot", node_type="character")
    for name in ("snapshot_a", "snapshot_b"):
        _import_mod(mm, char.id, tmp_path, name)
    mm.reconcile_library("game_a", trigger="poll")
    calls = []
    original = mm._index.get_all_mods

    def tracked(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(mm._index, "get_all_mods", tracked)
    report = mm.reconcile_library("game_a", trigger="poll")
    assert calls == []
    assert not report.changed


def test_unchanged_poll_checks_each_source_once(mm, monkeypatch, tmp_path):
    from src.utils import fs

    char = mm.add_character(name="Snapshot", node_type="character")
    mods = [_import_mod(mm, char.id, tmp_path, name) for name in ("check_a", "check_b")]
    mm.reconcile_library("game_a", trigger="poll")
    counts = {os.path.normcase(os.path.abspath(mod.path)): 0 for mod in mods}
    original = os.stat

    def tracked(path, *args, **kwargs):
        key = os.path.normcase(os.path.abspath(fs.normal_path(path)))
        if key in counts:
            counts[key] += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", tracked)
    report = mm.reconcile_library("game_a", trigger="poll")
    assert not report.changed
    assert list(counts.values()) == [1, 1]


def test_library_rows_are_immutable_and_game_scoped(mm, tmp_path):
    char = mm.add_character(name="Snapshot", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "immutable")
    rows = mm._index.get_library_rows("game_a")
    assert isinstance(rows, tuple)
    assert len(rows) == 1
    assert rows[0].mod_id == mod.mod_id
    assert rows[0].mod_id == mod.mod_id
    assert rows[0].path == mod.path
    assert rows[0].preview_images == tuple(mod.preview_images)
    assert rows[0].enabled == mod.is_enabled
    assert mm._index.get_library_rows("game_b") == ()
    with pytest.raises(FrozenInstanceError):
        rows[0].path = "changed"


def test_lightweight_rows_do_not_parse_unrelated_metadata(mm, tmp_path):
    char = mm.add_character(name="Snapshot", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "metadata")
    mm._index._db().execute("UPDATE mods SET info_json = ? WHERE mod_id = ?", ("broken", mod.mod_id))
    mm._index._db().commit()
    assert mm._index.get_library_rows("game_a")[0].mod_id == mod.mod_id


def test_lightweight_query_failure_is_not_an_empty_library(mm):
    import sqlite3

    mm._index.close()
    with pytest.raises(sqlite3.Error):
        mm._index.get_library_rows("game_a")


def test_committed_snapshot_reuses_rows_until_sql_changes(mm, tmp_path):
    mod = _import_mod(mm, mm.add_character(name="Cache", node_type="character").id, tmp_path, "reuse")
    index = mm._index
    before = index.get_library_rows("game_a")
    assert index.get_library_rows("game_a") is before
    index._db().execute(
        "INSERT INTO deployment_records VALUES(?,'active','') ON CONFLICT(mod_id) DO UPDATE SET state='active'",
        (mod.mod_id,),
    )
    index._db().commit()
    after = index.get_library_rows("game_a")
    assert after is not before
    assert after[0].enabled
    assert not before[0].enabled


def test_transaction_snapshot_cannot_survive_rollback(mm, tmp_path):
    _import_mod(mm, mm.add_character(name="Cache", node_type="character").id, tmp_path, "rollback")
    index = mm._index
    original = index.get_library_rows("game_a")
    index._db().execute("INSERT INTO deployment_records SELECT mod_id,'active','' FROM mods")
    assert index.get_library_rows("game_a")[0].enabled
    index._db().rollback()
    restored = index.get_library_rows("game_a")
    assert restored == original
    assert index.get_library_rows("game_a") is restored


def test_committed_change_only_reparses_changed_snapshot_rows(mm, tmp_path, monkeypatch):
    from src.core import mod_index

    char = mm.add_character(name="Row reuse", node_type="character")
    changed, unchanged = [_import_mod(mm, char.id, tmp_path, name) for name in ("changed", "unchanged")]
    index = mm._index
    before = {row.mod_id: row for row in index.get_library_rows("game_a")}
    index._db().execute(
        "INSERT INTO deployment_records VALUES(?,'active','') ON CONFLICT(mod_id) DO UPDATE SET state='active'",
        (changed.mod_id,),
    )
    index._db().commit()
    loads = mod_index.json.loads
    decoded = []

    def decode(value):
        decoded.append(value)
        return loads(value)

    monkeypatch.setattr(mod_index.json, "loads", decode)
    after = {row.mod_id: row for row in index.get_library_rows("game_a")}
    assert after[changed.mod_id].enabled
    assert not before[changed.mod_id].enabled
    assert after[unchanged.mod_id] is before[unchanged.mod_id]
    assert len(decoded) == 1


def test_external_commit_invalidates_snapshot_and_probe_stamp(mm, tmp_path):
    import sqlite3

    _import_mod(mm, mm.add_character(name="Cache", node_type="character").id, tmp_path, "external")
    before = mm._index.get_library_rows("game_a")
    stamp = mm.library_probe_stamp
    with sqlite3.connect(mm._index._db_path) as other:
        from src.core.library_schema import register_connection

        register_connection(other)
        other.execute("INSERT INTO deployment_records SELECT mod_id,'active','' FROM mods")
    assert mm.library_probe_stamp != stamp
    assert mm._index.get_library_rows("game_a")[0].enabled
    assert not before[0].enabled


def test_root_change_invalidates_relative_snapshot_and_probe_stamp(mm, monkeypatch, tmp_path):
    from src.core import mod_index
    from src.utils import paths

    mod = _import_mod(mm, mm.add_character(name="Cache", node_type="character").id, tmp_path, "relative")
    mm._index._db().execute("UPDATE mods SET path='data/relative.zip' WHERE mod_id=?", (mod.mod_id,))
    mm._index._db().commit()
    before = mm._index.get_library_rows("game_a")
    stamp = mm.library_probe_stamp
    moved = tmp_path / "moved-root"
    monkeypatch.setattr(paths, "get_app_dir", lambda: moved)
    monkeypatch.setattr(mod_index, "get_app_dir", lambda: moved)
    monkeypatch.setattr(mod_index, "get_data_dir", lambda: moved / "data")
    mm._index.refresh_paths()
    assert mm.library_probe_stamp != stamp
    after = mm._index.get_library_rows("game_a")
    assert after[0].path == str(mm._index.paths.container_path(mod.mod_id) / "data/relative.zip")
    assert after != before


def test_snapshot_switching_games_never_reuses_other_game(mm, tmp_path):
    _import_mod(mm, mm.add_character(name="Cache", node_type="character").id, tmp_path, "game-one")
    first = mm._index.get_library_rows("game_a")
    assert mm._index.get_library_rows("game_b") == ()
    assert mm._index.get_library_rows("game_a") == first


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "data/角色/../图.png",
        r"C:\Mods\body.zip",
        r"\\server\share\图.png",
        r"\\?\C:\Mods\body.zip",
        r"\rooted\image.png",
        r"C:drive-relative.png",
        "//server/share",
        "//server",
        r"\\server\share",
    ],
)
def test_batch_path_resolution_preserves_stored_path_semantics(mm, tmp_path, stored):
    import json
    import sqlite3

    mod = _import_mod(mm, mm.add_character(name="Paths", node_type="character").id, tmp_path, "paths")
    with pytest.raises(sqlite3.IntegrityError):
        mm._index._db().execute(
            "UPDATE mods SET path=?,preview_images_json=? WHERE mod_id=?",
            (stored, json.dumps([{"scope": "container", "path": stored}]), mod.mod_id),
        )
    mm._index._db().rollback()
    assert mm._index.get_mod(mod.mod_id).path == mod.path


def test_poll_uses_one_snapshot_with_deployment_and_no_full_objects(mm, monkeypatch, tmp_path):
    char = mm.add_character(name="Snapshot", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "deployed")
    mm.set_deploy_dir(str(tmp_path / "Mods"))
    assert mm.toggle_mod(mod.mod_id).is_enabled
    mm.reconcile_library("game_a", trigger="poll")
    reads, objects = [], []
    original_rows = mm._index.get_library_rows
    original_object = mm._index._row_to_mod

    def read_rows(game_id):
        reads.append(game_id)
        return original_rows(game_id)

    def read_object(row, *args):
        objects.append(row["mod_id"])
        return original_object(row, *args)

    monkeypatch.setattr(mm._index, "get_library_rows", read_rows)
    monkeypatch.setattr(mm._index, "_row_to_mod", read_object)
    report = mm.reconcile_library("game_a", trigger="poll")
    assert not report.changed
    assert reads == ["game_a"]
    assert objects == []


def test_permission_error_preserves_record_and_other_rows(mm, monkeypatch, tmp_path):
    from src.utils import fs

    char = mm.add_character(name="Snapshot", node_type="character")
    blocked = _import_mod(mm, char.id, tmp_path, "blocked")
    healthy = _import_mod(mm, char.id, tmp_path, "healthy")
    original = os.stat
    blocked_key = os.path.normcase(os.path.abspath(blocked.path))

    def denied(path, *args, **kwargs):
        if os.path.normcase(os.path.abspath(fs.normal_path(path))) == blocked_key:
            raise PermissionError("probe denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", denied)
    report = mm.reconcile_library("game_a", trigger="poll")
    assert report.removed_sources == 0
    assert mm._index.get_mod(blocked.mod_id) is not None
    assert mm._index.get_mod(healthy.mod_id) is not None


def test_claim_for_nonactive_game_preserves_index_ownership(mm, tmp_path):
    char = mm.add_character(name="Snapshot", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "other_game")
    original = Path(mod.path).parent
    relocated = original.with_name("renamed_container")
    original.rename(relocated)
    mm.set_game("game_b")
    report = mm.reconcile_library("game_a", trigger="poll")
    assert report.claimed == 1
    assert mm._index.get_mod_game_id(mod.mod_id) == "game_a"
    assert mm._index.get_mod(mod.mod_id).path == str(relocated / Path(mod.path).name)


def test_unreadable_relocation_marker_never_authorizes_source_cleanup(mm, monkeypatch, tmp_path):
    from src.core import mod_package

    char = mm.add_character(name="Snapshot", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "unreadable_move")
    original = Path(mod.path).parent
    relocated = original.with_name("relocated")
    original.rename(relocated)
    read_marker = mod_package._read_container_marker_data

    def denied(path):
        if Path(path) == relocated:
            raise mod_package.InvalidManagedMetadataError("marker locked")
        return read_marker(path)

    with monkeypatch.context() as patch:
        patch.setattr(mod_package, "_read_container_marker_data", denied)
        report = mm.reconcile_library("game_a", trigger="poll")
    assert report.removed_sources == 0
    assert mm._index.get_mod(mod.mod_id) is not None
    report = mm.reconcile_library("game_a", trigger="poll")
    assert report.claimed == 1
    assert mm._index.get_mod(mod.mod_id).path == str(relocated / Path(mod.path).name)
