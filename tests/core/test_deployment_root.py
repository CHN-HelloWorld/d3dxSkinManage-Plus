import os
import shutil
from pathlib import Path

import pytest

from src.core.deployment_root import DeploymentRootTracker


@pytest.fixture
def tracker():
    instance = DeploymentRootTracker()
    yield instance
    instance.close()


def assert_location(result, status, path, previous):
    assert result.status == status
    assert result.path == (str(path) if path else "")
    assert result.previous_path == str(previous)


def test_unset_missing_file_and_existing_directory(tracker, tmp_path):
    assert_location(tracker.resolve("unset", ""), "unset", "", "")
    missing = tmp_path / "absent"
    assert_location(tracker.resolve("missing", str(missing)), "missing", "", missing)
    file = tmp_path / "file"
    file.write_text("not a directory")
    assert_location(tracker.resolve("file", str(file)), "missing", "", file)
    directory = tmp_path / "Mods"
    directory.mkdir()
    assert_location(tracker.resolve("current", str(directory)), "current", directory, directory)


@pytest.mark.parametrize("operation", ["rename_root", "rename_parent", "move_ancestor"])
def test_tracks_moves_without_blocking_directory_or_ancestor_rename(tracker, tmp_path, operation):
    parent = tmp_path / "game" / "loader"
    root = parent / "Mods"
    root.mkdir(parents=True)
    (root / "deployed.txt").write_text("mod data")
    tracker.track("game", str(root))
    if operation == "rename_root":
        destination = parent / "renamed Mods"
        root.rename(destination)
    elif operation == "rename_parent":
        parent.rename(parent.with_name("renamed loader"))
        destination = parent.with_name("renamed loader") / "Mods"
    else:
        elsewhere = tmp_path / "unrelated" / "deep"
        elsewhere.mkdir(parents=True)
        (tmp_path / "game").rename(elsewhere / "moved game")
        destination = elsewhere / "moved game" / "loader" / "Mods"
    result = tracker.resolve("game", str(root))
    assert_location(result, "moved", destination, root)
    assert (Path(result.path) / "deployed.txt").read_text() == "mod data"


def test_repeated_track_keeps_original_object_when_old_path_is_reoccupied(tracker, tmp_path):
    root = tmp_path / "Mods"
    root.mkdir()
    (root / "original.txt").write_text("original")
    tracker.track("game", str(root))
    moved = tmp_path / "moved Mods"
    root.rename(moved)
    root.mkdir()
    (root / "replacement.txt").write_text("replacement")
    tracker.track("game", str(root))
    assert_location(tracker.resolve("game", str(root)), "moved", moved, root)
    final = tmp_path / "final Mods"
    moved.rename(final)
    assert_location(tracker.resolve("game", str(root)), "moved", final, root)


def test_deleted_object_is_missing_even_when_old_path_becomes_a_new_directory(tracker, tmp_path):
    root = tmp_path / "Mods"
    root.mkdir()
    tracker.track("game", str(root))
    root.rmdir()
    assert_location(tracker.resolve("game", str(root)), "missing", "", root)
    root.mkdir()
    tracker.track("game", str(root))
    assert_location(tracker.resolve("game", str(root)), "missing", "", root)


def test_copied_then_deleted_object_cannot_find_the_new_copy(tracker, tmp_path):
    root = tmp_path / "Mods"
    root.mkdir()
    (root / "mod.txt").write_text("mod")
    tracker.track("game", str(root))
    shutil.copytree(root, tmp_path / "copy")
    shutil.rmtree(root)
    assert_location(tracker.resolve("game", str(root)), "missing", "", root)


def test_new_session_does_not_discover_offline_move(tmp_path):
    root = tmp_path / "Mods"
    root.mkdir()
    previous = DeploymentRootTracker()
    previous.track("game", str(root))
    previous.close()
    root.rename(tmp_path / "offline move")
    current = DeploymentRootTracker()
    try:
        assert_location(current.resolve("game", str(root)), "missing", "", root)
    finally:
        current.close()


