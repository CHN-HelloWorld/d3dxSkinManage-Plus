import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from src.core.library_events import LibraryEventService
from src.core.library_paths import LibraryPaths
from src.core.library_schema import create_schema
from src.core.watcher import FileChange


def make_library(tmp_path, count=0):
    root = tmp_path / "library"
    root.mkdir()
    conn = sqlite3.connect(":memory:")
    create_schema(conn)
    conn.executemany(
        "INSERT INTO mods(mod_id,game_id,folder_name,path,imported_at) VALUES(?,'g',?,'body','2026')",
        [(f"unrelated-{i}", f"unrelated-{i}") for i in range(count)],
    )
    conn.execute("INSERT INTO mods(mod_id,game_id,folder_name,path,imported_at) VALUES('m','g','Box','body','2026')")
    conn.commit()
    box = root / "Box"
    box.mkdir()
    (box / "body").write_text("data")
    (box / ".d3dxmod").write_text(json.dumps({"mod_id": "m", "body": "body"}))
    paths = LibraryPaths(conn, {"g": root})
    return conn, root, paths, LibraryEventService(paths, generation=7)


@pytest.mark.parametrize("kind", ["rename", "delete", "marker"])
def test_single_event_work_independent_of_unrelated_library_size(tmp_path, monkeypatch, kind):
    work = []
    for size in (500, 5000):
        folder = tmp_path / str(size)
        folder.mkdir()
        conn, root, paths, service = make_library(folder, size)
        source = root / "Box"
        if kind == "rename":
            target = root / "Renamed"
            source.rename(target)
            event = FileChange(str(root), 7, "moved", str(source), str(target), True)
        elif kind == "delete":
            shutil.rmtree(source)
            event = FileChange(str(root), 7, "deleted", str(source), is_directory=True)
        else:
            event = FileChange(str(root), 7, "modified", str(source / ".d3dxmod"))
        counts = {"stat": 0, "open": 0, "sql_steps": 0}
        original_stat, original_open = Path.stat, Path.open

        def stat(path, *args, **kwargs):
            counts["stat"] += 1
            return original_stat(path, *args, **kwargs)

        def open_file(path, *args, **kwargs):
            counts["open"] += 1
            return original_open(path, *args, **kwargs)

        def progress():
            counts["sql_steps"] += 1
            return 0

        def forbidden(*args, **kwargs):
            raise AssertionError("ordinary event performed a directory/full-library scan")

        with monkeypatch.context() as patch:
            patch.setattr(Path, "stat", stat)
            patch.setattr(Path, "open", open_file)
            patch.setattr("os.scandir", forbidden)
            conn.set_progress_handler(progress, 1)
            result = service.handle(event)
            conn.set_progress_handler(None, 0)
        assert not result.conflicts
        assert result.pending_missing_ids == ({"m"} if kind == "delete" else set())
        if kind != "delete":
            assert result.updated_mod_ids == {"m"}
        assert conn.execute("SELECT count(*) FROM mods").fetchone()[0] == size + 1
        work.append(counts)
    assert work[0] == work[1], work


@pytest.mark.parametrize(
    "scenario",
    [
        "split_move",
        "out_of_order",
        "duplicate",
        "atomic_replace",
        "offline",
        "read_error",
        "overflow",
        "generation",
        "protected",
    ],
)
def test_move_and_failure_boundaries(tmp_path, monkeypatch, scenario):
    conn, root, paths, service = make_library(tmp_path)
    old, new = root / "Box", root / "New"

    def event(kind, path, dest=""):
        return FileChange(str(root), 7, kind, str(path), str(dest))

    if scenario in ("split_move", "out_of_order"):
        old.rename(new)
        if scenario == "split_move":
            pending = service.handle(event("deleted", old))
            assert pending.pending_missing_ids == {"m"}
            assert not service.confirm_missing("m", generation=7, scope_complete=True)
            result = service.handle(event("created", new))
        else:
            third = root / "Third"
            new.rename(third)
            result = service.handle(event("moved", new, third))
            service.handle(event("moved", old, new))
            new = third
        assert result.updated_mod_ids == {"m"}
        assert paths.container_path("m") == new
        assert not service.confirm_missing("m", generation=7, scope_complete=True)
    elif scenario == "duplicate":
        shutil.copytree(old, new)
        result = service.handle(event("created", new))
        assert result.conflicts == {"m"}
        assert len(service.locations.locations("m")) == 2
        assert paths.container_path("m") == old
    elif scenario == "atomic_replace":
        temporary = old / "tmp"
        temporary.write_text(json.dumps({"mod_id": "m", "body": "body"}))
        temporary.replace(old / ".d3dxmod")
        assert service.handle(event("moved", temporary, old / ".d3dxmod")).updated_mod_ids == {"m"}
    elif scenario == "offline":
        root.rename(tmp_path / "offline")
        result = service.handle(event("deleted", old))
        assert result.review_paths == {str(root)}
        assert not result.pending_missing_ids
    elif scenario == "read_error":

        def denied(*args, **kwargs):
            raise PermissionError("locked")

        import os

        if os.name == "nt":
            monkeypatch.setattr("src.core.watcher_windows.read_marker_text", denied)
        else:
            monkeypatch.setattr("src.utils.fs.read_text", denied)
        result = service.handle(event("modified", old / ".d3dxmod"))
        assert result.review_paths
        assert not result.pending_missing_ids
    elif scenario == "overflow":
        assert service.handle(event("overflow", root)).review_paths == {str(root)}
    elif scenario == "generation":
        assert service.handle(FileChange(str(root), 6, "deleted", str(old))).ignored
    else:
        old.rename(new)
        service.protected = lambda p: p == str(old)
        result = service.handle(event("created", new))
        assert result.review_paths
        assert paths.container_path("m") == old
        service.protected = lambda p: False
        assert service.handle(event("created", new)).updated_mod_ids == {"m"}
    assert conn.execute("SELECT count(*) FROM mods").fetchone()[0] == 1


