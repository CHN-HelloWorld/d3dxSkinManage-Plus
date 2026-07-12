import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from src.core.library_events import LibraryEventService
from src.core.watcher import FileChange
from src.core.watcher_windows import NativeContainerHandles, create_observer
from tests.core.test_library_local_events import make_library
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


@pytest.mark.skipif(os.name != "nt", reason="Windows file identity")
def test_native_identity_matches_python_stat_for_unchanged_file(tmp_path):
    from src.core.watcher_windows import _NativeAPI, read_marker_text

    marker = tmp_path / ".d3dxmod"
    marker.write_text('{"mod_id": "unchanged"}', encoding="utf-8")
    api = _NativeAPI()
    handle = api.open(marker)
    try:
        info = marker.stat()
        assert api.identity(handle) == (info.st_dev, info.st_ino)
    finally:
        api.k.CloseHandle(handle)
    assert read_marker_text(marker) == '{"mod_id": "unchanged"}'


@pytest.mark.skipif(os.name != "nt", reason="Windows shared marker read")
@pytest.mark.parametrize("kind", ["source", "deployment", "local_mod", "local_node"])
def test_container_marker_read_does_not_block_external_delete(tmp_path, monkeypatch, kind):
    from contextlib import contextmanager

    from src.core import watcher_windows as native
    from src.core.mod_identity import DEPLOYMENT_IDENTITY_KIND, read_deployment_identity
    from src.core.mod_package import _read_container_marker_data
    from src.utils import fs

    marker = tmp_path / (".d3dxcategory" if kind == "local_node" else ".d3dxmod")
    data = dict(mod_id="00000000000040008000000000000000", kind=DEPLOYMENT_IDENTITY_KIND, content_hash="hash")
    if kind == "local_node":
        data = {"id": "node"}
    marker.write_text(json.dumps(data), encoding="utf-8")
    deleted = []

    def remove():
        marker.unlink()
        deleted.append(True)

    def ordinary_read(path, **kwargs):
        with open(fs.native_path(path), encoding="utf-8") as stream:
            remove()
            return stream.read()

    monkeypatch.setattr(fs, "read_text", ordinary_read)
    original_open = Path.open

    @contextmanager
    def path_open(path, *args, **kwargs):
        with original_open(path, *args, **kwargs) as stream:
            if Path(fs.normal_path(path)) == marker:
                remove()
            yield stream

    monkeypatch.setattr(Path, "open", path_open)
    original_api = native._NativeAPI

    class ObservedAPI(original_api):
        def __init__(self):
            super().__init__()
            read = self.k.ReadFile

            def reading(*args):
                remove()
                return read(*args)

            self.k.ReadFile = reading

    monkeypatch.setattr(native, "_NativeAPI", ObservedAPI)
    reader = (
        _read_container_marker_data
        if kind == "source"
        else read_deployment_identity
        if kind == "deployment"
        else lambda path: LibraryEventService._marker(None, path)[2]
    )
    assert reader(tmp_path) == data
    assert deleted == [True]
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows deployment observer")
def test_deployment_watcher_delivers_moves_without_source_handle_baseline(qtbot, tmp_path, monkeypatch):
    from src.core import watcher_windows as native
    from src.core.watcher import FileWatcher

    def forbidden(*args, **kwargs):
        pytest.fail("deployment observer must not open source evidence handles")

    monkeypatch.setattr(native, "NativeContainerHandles", forbidden)
    source = tmp_path / "DISABLED_mod.partial"
    source.mkdir()
    (source / ".d3dxmod").write_text(json.dumps({"mod_id": "m"}))
    target = tmp_path / "active"
    watcher = FileWatcher(track_containers=False)
    events = []
    watcher.path_changed.connect(events.append)
    try:
        assert watcher.start(tmp_path)
        emitter = next(iter(watcher._observer.emitters))
        assert not hasattr(emitter, "_seed_thread")
        source.rename(target)
        qtbot.waitUntil(lambda: any(e.kind == "moved" and e.destination == str(target) for e in events))
        shutil.rmtree(target)
        qtbot.waitUntil(lambda: any(e.kind == "deleted" and e.path == str(target) for e in events))
        assert all(not e.native_evidence for e in events)
    finally:
        watcher.stop()


