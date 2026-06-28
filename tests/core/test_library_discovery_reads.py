import json
import os
from pathlib import Path

import pytest

from src.utils import fs
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def _track_marker_reads(monkeypatch, marker):
    if os.name == "nt":
        from src.core import watcher_windows as reader

        method = "read_marker_text"
    else:
        reader, method = fs, "read_text"
    original = getattr(reader, method)
    reads = []

    def read(path, *args, **kwargs):
        if Path(path) == marker:
            reads.append(Path(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(reader, method, read)
    return reads


def _replace_archive_with_folder(mod):
    body = Path(mod.path)
    body.unlink()
    body.mkdir()
    (body / "mod.ini").write_text("[Constants]\nx=folder", encoding="utf-8")
    marker_path = body.parent / ".d3dxmod"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["body_type"] = "folder"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    return body


def test_discovery_validates_each_known_container_marker_once(mm, tmp_path, monkeypatch):

    char = mm.add_character(name="Known", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "known")
    marker = Path(mod.path).parent / ".d3dxmod"
    reads = _track_marker_reads(monkeypatch, marker)
    discovered = mm._discover_library_mods("game_a")
    assert [(item["path"], item["node_id"]) for item in discovered] == [(mod.path, char.id)]
    assert len(reads) == 1


def test_discovery_fallback_preserves_nested_unknown_and_invalid_inputs(mm, tmp_path, monkeypatch):
    from src.utils import fs

    parent = mm.add_character(name="Parent", node_type="character")
    child = mm.add_character(name="Child", parent_id=parent.id, node_type="character")
    known = _import_mod(mm, parent.id, tmp_path, "known")
    nested = _import_mod(mm, child.id, tmp_path, "nested")
    broken = _import_mod(mm, parent.id, tmp_path, "broken")
    broken_container = Path(broken.path).parent
    (broken_container / ".d3dxmod").write_text("{broken", encoding="utf-8")
    library = mm._get_library_root("game_a")
    unknown = library / "Unclassified"
    unknown.mkdir()
    loose = unknown / "loose.zip"
    loose.write_bytes(b"archive candidate")
    listings = []
    original = fs.os.scandir

    def list_children(path):
        listings.append(Path(fs.normal_path(path)))
        return original(path)

    monkeypatch.setattr(fs.os, "scandir", list_children)
    invalid = []
    discovered = mm._discover_library_mods("game_a", invalid_metadata=invalid)
    assert {(item["path"], item["node_id"]) for item in discovered} == {
        (known.path, parent.id),
        (nested.path, child.id),
        (str(loose), ""),
    }
    assert invalid == [str(broken_container)]
    assert listings.count(mm._get_character_library(parent.id, "game_a")) == 1
    assert listings.count(mm._get_character_library(child.id, "game_a")) == 1
    assert listings.count(unknown) == 1


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("body_format", ["zip", "folder"])
def test_registered_discovery_avoids_realpath_for_known_bodies(mm, tmp_path, monkeypatch, fallback, body_format):
    from dataclasses import replace

    from src.core import mod_package
    from src.ui.library_discovery import capture_discovery, discover
    from src.utils import fs

    char = mm.add_character(name="Known", node_type="character")
    known = _import_mod(mm, char.id, tmp_path, "known")
    if body_format == "folder":
        _replace_archive_with_folder(known)
    request = capture_discovery(mm, None)
    if fallback:
        request = replace(request, characters=(), locations=())
    resolved, container_stats = [], []
    reads = _track_marker_reads(monkeypatch, Path(known.path).parent / ".d3dxmod")
    original = mod_package.os.path.realpath
    original_stat = mod_package.os.stat

    def realpath(path, *args, **kwargs):
        resolved.append(str(path))
        return original(path, *args, **kwargs)

    def stat(path, *args, **kwargs):
        if not isinstance(path, int) and Path(fs.normal_path(path)) == Path(known.path).parent:
            container_stats.append(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(mod_package.os.path, "realpath", realpath)
    monkeypatch.setattr(mod_package.os, "stat", stat)
    assert discover(request, lambda: False) == ([], [])
    assert resolved == [], f"Resolved registered body again: {known.path}"
    assert len(reads) == 1
    assert container_stats == []


@pytest.mark.parametrize("change", ["repoint", "corrupt", "invalid_hash", "wrong_type", "missing"])
def test_registered_discovery_keeps_changed_marker_and_body_checks(mm, tmp_path, change):
    import shutil

    from src.ui.library_discovery import capture_discovery, discover

    char = mm.add_character(name="Known", node_type="character")
    known = _import_mod(mm, char.id, tmp_path, "known")
    request = capture_discovery(mm, None)
    container = Path(known.path).parent
    marker_path = container / ".d3dxmod"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if change == "repoint":
        replacement = container / "replacement.zip"
        shutil.copy2(known.path, replacement)
        marker["body"] = replacement.name
    elif change == "invalid_hash":
        marker["content_hash"] = "invalid"
    elif change == "wrong_type":
        marker["body_type"] = "folder"
    elif change == "missing":
        Path(known.path).unlink()
    marker_path.write_text("{broken" if change == "corrupt" else json.dumps(marker), encoding="utf-8")

    descriptors, invalid = discover(request, lambda: False)
    if change == "repoint":
        assert [item["path"] for item in descriptors] == [str(replacement)]
        assert invalid == []
    else:
        assert descriptors == []
        assert invalid == [str(container)]


def test_registered_discovery_full_scan_keeps_unknown_folder_and_archive(mm, tmp_path):
    from src.ui.library_discovery import capture_discovery, discover

    char = mm.add_character(name="Known", node_type="character")
    _import_mod(mm, char.id, tmp_path, "known")
    root = mm._get_character_library(char.id, "game_a")
    folder, archive = root / "Incoming", root / "incoming.zip"
    folder.mkdir()
    archive.write_bytes(b"pending archive")
    descriptors, invalid = discover(capture_discovery(mm, None), lambda: False)
    assert {item["path"] for item in descriptors} == {str(folder), str(archive)}
    assert invalid == []


def test_registered_discovery_rejects_body_replaced_with_escaping_link(mm, tmp_path):
    from src.ui.library_discovery import capture_discovery, discover
    from tests.core.test_mod_package import _link_directory

    char = mm.add_character(name="Known", node_type="character")
    known = _import_mod(mm, char.id, tmp_path, "known")
    request = capture_discovery(mm, None)
    body = _replace_archive_with_folder(known)
    outside = tmp_path / "outside"
    body.rename(outside)
    _link_directory(body, outside)
    try:
        assert discover(request, lambda: False) == ([], [str(body.parent)])
    finally:
        if body.is_symlink():
            body.unlink()
        else:
            body.rmdir()


def test_discovery_reuses_readable_unchanged_inventory_marker(mm, tmp_path, monkeypatch):
    from dataclasses import replace

    from src.core.library_inventory import scan_source_inventory
    from src.core.library_snapshot import ProbeToken
    from src.ui.library_discovery import capture_discovery, discover

    char = mm.add_character(name="Cached", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "cached")
    inventory = scan_source_inventory(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)))
    request = replace(capture_discovery(mm, None), source_inventory=inventory)
    assert not request.audit_owns_known_checks
    reads = _track_marker_reads(monkeypatch, Path(mod.path).parent / ".d3dxmod")
    assert discover(request, lambda: False) == ([], [])
    assert reads == []
    marker = Path(mod.path).parent / ".d3dxmod"
    marker.write_text("{corrupt", encoding="utf-8")
    assert discover(request, lambda: False) == ([], [str(marker.parent)])
    assert reads == [marker]


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("body_format", ["zip", "folder"])
def test_audit_owned_discovery_skips_known_checks_but_finds_new_inputs(
    mm, tmp_path, monkeypatch, fallback, body_format
):
    import shutil
    from dataclasses import replace

    from src.core import library_inventory, mod_package
    from src.core.library_snapshot import ProbeToken
    from src.ui.library_discovery import capture_discovery, discover
    from src.utils import fs

    char = mm.add_character(name="Audit", node_type="character")
    known = _import_mod(mm, char.id, tmp_path, "known")
    if body_format == "folder":
        _replace_archive_with_folder(known)
    body = Path(known.path)
    container = body.parent
    inventory = library_inventory.scan_source_inventory(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)))
    new_container = container.with_name("NewManaged")
    shutil.copytree(container, new_container)
    folder, archive = container.parent / "Incoming", container.parent / "incoming.zip"
    folder.mkdir()
    (folder / "mod.ini").write_text("[Constants]\nx=new", encoding="utf8")
    archive.write_bytes(b"pending archive")
    request = capture_discovery(mm, None, source_inventory=inventory, audit_owns_known_checks=True)
    if fallback:
        request = replace(request, characters=(), locations=())
    known_checks, raw_reads = [], []
    original_version = library_inventory.marker_version
    original_read = mod_package._read_container_marker_data
    original_stat = mod_package.os.stat

    def version(path):
        if Path(path) == container:
            known_checks.append("version")
        return original_version(path)

    def read(path):
        raw_reads.append(Path(path))
        if Path(path) == container:
            known_checks.append("marker")
        return original_read(path)

    def stat(path, *args, **kwargs):
        if not isinstance(path, int) and Path(fs.normal_path(path)) == body:
            known_checks.append("body")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(library_inventory, "marker_version", version)
    monkeypatch.setattr(mod_package, "_read_container_marker_data", read)
    monkeypatch.setattr(mod_package.os, "stat", stat)
    descriptors, invalid = discover(request, lambda: False)
    assert known_checks == []
    assert {item["path"] for item in descriptors} == {
        str(new_container / body.name),
        str(folder),
        str(archive),
    }
    assert new_container in raw_reads and folder in raw_reads
    assert invalid == []


