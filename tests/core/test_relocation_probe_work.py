import json
import shutil
from pathlib import Path

from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm
from tests.helpers import registered_games as registered_games


def test_relocation_only_fully_validates_unoccupied_or_changed_markers(mm, tmp_path, monkeypatch):
    from dataclasses import replace

    from src.core import mod_package
    from src.core.library_snapshot import ProbeToken, probe_library

    char = mm.add_character(name="Relocation", node_type="character")
    occupied = _import_mod(mm, char.id, tmp_path, "occupied")
    missing = _import_mod(mm, char.id, tmp_path, "missing")
    baseline = probe_library(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)))
    assert not baseline.error
    old = Path(missing.path).parent
    moved = old.with_name("Moved")
    old.rename(moved)
    validated = []
    original = mod_package._validate_container_marker

    def validate(container, data):
        validated.append(Path(container))
        return original(container, data)

    monkeypatch.setattr(mod_package, "_validate_container_marker", validate)
    request = mm.create_library_probe_request(ProbeToken("game_a", 1, 2))
    result = probe_library(replace(request, source_inventory=baseline.source_inventory))
    assert not result.error
    relocated = {candidate.mod_id: candidate.body for candidate in result.relocated}
    assert relocated[missing.mod_id] == str(moved / Path(missing.path).name)
    assert relocated[occupied.mod_id] == occupied.path
    assert Path(occupied.path).parent not in validated
    assert moved in validated


def test_occupied_container_repointed_to_missing_identity_is_not_pruned(mm, tmp_path):
    from src.core.library_snapshot import ProbeToken, probe_library

    char = mm.add_character(name="Repointed", node_type="character")
    occupied = _import_mod(mm, char.id, tmp_path, "occupied")
    missing = _import_mod(mm, char.id, tmp_path, "missing")
    old = Path(missing.path).parent
    destination = Path(occupied.path).parent
    moved_body = destination / ("relocated-body" + Path(missing.path).suffix)
    marker = json.loads((old / ".d3dxmod").read_text(encoding="utf-8"))
    Path(missing.path).rename(moved_body)
    shutil.rmtree(old)
    marker["body"] = moved_body.name
    (destination / ".d3dxmod").write_text(json.dumps(marker), encoding="utf-8")
    result = probe_library(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)))
    assert not result.error
    assert result.relocated[0].body == str(moved_body)
    list(mm.apply_library_probe_result(result))
    assert mm.get_mod(missing.mod_id).path == missing.path
    assert mm.get_mod(occupied.mod_id).path == occupied.path


def test_unreadable_occupied_marker_cannot_authorize_unrelated_deletion(mm, tmp_path):
    from src.core.library_snapshot import ProbeToken, probe_library

    char = mm.add_character(name="Invalid", node_type="character")
    occupied = _import_mod(mm, char.id, tmp_path, "occupied")
    missing = _import_mod(mm, char.id, tmp_path, "missing")
    shutil.rmtree(Path(missing.path).parent)
    (Path(occupied.path).parent / ".d3dxmod").write_text("{broken", encoding="utf-8")
    result = probe_library(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)))
    assert result.error
    assert list(mm.apply_library_probe_result(result)) == []
    assert mm.get_mod(missing.mod_id) is not None


def test_relocation_uses_directory_entry_type_without_restating_occupied_container(mm, tmp_path, monkeypatch):
    import os

    from src.core.library_snapshot import ProbeToken, path_key, probe_library
    from src.utils import fs

    char = mm.add_character(name="DirectoryEntries", node_type="character")
    occupied = _import_mod(mm, char.id, tmp_path, "occupied")
    missing = _import_mod(mm, char.id, tmp_path, "missing")
    shutil.rmtree(Path(missing.path).parent)
    container = path_key(str(Path(occupied.path).parent))
    original = os.stat

    def stat(path, *args, **kwargs):
        assert path_key(fs.normal_path(path)) != container, "scandir already reports directory type"
        return original(path, *args, **kwargs)

    request = mm.create_library_probe_request(ProbeToken("game_a", 1, 1))
    monkeypatch.setattr(os, "stat", stat)
    result = probe_library(request)
    assert not result.error


def test_relocation_identity_reads_are_bounded_and_can_overlap(mm, tmp_path, monkeypatch):
    import time
    from threading import Lock

    from src.core import mod_package
    from src.core.library_snapshot import ProbeToken, probe_library

    char = mm.add_character(name="ConcurrentReads", node_type="character")
    mods = [_import_mod(mm, char.id, tmp_path, f"concurrent{i}") for i in range(9)]
    shutil.rmtree(Path(mods[-1].path).parent)
    original = mod_package._read_container_marker_data
    lock = Lock()
    active = peak = 0

    def read(container):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(active, peak)
        try:
            time.sleep(0.01)
            return original(container)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(mod_package, "_read_container_marker_data", read)
    result = probe_library(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)))
    assert not result.error
    assert 1 < peak <= 4


def test_cancelled_relocation_joins_identity_readers_before_returning(mm, tmp_path, monkeypatch):
    import time
    from threading import Event, Lock

    from src.core import mod_package
    from src.core.library_snapshot import ProbeToken, probe_library

    char = mm.add_character(name="CancelledReads", node_type="character")
    mods = [_import_mod(mm, char.id, tmp_path, f"cancel{i}") for i in range(6)]
    shutil.rmtree(Path(mods[-1].path).parent)
    cancelled, lock = Event(), Lock()
    original = mod_package._read_container_marker_data
    active = 0

    def read(container):
        nonlocal active
        with lock:
            active += 1
        try:
            cancelled.set()
            time.sleep(0.01)
            return original(container)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(mod_package, "_read_container_marker_data", read)
    result = probe_library(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)), should_cancel=cancelled.is_set)
    assert result.cancelled and not result.error
    assert active == 0
    assert mm.get_mod(mods[-1].mod_id) is not None


def test_relocation_checks_other_categories_for_duplicate_missing_identity(mm, tmp_path):
    from src.core.library_snapshot import ProbeToken, probe_library

    first = mm.add_character(name="FirstBranch", node_type="character")
    second = mm.add_character(name="SecondBranch", node_type="character")
    mod = _import_mod(mm, first.id, tmp_path, "duplicate")
    _import_mod(mm, second.id, tmp_path, "occupied")
    old = Path(mod.path).parent
    moved = old.with_name("Renamed")
    old.rename(moved)
    shutil.copytree(moved, mm._get_character_library(second.id) / "DuplicateIdentity")
    result = probe_library(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)))
    assert "Duplicate source identity" in result.error
    assert list(mm.apply_library_probe_result(result)) == []
    assert mm.get_mod(mod.mod_id).path == mod.path