def test_missing_configuration_is_not_silently_bound_when_directory_appears(tracker, tmp_path):
    root = tmp_path / "initially missing"
    assert_location(tracker.resolve("game", str(root)), "missing", "", root)
    root.mkdir()
    assert_location(tracker.resolve("game", str(root)), "missing", "", root)
    tracker.track("game", "")
    assert_location(tracker.resolve("game", str(root)), "current", root, root)


def test_adopting_resolved_path_preserves_tracking_for_the_next_move(tracker, tmp_path):
    root = tmp_path / "Mods"
    root.mkdir()
    tracker.track("game", str(root))
    moved = tmp_path / "moved"
    root.rename(moved)
    assert_location(tracker.resolve("game", str(root)), "moved", moved, root)
    assert_location(tracker.resolve("game", str(moved)), "current", moved, moved)
    again = tmp_path / "moved again"
    moved.rename(again)
    assert_location(tracker.resolve("game", str(moved)), "moved", again, moved)


@pytest.mark.skipif(os.name != "nt", reason="Windows provider without file ID opens")
def test_unsupported_id_open_falls_back_without_pinning_parent_names(tracker, tmp_path, monkeypatch):
    import ctypes

    def unsupported(*args, **kwargs):
        raise ctypes.WinError(50)

    monkeypatch.setattr(tracker._api, "open_by_id", unsupported)
    parent = tmp_path / "game"
    root = parent / "Mods"
    root.mkdir(parents=True)
    assert_location(tracker.resolve("game", str(root)), "current", root, root)
    parent.rename(tmp_path / "moved game")
    assert_location(tracker.resolve("game", str(root)), "missing", "", root)
    root.mkdir(parents=True)
    assert_location(tracker.resolve("game", str(root)), "missing", "", root)


def test_changed_configuration_rebinds_only_that_game(tracker, tmp_path):
    first, second, replacement = (tmp_path / name for name in ("first", "second", "replacement"))
    for directory in (first, second, replacement):
        directory.mkdir()
    tracker.track("one", str(first))
    tracker.track("two", str(second))
    tracker.track("one", str(replacement))
    moved = tmp_path / "second moved"
    second.rename(moved)
    assert_location(tracker.resolve("one", str(replacement)), "current", replacement, replacement)
    assert_location(tracker.resolve("two", str(second)), "moved", moved, second)
    tracker.track("one", "")
    assert_location(tracker.resolve("one", ""), "unset", "", "")


def test_forget_allows_explicit_reselection_without_losing_other_game_identity(tracker, tmp_path):
    missing, other = tmp_path / "missing", tmp_path / "other"
    other.mkdir()
    tracker.track("missing", str(missing))
    tracker.track("other", str(other))
    missing.mkdir()
    moved = tmp_path / "moved other"
    other.rename(moved)
    tracker.forget("missing")
    tracker.forget("unknown")
    assert_location(tracker.resolve("missing", str(missing)), "current", missing, missing)
    assert_location(tracker.resolve("other", str(other)), "moved", moved, other)


@pytest.mark.skipif(os.name != "nt", reason="Checks Windows handle lifecycle")
def test_replacement_unset_and_close_release_native_handles(tmp_path):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessHandleCount.restype = wintypes.BOOL

    def count():
        value = wintypes.DWORD()
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(value))
        return value.value

    tracker = DeploymentRootTracker()
    roots = [tmp_path / str(i) for i in range(12)]
    for root in roots:
        root.mkdir()
    baseline = count()
    try:
        tracker.track("game", str(roots[0]))
        assert count() == baseline + 1
        for root in roots[1:]:
            tracker.track("game", str(root))
            assert count() == baseline + 1
        tracker.track("other", str(roots[0]))
        assert count() == baseline + 2
        tracker.track("game", "")
        assert count() == baseline + 1
        tracker.forget("other")
        assert count() == baseline
        tracker.track("game", str(roots[0]))
        assert count() == baseline + 1
    finally:
        tracker.close()
    tracker.close()
    assert count() == baseline