def test_audit_owned_discovery_requires_same_root_inventory(mm, tmp_path):
    from dataclasses import replace

    from src.core.library_inventory import scan_source_inventory
    from src.core.library_snapshot import ProbeToken
    from src.ui.library_discovery import capture_discovery

    inventory = scan_source_inventory(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)))
    for previous in (None, replace(inventory, root=str(tmp_path / "other-library"))):
        request = capture_discovery(mm, None, source_inventory=previous, audit_owns_known_checks=True)
        assert not request.audit_owns_known_checks


@pytest.mark.parametrize("kind", ["folder", "archive"])
def test_discovery_waits_for_new_input_version_to_settle(mm, monkeypatch, kind):
    from types import SimpleNamespace

    from src.ui import library_discovery as module

    char = mm.add_character(name="Copying", node_type="character")
    root = mm._get_character_library(char.id, "game_a")
    source = root / ("Incoming" if kind == "folder" else "incoming.zip")
    if kind == "folder":
        source.mkdir()
        payload = source / "mod.ini"
    else:
        payload = source
    payload.write_bytes(b"first")
    request = module.capture_discovery(mm, {str(source)})
    elapsed, changed = [0.0], [False]

    def sleep(seconds):
        elapsed[0] += seconds
        if elapsed[0] >= 0.25 and not changed[0]:
            payload.write_bytes(b"completed copy")
            changed[0] = True

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: elapsed[0], sleep=sleep))
    descriptors, invalid = module.discover(request, lambda: False)
    assert elapsed[0] >= 0.5
    assert descriptors[0].get("_deferred"), "a changing input must wait for a fresh quiet window"
    descriptors, invalid = module.discover(request, lambda: False)
    assert elapsed[0] >= 1.0
    assert not descriptors[0].get("_deferred")
    assert [item["path"] for item in descriptors] == [str(source)]
    assert invalid == []
    assert payload.read_bytes() == b"completed copy"


