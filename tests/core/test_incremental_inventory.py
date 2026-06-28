import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def request(mm, **kw):
    from src.core.library_snapshot import ProbeToken

    return replace(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)), **kw)


def established(mm):
    from src.core.library_snapshot import probe_library

    result = probe_library(request(mm, mode="full"))
    assert not result.error
    assert result.source_inventory is not None
    list(mm.apply_library_probe_result(result))
    return result.source_inventory


def test_source_event_reuses_identity_but_checks_missing_and_destination(mm, tmp_path, monkeypatch):
    from threading import get_ident

    from src.core import library_inventory, mod_package
    from src.core.library_snapshot import probe_library

    char = mm.add_character(name="A", node_type="character")
    mods = [_import_mod(mm, char.id, tmp_path, f"mod{i}") for i in range(4)]
    inventory = established(mm)
    old = Path(mods[0].path).parent
    new = old.with_name("Renamed")
    old.rename(new)
    reads, read_threads, version_threads = [], [], []
    caller = get_ident()
    original = mod_package._read_container_marker_data
    original_version = library_inventory.marker_version

    def read(path):
        reads.append(Path(path))
        read_threads.append(get_ident())
        return original(path)

    def version(path):
        version_threads.append(get_ident())
        return original_version(path)

    monkeypatch.setattr(mod_package, "_read_container_marker_data", read)
    monkeypatch.setattr(library_inventory, "marker_version", version)
    result = probe_library(request(mm, mode="source", source_inventory=inventory, changed_paths=(str(old), str(new))))
    assert not result.error
    assert set(reads) == {new}
    assert read_threads == [caller]
    assert version_threads and set(version_threads) == {caller}
    assert result.checks[0].state == "missing"
    assert all(check.state == "unchecked" for check in result.checks[1:])
    list(mm.apply_library_probe_result(result))
    assert mm.get_mod(mods[0].mod_id).path == str(new / Path(mods[0].path).name)


def test_unreported_duplicate_in_other_category_blocks_local_claim(mm, tmp_path):
    from src.core.library_snapshot import probe_library

    a = mm.add_character(name="A", node_type="character")
    b = mm.add_character(name="B", node_type="character")
    mod = _import_mod(mm, a.id, tmp_path, "source")
    inventory = established(mm)
    old = Path(mod.path).parent
    new = old.with_name("Renamed")
    old.rename(new)
    shutil.copytree(new, mm._get_character_library(b.id) / "Duplicate")
    result = probe_library(request(mm, mode="source", source_inventory=inventory, changed_paths=(str(old), str(new))))
    assert "Duplicate source identity" in result.error
    assert list(mm.apply_library_probe_result(result)) == []
    assert mm.get_mod(mod.mod_id).path == mod.path


def test_unreported_marker_repoint_is_reloaded_even_outside_event_paths(mm, tmp_path):
    from src.core.library_snapshot import probe_library

    char = mm.add_character(name="A", node_type="character")
    first = _import_mod(mm, char.id, tmp_path, "first")
    second = _import_mod(mm, char.id, tmp_path, "second")
    inventory = established(mm)
    first_marker = Path(first.path).parent / ".d3dxmod"
    data = json.loads(first_marker.read_text(encoding="utf8"))
    other_marker = Path(second.path).parent / ".d3dxmod"
    other = json.loads(other_marker.read_text(encoding="utf8"))
    other["mod_id"] = data["mod_id"]
    other_marker.write_text(json.dumps(other), encoding="utf8")
    result = probe_library(request(mm, mode="source", source_inventory=inventory, changed_paths=(first.path,)))
    assert "Duplicate source identity" in result.error


def test_marker_permission_error_never_authorizes_missing_source_cleanup(mm, tmp_path, monkeypatch):
    from src.core import library_inventory
    from src.core.library_snapshot import probe_library

    char = mm.add_character(name="A", node_type="character")
    missing = _import_mod(mm, char.id, tmp_path, "missing")
    occupied = _import_mod(mm, char.id, tmp_path, "occupied")
    inventory = established(mm)
    shutil.rmtree(Path(missing.path).parent)
    original = library_inventory.marker_version

    def denied(path):
        if Path(path) == Path(occupied.path).parent:
            raise PermissionError("unreadable identity")
        return original(path)

    monkeypatch.setattr(library_inventory, "marker_version", denied)
    result = probe_library(request(mm, mode="source", source_inventory=inventory, changed_paths=(missing.path,)))
    assert result.error
    assert list(mm.apply_library_probe_result(result)) == []
    assert mm.get_mod(missing.mod_id)


