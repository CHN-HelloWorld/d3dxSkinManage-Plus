from dataclasses import replace
from pathlib import Path

from src.core.library_snapshot import LibraryRow, ProbeToken
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm
from tests.helpers import registered_games as registered_games


def test_protected_names_are_immutable_and_reuse_unchanged_snapshot(tmp_path, monkeypatch):
    from src.core import library_snapshot as module

    root = str(tmp_path / "cache")
    rows = (LibraryRow("owner", "source", "", False, (str(Path(root) / "shared" / "preview.png"),)),)
    first = module.protected_cache_names(rows, root)
    assert isinstance(first, frozenset)
    assert first == {"owner", "shared"}

    def repeated_path_work(*args):
        raise AssertionError("unchanged cache references must not be recomputed")

    monkeypatch.setattr(module.os.path, "relpath", repeated_path_work)
    assert module.protected_cache_names(rows, root) == first


def test_one_changed_row_recomputes_only_its_preview_projection(tmp_path, monkeypatch):
    from src.core import library_snapshot as module

    root = str(tmp_path / "cache")
    rows = tuple(LibraryRow(str(i), "body", "", False, (str(Path(root) / f"ref{i}" / "p.png"),)) for i in range(5000))
    module.protected_cache_names(rows, root)
    calls = []
    original = module.os.path.relpath

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(module.os.path, "relpath", counted)
    changed = list(rows)
    changed[42] = replace(rows[42], preview_images=(str(Path(root) / "changed" / "p.png"),))
    changed[43] = replace(rows[43], path="new-body", enabled=True)
    result = module.protected_cache_names(changed, root)
    assert len(calls) == 1
    assert "changed" in result and "ref42" not in result


def test_collection_checkpoints_remain_active_for_all_cached_rows(tmp_path):
    import pytest

    from src.core import library_snapshot as module

    rows = tuple(LibraryRow(str(i), "", "", False, ()) for i in range(100))
    module.protected_cache_names(rows, str(tmp_path))
    checkpoints = []

    def cancel():
        checkpoints.append(True)
        return len(checkpoints) == 7

    with pytest.raises(InterruptedError):
        module.protected_cache_names(rows, str(tmp_path), should_cancel=cancel)
    assert len(checkpoints) == 7


def test_name_projection_preserves_prefix_case_parent_and_cross_drive_rules(tmp_path, monkeypatch):
    import os

    from src.core.library_snapshot import protected_cache_names

    root = tmp_path / "cache"
    references = [
        str(root / "Shared" / "p.png"),
        str(tmp_path / "cache-other" / "bad.png"),
        str(root / ".." / "outside" / "bad.png"),
        str(root),
    ]
    if os.name == "nt":
        other_drive = "Z:" if root.drive.upper() != "Z:" else "Y:"
        references += [other_drive + "\\foreign\\bad.png"]
    row = LibraryRow("Owner", "", "", False, tuple(references))
    monkeypatch.chdir(tmp_path)
    assert protected_cache_names((row,), "cache") == {os.path.normcase("Owner"), os.path.normcase("Shared")}
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert protected_cache_names((row,), "cache") == {os.path.normcase("Owner")}


def test_protected_names_refresh_for_new_rows_preview_paths_and_root(tmp_path):
    from src.core.library_snapshot import protected_cache_names

    root = tmp_path / "cache"
    row = LibraryRow("owner", "source", "", False, (str(root / "shared" / "preview.png"),))
    first = protected_cache_names((row,), str(root))
    changed = replace(row, mod_id="new-owner", preview_images=(str(root / "new-reference" / "p.png"),))
    assert protected_cache_names((changed,), str(root)) == {"new-owner", "new-reference"}
    assert protected_cache_names((row,), str(tmp_path / "different-root")) == {"owner"}
    assert first == {"owner", "shared"}


def test_protected_names_refresh_when_relative_image_base_changes(tmp_path, monkeypatch):
    from src.core.library_snapshot import protected_cache_names

    root = tmp_path / "cache"
    root.mkdir()
    rows = (LibraryRow("owner", "source", "", False, ("cache/shared/p.png",)),)
    monkeypatch.chdir(tmp_path)
    assert protected_cache_names(rows, str(root)) == {"owner", "shared"}
    monkeypatch.chdir(root)
    assert protected_cache_names(rows, str(root)) == {"owner", "cache"}


def test_protection_cache_releases_old_snapshots(tmp_path):
    import gc
    import weakref

    from src.core.library_snapshot import protected_cache_names

    snapshots = []
    for ordinal in range(20):
        row = LibraryRow(str(ordinal), "source", "", False, ())
        snapshots.append(weakref.ref(row))
        protected_cache_names((row,), str(tmp_path / "bounded-cache"))
    gc.collect()
    assert sum(reference() is not None for reference in snapshots) <= 4


def test_probe_does_not_materialize_paths_for_protected_cache_entries(mm, tmp_path, monkeypatch):
    from src.core import library_snapshot as module

    char = mm.add_character(name="Protected", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "protected")
    request = mm.create_library_probe_request(ProbeToken("game_a", 1, 1))
    protected = Path(request.cache_root) / mod.mod_id
    protected.mkdir(parents=True, exist_ok=True)
    (protected / "preview.png").write_bytes(b"preview")
    listing = module.fs.list_children

    def list_children(path):
        assert Path(path) != Path(request.cache_root), "filter scandir names before building Paths"
        return listing(path)

    monkeypatch.setattr(module.fs, "list_children", list_children)
    result = module.probe_library(request)
    assert not result.error
    assert result.cache_candidates == ()
    assert protected.exists()
