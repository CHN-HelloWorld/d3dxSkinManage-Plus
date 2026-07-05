import json
import sqlite3
from pathlib import Path

import pytest

from src.core.watcher import FileChange
from src.utils import fs
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def setup_rename(mm, tmp_path, custom=False):
    mod = _import_mod(mm, mm.add_character("Names").id, tmp_path, "default")
    mod.name = "Custom title" if custom else mod.folder_name
    assert mm.update_mod(mod)
    source = mm._index.paths.container_path(mod.mod_id)
    target = source.with_name("Renamed")
    sidecar = source / ".d3dxmeta" / "info.json"
    before = sidecar.read_bytes()
    source.rename(target)
    event = FileChange(str(mm._index.paths.roots["game_a"]), 1, "moved", str(source), str(target), True)
    return mod, source, target, target / ".d3dxmeta" / "info.json", before, event


@pytest.mark.parametrize("custom", [False, True])
def test_external_rename_survives_rebuild_without_changing_custom_name(mm, tmp_path, custom):
    mod, source, target, sidecar, before, event = setup_rename(mm, tmp_path, custom)
    result = mm.apply_local_source_events([event])
    expected = "Custom title" if custom else "Renamed"
    assert result.changed_ids == {mod.mod_id}
    assert mm.get_mod(mod.mod_id).name == expected
    assert json.loads(sidecar.read_bytes())["name"] == expected
    if custom:
        assert sidecar.read_bytes() == before
    mm.rebuild_index_from_filesystem("game_a")
    rebuilt = mm.get_mod(mod.mod_id)
    assert rebuilt.name == expected and rebuilt.folder_name == "Renamed"
    assert rebuilt.imported_at == mod.imported_at


def test_external_sidecar_name_wins_over_stale_default_index(mm, tmp_path):
    mod, source, target, sidecar, before, event = setup_rename(mm, tmp_path)
    data = json.loads(before)
    data["name"] = "Edited outside"
    sidecar.write_text(json.dumps(data), encoding="utf-8")
    external = sidecar.read_bytes()
    assert mm.apply_local_source_events([event]).changed_ids == {mod.mod_id}
    assert sidecar.read_bytes() == external
    assert mm.get_mod(mod.mod_id).name == "Edited outside"


def test_sidecar_write_failure_rolls_back_sql_and_keeps_original_bytes(mm, tmp_path, monkeypatch):
    from src.core import library_events

    mod, source, target, sidecar, before, event = setup_rename(mm, tmp_path)
    replace = library_events.os.replace

    def denied(src, dst):
        if Path(fs.normal_path(dst)) == sidecar:
            raise PermissionError("sidecar locked")
        return replace(src, dst)

    monkeypatch.setattr(library_events.os, "replace", denied)
    result = mm.apply_local_source_events([event])
    assert not result.changed_ids and result.review_paths
    assert sidecar.read_bytes() == before
    assert mm.get_mod(mod.mod_id).folder_name == source.name


def test_commit_failure_restores_sidecar_and_index(mm, tmp_path):
    mod, source, target, sidecar, before, event = setup_rename(mm, tmp_path)
    conn = mm._index.paths.connection

    class FailingCommit:
        def __getattr__(self, name):
            return getattr(conn, name)

        def commit(self):
            raise sqlite3.OperationalError("commit failed")

    mm._index.paths.connection = FailingCommit()
    result = mm.apply_local_source_events([event])
    assert not result.changed_ids and result.review_paths
    assert sidecar.read_bytes() == before
    assert mm.get_mod(mod.mod_id).folder_name == source.name


def test_sidecar_version_change_before_publish_is_not_overwritten(mm, tmp_path, monkeypatch):
    from src.core import library_events

    mod, source, target, sidecar, before, event = setup_rename(mm, tmp_path)
    replace = library_events._replace_sidecar
    data = json.loads(before)
    data["name"] = "Concurrent edit"
    external = json.dumps(data).encode("utf-8")

    def concurrent(path, expected, payload):
        sidecar.write_bytes(external)
        return replace(path, expected, payload)

    monkeypatch.setattr(library_events, "_replace_sidecar", concurrent)
    result = mm.apply_local_source_events([event])
    assert not result.changed_ids and result.review_paths
    assert sidecar.read_bytes() == external
    assert mm.get_mod(mod.mod_id).folder_name == source.name


def test_rollback_does_not_replace_an_external_edit_after_publication(mm, tmp_path):
    mod, source, target, sidecar, before, event = setup_rename(mm, tmp_path)
    conn = mm._index.paths.connection
    data = json.loads(before)
    data["name"] = "Edited during failed commit"
    external = json.dumps(data).encode("utf-8")

    class FailingCommit:
        def __getattr__(self, name):
            return getattr(conn, name)

        def commit(self):
            sidecar.write_bytes(external)
            raise sqlite3.OperationalError("commit failed after external edit")

    mm._index.paths.connection = FailingCommit()
    result = mm.apply_local_source_events([event])
    assert not result.changed_ids and result.review_paths
    assert sidecar.read_bytes() == external
    assert mm.get_mod(mod.mod_id).folder_name == source.name