def test_deployment_event_does_not_wait_for_unrelated_source_io(mm, tmp_path, monkeypatch):
    from src.core import library_snapshot

    char = mm.add_character(name="A", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "source")
    mm.set_deploy_dir(str(tmp_path / "deploy"))
    assert mm.toggle_mod(mod.mod_id)
    mod = mm.get_mod(mod.mod_id)
    old = Path(mod.deployed_path)
    new = old.with_name("Renamed")
    old.rename(new)
    original = library_snapshot.check_path

    def check(path):
        assert path != mod.path, "deployment events must not stat unrelated source bodies"
        return original(path)

    monkeypatch.setattr(library_snapshot, "check_path", check)
    result = library_snapshot.probe_library(request(mm, mode="deployment"))
    assert not result.error
    list(mm.apply_library_probe_result(result))
    assert mm.get_mod(mod.mod_id).deployed_path == str(new)


@pytest.mark.parametrize("cancel,remove_root", [(True, False), (False, True)])
def test_cancel_or_offline_root_keeps_inventory_uncommitted(mm, tmp_path, cancel, remove_root):
    from src.core.library_snapshot import probe_library

    char = mm.add_character(name="A", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "source")
    inventory = established(mm)
    root = mm._get_library_root("game_a")
    if remove_root:
        root.rename(root.with_name("offline"))
    result = probe_library(
        request(mm, mode="source", source_inventory=inventory, changed_paths=(mod.path,)), should_cancel=lambda: cancel
    )
    assert result.error or result.cancelled
    assert list(mm.apply_library_probe_result(result)) == []
    assert mm.get_mod(mod.mod_id)


def test_explicit_source_audit_preserves_missing_row_when_same_version_identity_moved(mm, tmp_path, monkeypatch):
    from src.core import library_inventory
    from src.core.library_snapshot import probe_library

    char = mm.add_character(name="A", node_type="character")
    missing = _import_mod(mm, char.id, tmp_path, "missing")
    occupied = _import_mod(mm, char.id, tmp_path, "occupied")
    inventory = established(mm)
    occupied_container = Path(occupied.path).parent
    old_version = next(e.version for e in inventory.entries if e.container == str(occupied_container))
    shutil.rmtree(Path(missing.path).parent)
    marker = occupied_container / ".d3dxmod"
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["mod_id"] = missing.mod_id
    marker.write_text(json.dumps(data), encoding="utf-8")
    original = library_inventory.marker_version
    monkeypatch.setattr(
        library_inventory,
        "marker_version",
        lambda path: old_version if Path(path) == occupied_container else original(path),
    )

    result = probe_library(request(mm, mode="full", source_inventory=inventory, force_source_audit=True))
    assert not result.error
    assert any(item.mod_id == missing.mod_id and item.body == occupied.path for item in result.relocated)
    list(mm.apply_library_probe_result(result))
    assert mm.get_mod(missing.mod_id) is not None
    assert mm.get_mod(occupied.mod_id).path == occupied.path


@pytest.mark.parametrize("audit", ["initial", "forced", "expired"])
def test_complete_inventory_overlaps_only_bounded_identity_reads(mm, tmp_path, monkeypatch, audit):
    import time
    from threading import Lock, get_ident

    from src.core import library_inventory, mod_package
    from src.core.library_snapshot import probe_library

    char = mm.add_character(name="Concurrent", node_type="character")
    mods = [_import_mod(mm, char.id, tmp_path, f"parallel{i}") for i in range(8)]
    previous = None if audit == "initial" else established(mm)
    if audit == "expired":
        previous = replace(previous, audited_at=time.monotonic() - 31)
    original_read, original_check = mod_package._read_container_marker_data, library_inventory.check_path
    active = peak = 0
    lock = Lock()
    checks = []
    paths = {mod.path for mod in mods}
    caller = get_ident()

    def read(path):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(active, peak)
        try:
            time.sleep(0.015)
            return original_read(path)
        finally:
            with lock:
                active -= 1

    def check(path):
        if path in paths:
            checks.append(get_ident())
        return original_check(path)

    monkeypatch.setattr(mod_package, "_read_container_marker_data", read)
    monkeypatch.setattr(library_inventory, "check_path", check)
    result = probe_library(request(mm, mode="full", source_inventory=previous, force_source_audit=audit == "forced"))
    assert not result.error
    assert 1 < peak <= 4
    assert active == 0
    assert checks == [caller] * len(mods)
    assert {entry.source.body for entry in result.source_inventory.entries if entry.source} == paths