def test_discovery_settling_is_cancellable(mm, monkeypatch):
    from types import SimpleNamespace

    from src.ui import library_discovery as module

    char = mm.add_character(name="Copying", node_type="character")
    source = mm._get_character_library(char.id, "game_a") / "incoming.zip"
    source.write_bytes(b"first")
    request = module.capture_discovery(mm, {str(source)})
    elapsed = [0.0]
    monkeypatch.setattr(
        module,
        "time",
        SimpleNamespace(
            monotonic=lambda: elapsed[0], sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds)
        ),
    )
    with pytest.raises(InterruptedError):
        module.discover(request, lambda: elapsed[0] >= 0.15)
    assert elapsed[0] <= 0.2


@pytest.mark.parametrize("scope_kind", ["container", "parent", "marker", "manual"])
def test_discovery_rechecks_repointed_marker_despite_matching_version(mm, tmp_path, monkeypatch, scope_kind):
    from dataclasses import replace

    from src.core import library_inventory
    from src.core.library_snapshot import ProbeToken
    from src.ui.library_discovery import capture_discovery, discover

    char = mm.add_character(name="Cached", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "cached")
    body = Path(mod.path)
    container = body.parent
    marker = container / ".d3dxmod"
    inventory = library_inventory.scan_source_inventory(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)))
    original_version = next(entry.version for entry in inventory.entries if entry.container == str(container))
    replacement = body.with_name("alt_cached.zip")
    replacement.write_bytes(body.read_bytes())
    # 模拟 Windows 同尺寸写入并还原修改时间，验证时间和大小不能单独证明标记未变化。
    marker.write_text(marker.read_text(encoding="utf-8").replace(body.name, replacement.name), encoding="utf-8")
    monkeypatch.setattr(library_inventory, "marker_version", lambda path: original_version)
    scopes = {"container": container, "parent": container.parent, "marker": marker}
    scope = {str(scopes[scope_kind])} if scope_kind != "manual" else None
    request = replace(capture_discovery(mm, scope), source_inventory=inventory)
    if scope_kind == "manual":
        request = replace(request, verify_markers=True)
    descriptors, invalid = discover(request, lambda: False)
    if scope_kind in {"container", "marker"}:
        from src.core.watcher import FileChange

        result = mm.apply_local_source_events([FileChange(str(container.parent.parent), 1, "modified", str(marker))])
        assert result.changed_ids == {mod.mod_id}
        assert mm.get_mod(mod.mod_id).path == str(replacement)
        assert descriptors == []
        return
    assert [item["path"] for item in descriptors] == [str(replacement)]
    assert invalid == []