@pytest.mark.skipif(os.name != "nt", reason="Windows marker readability probe")
def test_marker_version_probe_allows_external_delete(tmp_path, monkeypatch):
    from src.core.library_inventory import marker_version

    marker = tmp_path / ".d3dxmod"
    marker.write_text("{}")
    fstat = os.fstat

    def during_probe(fd):
        marker.unlink()
        return fstat(fd)

    monkeypatch.setattr(os, "fstat", during_probe)
    assert marker_version(tmp_path)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle evidence")
def test_live_handle_proves_delete_and_tracks_cross_node_move(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    for path in (first, second):
        path.mkdir()
    source = first / "mod"
    source.mkdir()
    (source / ".d3dxmod").write_text(json.dumps({"mod_id": "m"}))
    handles = NativeContainerHandles()
    try:
        handles.track(source)
        target = second / "mod"
        source.rename(target)
        assert handles.inspect(source) == ("relocated", str(target), "m")
        shutil.rmtree(target)
        assert handles.inspect(target) == ("delete_pending", "", "m")
        assert not handles._handles
        assert not target.exists()
    finally:
        handles.close()


@pytest.mark.skipif(os.name != "nt", reason="actual native observer")
def test_native_observer_arms_seeds_delivers_evidence_and_closes(tmp_path):
    from watchdog.events import FileSystemEventHandler

    path = tmp_path / "mod"
    path.mkdir()
    (path / ".d3dxmod").write_text(json.dumps({"mod_id": "m"}))
    body = path / "body"
    body.mkdir()
    for number in range(20):
        (body / f"{number}.ini").write_text("[Constants]")
    moving = path / "moving"
    moving.mkdir()
    records = []
    observer = create_observer(lambda *args: None, native_callback=lambda *args: records.append(args))
    observer.schedule(FileSystemEventHandler(), str(tmp_path), recursive=True)
    observer.start()
    emitter = next(iter(observer.emitters))
    deadline = time.monotonic() + 3
    try:
        while not emitter._container_handles._handles and time.monotonic() < deadline:
            time.sleep(0.005)
        assert emitter._container_handles._handles
        shutil.rmtree(body)
        moving.rename(path / "moved-body")
        while not any(row[0] == "modified" for row in records) and time.monotonic() < deadline:
            time.sleep(0.005)
        assert any(row[0] == "modified" and row[1] == str(path / ".d3dxmod") for row in records)
        assert all(row[1] in {str(tmp_path), str(path / ".d3dxmod")} for row in records)
        shutil.rmtree(path)
        while (
            not any(row[0] == "batch_end" and any(r[4] == "delete_pending" for r in records) for row in records)
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        deleted = next(row for row in records if row[4] == "delete_pending")
        assert deleted[1] == str(path) and deleted[5] == "m"
        assert any(row[0] == "batch_end" and row[6] >= deleted[6] for row in records)
    finally:
        observer.stop()
        observer.join(5)
    assert not observer.is_alive()
    assert not emitter._seed_thread.is_alive()
    assert not emitter._container_handles._handles


def verified_delete(service, root, source, mid="m", batch=1):
    info = root.stat()
    identity = (info.st_dev, info.st_ino)
    service.handle(
        FileChange(
            str(root),
            service.generation,
            "deleted",
            str(source),
            native_evidence="delete_pending",
            evidence_mod_id=mid,
            batch_id=batch,
            root_identity=identity,
        )
    )
    return FileChange(str(root), service.generation, "batch_end", str(root), batch_id=batch, root_identity=identity)


@pytest.mark.skipif(os.name != "nt", reason="native atomic marker publication")
def test_post_start_atomic_marker_commit_seeds_outside_root_move_evidence(tmp_path):
    from watchdog.events import FileSystemEventHandler

    root = tmp_path / "library"
    root.mkdir()
    rows = []
    observer = create_observer(lambda *args: None, native_callback=lambda *args: rows.append(args))
    observer.schedule(FileSystemEventHandler(), str(root), recursive=True)
    observer.start()
    emitter = next(iter(observer.emitters))
    try:
        # 模拟启动时空目录基线已完成，然后导入创建容器并原子提交标记。
        emitter._seed_thread.join(2)
        assert not emitter._seed_thread.is_alive()
        source = root / "Imported"
        source.mkdir()
        pending = source / ".marker-pending"
        pending.write_text(json.dumps({"mod_id": "new-after-start"}))
        # 确认目录创建事件的首次登记确实未发现标记。
        deadline = time.monotonic() + 2
        while not any(row[0] == "created" and row[1] == str(source) for row in rows) and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not emitter._container_handles._handles
        os.replace(pending, source / ".d3dxmod")
        while not emitter._container_handles._handles and time.monotonic() < deadline:
            time.sleep(0.005)
        assert emitter._container_handles.containing_mod(source) == str(source)
        source.rename(tmp_path / "quarantine")
        while not any(row[4] == "outside_root" for row in rows) and time.monotonic() < deadline:
            time.sleep(0.005)
        proof = next(row for row in rows if row[4] == "outside_root")
        assert proof[0:2] == ("deleted", str(source))
        assert proof[5] == "new-after-start" and proof[6] > 0 and proof[7]
    finally:
        observer.stop()
        observer.join(5)
    assert not observer.is_alive()
    assert not emitter._container_handles._handles


def test_exact_deadline_boundary_and_early_timer_retry_hint(tmp_path):
    conn, root, paths, service = make_library(tmp_path)
    clock = [40000.0]
    service.clock = lambda: clock[0]
    source = root / "Box"
    shutil.rmtree(source)
    service.handle(FileChange(str(root), 7, "deleted", str(source)))
    deadline = service._missing["m"][0]
    clock[0] = deadline - 0.0005
    assert service.missing_retry_after_ms("m") == 2
    assert not service.recover_missing("m")
    assert not service.confirm_missing("m", generation=7, scope_complete=True)
    clock[0] = deadline
    assert service.missing_retry_after_ms("m") is None
    assert not service.recover_missing("m"), "elapsed time never proves a generic removed action"
    assert service.confirm_missing("m", generation=7, scope_complete=True)


def test_native_proof_skips_grace_only_after_batch_and_final_absence_checks(tmp_path):
    conn, root, paths, service = make_library(tmp_path)
    service.clock = lambda: 100.0
    source = root / "Box"
    shutil.rmtree(source)
    barrier = verified_delete(service, root, source)
    assert not service.recover_missing("m")
    assert not service.confirm_missing("m", generation=7, scope_complete=True)
    service.handle(barrier)
    assert service._missing["m"][0] > service.clock()
    assert service.missing_retry_after_ms("m") == 0
    assert service.confirm_missing("m", generation=7, scope_complete=True)
    source.mkdir()
    (source / ".d3dxmod").write_text(json.dumps({"mod_id": "replacement"}))
    assert not service.confirm_missing("m", generation=7, scope_complete=True)
    assert service.missing_retry_after_ms("m", immediate=False) != 0
    shutil.rmtree(source)
    service.protected = lambda path: True
    assert not service.recover_missing("m")
    assert service.missing_retry_after_ms("m", immediate=False) != 0


def test_immediate_hint_has_no_disk_io_and_final_root_is_checked_once(tmp_path, monkeypatch):
    conn, root, paths, service = make_library(tmp_path)
    source = root / "Box"
    shutil.rmtree(source)
    service.handle(verified_delete(service, root, source))
    original_stat = Path.stat
    calls = []

    def tracked_stat(path, *args, **kwargs):
        calls.append(str(path))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", tracked_stat)
    assert service.missing_retry_after_ms("m") == 0
    assert calls == []
    assert service.recover_missing("m")
    assert calls == []
    assert service.confirm_missing("m", generation=7, scope_complete=True)
    from src.utils import fs

    assert calls.count(fs.native_path(root)) == 1
    root.rename(root.with_name("old-root"))
    root.mkdir()
    assert not service.confirm_missing("m", generation=7, scope_complete=True)


@pytest.mark.parametrize("kind", ["overflow", "zero_bytes", "root_replaced", "watch_failed", "root_offline"])
def test_health_taint_blocks_proven_delete_until_matching_recovery(tmp_path, kind):
    conn, root, paths, service = make_library(tmp_path)
    service.missing_grace = 0
    source = root / "Box"
    shutil.rmtree(source)
    barrier = verified_delete(service, root, source)
    assert not service.recover_missing("m"), "batch must finish before final cleanup"
    service.handle(barrier)
    before = service.recovery_token()
    service.handle(FileChange(str(root), 7, kind, str(root)))
    assert not service.recover_missing("m")
    assert not service.confirm_missing("m", generation=7, scope_complete=True)
    assert not service.accept_recovery(before, scope_complete=True)
    assert not service.accept_recovery(service.recovery_token())
    assert service.accept_recovery(service.recovery_token(), scope_complete=True)
    assert service.recover_missing("m")


def test_generic_removed_never_scans_siblings_or_claims_definite_delete(tmp_path, monkeypatch):
    conn, root, paths, service = make_library(tmp_path)
    service.missing_grace = 0
    source = root / "Box"
    shutil.rmtree(source)
    service.handle(FileChange(str(root), 7, "deleted", str(source)))
    monkeypatch.setattr(os, "scandir", lambda *args: pytest.fail("ambiguous deletion must not scan siblings"))
    assert not service.recover_missing("m")


@pytest.mark.skipif(os.name != "nt", reason="Windows handle evidence")
@pytest.mark.parametrize("siblings", [500, 5000])
def test_actual_large_library_final_delete_is_local(mm, tmp_path, monkeypatch, siblings, record_property):
    from src.core.library_publication import _service
    from tests.helpers import make_mod

    node = mm.add_character("Many")
    victim = _import_mod(mm, node.id, tmp_path, "victim")
    source = mm._index.paths.container_path(victim.mod_id)
    root = mm._index.paths.roots["game_a"]
    marker_template = json.loads((source / ".d3dxmod").read_text(encoding="utf-8"))
    body = Path(victim.path).read_bytes()
    for number in range(siblings):
        mod = make_mod(mm._index, f"sibling{number}", node_id=node.id)
        folder = Path(mod.path).parent
        folder.mkdir()
        Path(mod.path).write_bytes(body)
        marker = dict(marker_template, mod_id=mod.mod_id)
        (folder / ".d3dxmod").write_text(json.dumps(marker), encoding="utf-8")
        assert mm._index.add_mod(mod, commit=False)
    mm._index._db().commit()
    handles = NativeContainerHandles()
    handles.track(source)
    assert handles._handles
    shutil.rmtree(source)
    try:
        evidence, destination, identity = handles.inspect(source)
        assert (evidence, destination, identity) == ("delete_pending", "", victim.mod_id)
    finally:
        handles.close()
    info = root.stat()
    root_identity = (info.st_dev, info.st_ino)
    service = _service(mm, 1)
    service.missing_grace = 0
    counts = {"scandir": 0, "markers": 0}
    path_open = Path.open

    def marker_read(path, *args, **kwargs):
        if Path(path).name in {".d3dxmod", ".d3dxcategory"}:
            counts["markers"] += 1
        return path_open(path, *args, **kwargs)

    def forbidden_scan(*args, **kwargs):
        counts["scandir"] += 1
        raise AssertionError("final deletion must not scan sibling directories")

    statements = []
    monkeypatch.setattr(os, "scandir", forbidden_scan)
    monkeypatch.setattr(Path, "open", marker_read)
    mm._index._db().set_trace_callback(statements.append)
    try:
        mm.apply_local_source_events(
            [
                FileChange(
                    str(root),
                    1,
                    "deleted",
                    str(source),
                    native_evidence=evidence,
                    evidence_mod_id=identity,
                    batch_id=1,
                    root_identity=root_identity,
                ),
                FileChange(str(root), 1, "batch_end", str(root), batch_id=1, root_identity=root_identity),
            ]
        )
        result = mm.confirm_local_missing()
        assert result.removed_ids == {victim.mod_id}
        assert mm.get_mod(victim.mod_id) is None
        assert mm._index._db().execute("SELECT count(*) FROM mods").fetchone()[0] == siblings
    finally:
        mm._index._db().set_trace_callback(None)
    assert counts["scandir"] == 0
    assert counts["markers"] <= 4
    assert len(statements) < 100
    record_property("local_delete_counts", dict(counts, sql=len(statements)))


def test_stream_control_bypasses_root_operation_protection(mm, monkeypatch):
    from src.core import library_publication as publication

    root = mm._index.paths.roots["game_a"]
    monkeypatch.setattr(publication, "path_protected", lambda *args: True)
    rows = [FileChange(str(root), 1, kind, str(root)) for kind in ("overflow", "batch_end", "rename_unpaired")]
    assert publication._ready_events(mm, rows, "source") == rows


@pytest.mark.skipif(os.name != "nt", reason="native body event filtering")
def test_native_body_events_coalesce_and_unknown_file_modifications_are_filtered(tmp_path):
    from watchdog.observers.api import EventQueue, ObservedWatch

    mod = tmp_path / "mod"
    mod.mkdir()
    (mod / ".d3dxmod").write_text(json.dumps({"mod_id": "m"}))
    rows = []
    observer = create_observer(lambda *args: None, native_callback=lambda *args: rows.append(args))
    emitter = observer._emitter_class(EventQueue(), ObservedWatch(str(tmp_path), recursive=True))
    handles = emitter._container_handles = NativeContainerHandles()
    handles.track(mod)
    try:
        for number in range(1000):
            for kind in ("modified", "deleted", "created", "moved"):
                emitter._detail(
                    kind,
                    str(mod / "body" / f"{number}.ini"),
                    str(mod / "body" / f"{number}.bak") if kind == "moved" else "",
                )
        emitter._detail("modified", str(tmp_path / "unmanaged.ini"))
        assert len(rows) == 1
        assert rows[0][:3] == ("modified", str(mod / ".d3dxmod"), "")
        emitter._detail("modified", str(tmp_path / ".d3dxcategory"))
        emitter._detail("overflow", str(tmp_path))
        assert [row[0] for row in rows] == ["modified", "modified", "overflow"]
        emitter._body_changes = set()
        emitter._detail("deleted", str(mod / "body"), directory=True)
        assert rows[-1][:2] == ("modified", str(mod / ".d3dxmod"))
    finally:
        handles.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle evidence")
def test_node_reparent_keeps_descendant_handle_and_no_global_handle_walk(tmp_path):
    node = tmp_path / "node"
    mod = node / "mod"
    mod.mkdir(parents=True)
    (node / ".d3dxcategory").write_text(json.dumps({"id": "n"}))
    (mod / ".d3dxmod").write_text(json.dumps({"mod_id": "m"}))
    handles = NativeContainerHandles()
    handles.track(node)
    handles.track(mod)

    class NoWalk(dict):
        def values(self):
            raise AssertionError("node move must not enumerate descendant handles")

        def __iter__(self):
            raise AssertionError("node move must not enumerate descendant handles")

    original = handles._handles
    handles._handles = NoWalk(original)
    target = node.with_name("renamed")
    try:
        node.rename(target)
        assert handles.inspect(node) == ("relocated", str(target), "n")
        shutil.rmtree(target / "mod")
        assert handles.inspect(target / "mod") == ("delete_pending", "", "m")
        assert len(handles._handles) == 1
    finally:
        handles._handles = {key: value for key, value in handles._handles.items()}
        handles.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows native stream")
def test_old_new_rename_records_pair_across_native_buffers(tmp_path, monkeypatch):
    from watchdog.observers import winapi
    from watchdog.observers.api import EventQueue, ObservedWatch

    rows = []
    observer = create_observer(lambda *args: None, native_callback=lambda *args: rows.append(args))
    emitter = observer._emitter_class(EventQueue(), ObservedWatch(str(tmp_path), recursive=True))
    emitter._whandle = 1
    monkeypatch.setattr(emitter, "should_keep_running", lambda: True)
    monkeypatch.setattr(winapi, "read_directory_changes", lambda *args, **kwargs: (b"x", 1))
    buffers = iter([[(winapi.FILE_ACTION_RENAMED_OLD_NAME, "old")], [(winapi.FILE_ACTION_RENAMED_NEW_NAME, "new")]])
    monkeypatch.setattr(winapi, "_parse_event_buffer", lambda *args: next(buffers))
    emitter.queue_events(0)
    emitter.queue_events(0)
    assert not any(row[0] == "deleted" for row in rows)
    moved = [row for row in rows if row[0] == "moved"]
    assert len(moved) == 1
    assert moved[0][1:3] == (str(tmp_path / "old"), str(tmp_path / "new"))


@pytest.mark.skipif(os.name != "nt", reason="Windows startup cleanup")
@pytest.mark.parametrize("failure", ["reader", "baseline"])
def test_failed_native_start_closes_handles_and_observer(tmp_path, monkeypatch, failure):
    from src.core import watcher_windows as native
    from src.core.watcher import FileWatcher

    created = []
    original = native.NativeContainerHandles

    def handles(*args):
        result = original(*args)
        created.append(result)
        return result

    monkeypatch.setattr(native, "NativeContainerHandles", handles)
    if failure == "reader":
        monkeypatch.setattr(native, "_ArmedDirectoryRead", lambda *args: (_ for _ in ()).throw(OSError("denied")))
    else:
        monkeypatch.setattr(threading.Thread, "start", lambda *args: (_ for _ in ()).throw(OSError("denied")))
    watcher = FileWatcher()
    with pytest.raises(OSError, match="denied"):
        watcher.start(tmp_path)
    assert (created and created[0].closed and not created[0]._handles) if failure == "baseline" else not created
    assert watcher._observer is None and not watcher.is_running()


@pytest.mark.skipif(os.name != "nt", reason="Windows unsupported ID API")
def test_unsupported_id_open_never_acquires_deletion_evidence(tmp_path, monkeypatch):
    source = tmp_path / "mod"
    source.mkdir()
    (source / ".d3dxmod").write_text(json.dumps({"mod_id": "m"}))
    handles = NativeContainerHandles()
    monkeypatch.setattr(handles.api, "open_by_id", lambda *args: (_ for _ in ()).throw(OSError("unsupported")))
    try:
        handles.track(source)
        shutil.rmtree(source)
        assert handles.inspect(source) == ("", "", "")
        assert not handles._handles
    finally:
        handles.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows seed identity race")
def test_track_rejects_directory_replacement_between_stat_and_open(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / ".d3dxmod").write_text(json.dumps({"mod_id": "old"}))
    handles = NativeContainerHandles()
    native_open = handles.api.open_by_id

    def replace_before_open(path, file_id, volume_hint, **kwargs):
        source.rename(tmp_path / "moved")
        source.mkdir()
        (source / ".d3dxmod").write_text(json.dumps({"mod_id": "new"}))
        return native_open(path, file_id, volume_hint, **kwargs)

    monkeypatch.setattr(handles.api, "open_by_id", replace_before_open)
    try:
        handles.track(source)
        assert not handles._handles
        assert handles.inspect(source) == ("", "", "")
    finally:
        handles.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows marker read rename race")
def test_id_marker_read_does_not_pin_partial_directory_during_rename(tmp_path, monkeypatch):
    source = tmp_path / "DISABLED_test.partial"
    source.mkdir()
    marker = source / ".d3dxmod"
    marker.write_text(json.dumps({"mod_id": "m"}))
    target = source.with_name("active")
    handles = NativeContainerHandles()
    original_read = handles.api.k.ReadFile

    def rename_while_marker_handle_open(*args):
        source.rename(target)
        return original_read(*args)

    monkeypatch.setattr(handles.api.k, "ReadFile", rename_while_marker_handle_open)
    try:
        value = handles.api.read_marker_by_id(marker, marker.stat())
        assert json.loads(value)["mod_id"] == "m"
        assert target.is_dir() and not source.exists()
        assert not handles._handles
    finally:
        handles.close()