def test_confirmed_missing_requires_review_grace_and_online_root(tmp_path):
    conn, root, paths, service = make_library(tmp_path)
    now = [0.0]
    service.clock = lambda: now[0]
    shutil.rmtree(root / "Box")
    service.handle(FileChange(str(root), 7, "deleted", str(root / "Box")))
    now[0] = 1.0
    assert not service.confirm_missing("m", generation=7)
    assert service.confirm_missing("m", generation=7, scope_complete=True)
    assert conn.execute("SELECT count(*) FROM mods").fetchone()[0] == 1
    root.rmdir()
    assert not service.confirm_missing("m", generation=7, scope_complete=True)


def test_node_move_only_updates_node_row(tmp_path):
    conn, root, paths, service = make_library(tmp_path)
    conn.execute("INSERT INTO nodes(id,game_id,directory_name) VALUES('n','g','Node')")
    conn.execute("UPDATE mods SET node_id='n' WHERE mod_id='m'")
    conn.commit()
    node = root / "Node"
    node.mkdir()
    (root / "Box").rename(node / "Box")
    (node / ".d3dxcategory").write_text(json.dumps({"id": "n"}))
    node.rename(root / "NewNode")
    sql = []
    conn.set_trace_callback(sql.append)
    result = service.handle(FileChange(str(root), 7, "moved", str(node), str(root / "NewNode"), True))
    assert result.moved_node_ids == {"n"}
    assert not any(s.upper().startswith("UPDATE MODS") for s in sql)
    assert paths.body_path("m") == root / "NewNode/Box/body"


@pytest.mark.skipif(os.name != "nt", reason="native Windows watchdog adapter")
@pytest.mark.parametrize("scenario", ["zero_bytes", "watch_failed", "root_offline", "split_rename"])
def test_windows_native_health_and_split_records(tmp_path, monkeypatch, scenario):
    from watchdog.observers import winapi
    from watchdog.observers.api import EventQueue, ObservedWatch

    from src.core.watcher_windows import create_observer

    health, events = [], []
    observer = create_observer(lambda kind, path: health.append((kind, path)))
    emitter = observer._emitter_class(EventQueue(), ObservedWatch(str(tmp_path), recursive=True))
    emitter._whandle = 1
    monkeypatch.setattr(emitter, "should_keep_running", lambda: True)
    monkeypatch.setattr(emitter, "queue_event", events.append)
    monkeypatch.setattr(emitter, "stop", lambda: None)

    def read(*args, **kwargs):
        if scenario == "watch_failed":
            raise OSError("native read failed")
        return b"buffer", 0 if scenario == "zero_bytes" else 1

    monkeypatch.setattr(winapi, "read_directory_changes", read)
    action = winapi.FILE_ACTION_REMOVED_SELF if scenario == "root_offline" else winapi.FILE_ACTION_RENAMED_NEW_NAME
    monkeypatch.setattr(winapi, "_parse_event_buffer", lambda *args: [(action, "new")])
    emitter.queue_events(0)
    if scenario == "split_rename":
        assert not health
        assert len(events) == 1 and events[0].event_type == "created"
        assert events[0].src_path == str(tmp_path / "new")
    else:
        assert health == [(scenario, str(tmp_path))]