def test_new_input_snapshot_cancels_while_enumerating_tree(tmp_path, monkeypatch):
    from src.ui.library_discovery import _wait_for_stable_inputs
    from src.utils import fs

    folder = tmp_path / "copying"
    folder.mkdir()
    for i in range(50):
        (folder / f"{i:03}.ini").write_text("[Constants]", encoding="utf-8")
    original = fs.os.scandir
    enumerated = []

    class Entries:
        def __init__(self, path):
            self.entries = original(path)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.entries.close()

        def __iter__(self):
            return self

        def __next__(self):
            item = next(self.entries)
            enumerated.append(item.name)
            return item

    def check():
        if len(enumerated) >= 3:
            raise InterruptedError("switched game")

    monkeypatch.setattr(fs.os, "scandir", Entries)
    with pytest.raises(InterruptedError):
        _wait_for_stable_inputs([{"path": str(folder)}], check)
    assert len(enumerated) == 3


def test_cancellable_input_snapshot_matches_existing_file_selection(tmp_path):
    from src.core.preview_cache_index import capture_source_version

    (tmp_path / "z.ini").write_text("last", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "a.ini").write_text("first", encoding="utf-8")
    (nested / ".d3dxmod").write_text("ignored marker", encoding="utf-8")
    metadata = tmp_path / ".d3dxmeta"
    metadata.mkdir()
    (metadata / "ignored.txt").write_text("metadata", encoding="utf-8")
    expected = capture_source_version(tmp_path)
    actual = capture_source_version(tmp_path, checkpoint=lambda: None)
    assert actual == expected
    assert [entry[0] for entry in actual] == ["nested/a.ini", "z.ini"]


def test_new_input_snapshot_checks_cancel_before_each_file_stat(tmp_path, monkeypatch):
    from src.core.preview_cache_index import capture_source_version

    for i in range(20):
        (tmp_path / f"{i:03}.ini").write_text("payload", encoding="utf-8")
    original = Path.stat
    inspected = []

    def stat(path, *args, **kwargs):
        if Path(fs.normal_path(path)).parent == tmp_path:
            inspected.append(path)
        return original(path, *args, **kwargs)

    def check():
        if len(inspected) >= 3:
            raise InterruptedError("closing")

    monkeypatch.setattr(Path, "stat", stat)
    with pytest.raises(InterruptedError):
        capture_source_version(tmp_path, checkpoint=check)
    assert len(inspected) == 3
