from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from src.core.mod_package import write_container_marker
from src.core.operation_preflight import OperationPreflightError
from src.models.mod import Mod, ModInfo
from tests.core import test_library_reconcile as library_fixtures

mm = library_fixtures.mm


def seed_shared_mods(manager, count=8, *, parent_id=""):
    node = manager.add_character("Shared tags", parent_id=parent_id)
    root = manager._index.paths.node_path(node.id)
    mods = []
    with manager._index.transaction():
        for number in range(count):
            name = f"mod-{number:05d}"
            body = root / name / "body.zip"
            body.parent.mkdir(parents=True)
            with ZipFile(body, "w") as archive:
                archive.writestr("mod.ini", f"[Constants]\nvalue={number}\n")
            mod = Mod(
                mod_id=uuid.UUID(int=number + 1, version=4).hex,
                game_id=manager.current_game_id,
                node_id=node.id,
                name=name,
                folder_name=name,
                path=str(body),
                content_hash=Mod.compute_sha1(body),
                info=ModInfo(tags=["Shared"], author="Original"),
            )
            assert write_container_marker(
                body.parent, body.name, mod.content_hash, mod_id=mod.mod_id, imported_at=mod.imported_at
            )
            assert manager._index.add_mod(mod, commit=False)
            assert manager._write_mod_sidecar(mod)
            mods.append(mod)
    return mods


def sidecar_path(mod):
    return Path(mod.path).parent / ".d3dxmeta" / "info.json"


def test_rollback_cleanup_failure_does_not_skip_other_sidecars(mm, monkeypatch, caplog):
    from contextlib import ExitStack

    from src.core.tag_file_batch import TagFileTarget, check_tag_files, restore_tag_files
    from src.utils import fs

    mods = seed_shared_mods(mm, 2)
    original_replace = os.replace
    original_unlink = fs.unlink
    blocked_path = sidecar_path(mods[-1])

    def replace(source, destination):
        if Path(fs.normal_path(destination)) == blocked_path:
            raise PermissionError("restore target locked")
        return original_replace(source, destination)

    def unlink(path, **kwargs):
        if Path(path).parent == blocked_path.parent:
            raise PermissionError("rollback cleanup locked")
        return original_unlink(path, **kwargs)

    with ExitStack() as pins:
        snapshots = check_tag_files(
            tuple(TagFileTarget.from_mod(mod) for mod in mods), full=False, previous={}, pins=pins
        )
        for snapshot in snapshots.values():
            data = json.loads(snapshot.data)
            data["info"]["author"] = "Changed"
            Path(snapshot.path).write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(fs, "unlink", unlink)
        restore_tag_files(list(snapshots.values()))

    first = snapshots[mods[0].mod_id]
    assert Path(first.path).read_bytes() == first.data
    assert "restore target locked" in caplog.text
    assert "rollback cleanup locked" in caplog.text


@pytest.mark.parametrize("trusted", [False, True])
def test_shared_rename_batches_io_without_crossing_sqlite_thread(mm, monkeypatch, trusted):
    mods = seed_shared_mods(mm)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.rename(draft.selected_ids[0], "Renamed")
    if trusted:
        mm.accept_operation_trust(mm.current_game_id)
    else:
        mm.invalidate_operation_trust()
    owner = threading.get_ident()
    original_db = mm._index._db
    original_hash = Mod.compute_sha1
    original_replace = os.replace
    hash_threads, write_threads, jobs = [], [], []

    def owner_db():
        assert threading.get_ident() == owner, "SQLite and path hydration must stay on their owner"
        return original_db()

    def compute_hash(path, **kwargs):
        hash_threads.append(threading.get_ident())
        return original_hash(path, **kwargs)

    def replace(source, destination):
        if Path(destination).name == "info.json":
            write_threads.append(threading.get_ident())
        return original_replace(source, destination)

    monkeypatch.setattr(mm._index, "_db", owner_db)
    monkeypatch.setattr(Mod, "compute_sha1", staticmethod(compute_hash))
    monkeypatch.setattr(os, "replace", replace)
    with ThreadPoolExecutor(max_workers=1) as executor:

        def run(work):
            jobs.append(work)
            return executor.submit(work).result(timeout=30)

        mm.operation_check_runner = run
        result = mm.commit_tag_edit(draft, {})

    assert len(jobs) <= 2, "A shared save must not create one worker/dialog per Mod"
    assert len(write_threads) == 8
    assert all(thread != owner for thread in write_threads), "Sidecar writes must use the I/O runner"
    assert len(hash_threads) == (0 if trusted else 8)
    assert all(thread != owner for thread in hash_threads)
    assert result.sidecar_mod_ids == {mod.mod_id for mod in mods}
    for mod in mods:
        assert mm.get_mod(mod.mod_id).info.tags == ["Renamed"]
        assert json.loads(sidecar_path(mod).read_text(encoding="utf-8"))["info"]["tags"] == ["Renamed"]
    assert not mm._index.in_transaction


def test_color_only_edit_does_not_inspect_other_mod_files(mm):
    mods = seed_shared_mods(mm, 3)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.set_color(draft.selected_ids[0], "#123ABC")
    before = {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods}
    Path(mods[1].path).unlink()

    result = mm.commit_tag_edit(draft, {})

    assert {mod.mod_id for mod in result.mods} == {mods[0].mod_id}
    assert result.affected_mod_ids == {mod.mod_id for mod in mods}
    assert result.sidecar_mod_ids == set()
    assert mm.get_tag_service().list_tags()[0].color == "#123ABC"
    assert {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods} == before


def test_shared_rename_preserves_unknown_sidecar_fields(mm):
    mods = seed_shared_mods(mm, 2)
    path = sidecar_path(mods[1])
    data = json.loads(path.read_text(encoding="utf-8"))
    data["future_top"] = {"keep": [1, 2]}
    data["info"]["future_info"] = {"keep": "description"}
    data["local"]["future_local"] = ["unchanged"]
    path.write_text(json.dumps(data), encoding="utf-8")
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.rename(draft.selected_ids[0], "Renamed")

    mm.commit_tag_edit(draft, {"author": "Edited"})

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["info"]["tags"] == ["Renamed"]
    assert saved["future_top"] == {"keep": [1, 2]}
    assert saved["info"]["future_info"] == {"keep": "description"}
    assert saved["local"]["future_local"] == ["unchanged"]
    assert saved["info"]["author"] == "Original"


def test_shared_write_failure_restores_bytes_in_io_runner(mm, monkeypatch):
    mods = seed_shared_mods(mm, 3)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.rename(draft.selected_ids[0], "Renamed")
    before = {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods}
    saved = {mod.mod_id: mm.get_mod(mod.mod_id).to_dict() for mod in mods}
    owner = threading.get_ident()
    replaced, restore_threads, events = [], [], []
    original_replace = os.replace
    failure_injected = False

    def replace(source, destination):
        nonlocal failure_injected
        if Path(destination).name == "info.json":
            if "restore" in Path(source).name:
                restore_threads.append(threading.get_ident())
            elif len(replaced) == 1 and not failure_injected:
                failure_injected = True
                raise PermissionError("injected second sidecar failure")
            else:
                replaced.append(str(destination))
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    mm.add_event_listener(lambda event, **payload: events.append(event))
    with ThreadPoolExecutor(max_workers=1) as executor:
        mm.operation_check_runner = lambda work: executor.submit(work).result(timeout=30)
        with pytest.raises(OperationPreflightError):
            mm.commit_tag_edit(draft, {"author": "Not committed"})

    assert failure_injected and len(replaced) == 1
    assert {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods} == before
    assert {mod.mod_id: mm.get_mod(mod.mod_id).to_dict() for mod in mods} == saved
    assert [tag.name for tag in mm.get_tag_service().list_tags()] == ["Shared"]
    assert restore_threads and all(thread != owner for thread in restore_threads)
    assert not {"mod_metadata_changed", "tags_changed"} & set(events)
    assert not mm._index.in_transaction


@pytest.mark.parametrize("failure_phase", ["sidecar", "sql"])
def test_failed_shared_deletion_can_retry_the_same_draft(mm, monkeypatch, failure_phase):
    from src.core import tag_file_batch

    mods = seed_shared_mods(mm, 3)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.remove_candidate(draft.selected_ids[0])
    original_bytes = {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods}
    original_write = tag_file_batch.write_sidecar

    def write(path, manifest):
        if path == mods[1].path:
            raise PermissionError("temporary metadata lock")
        return original_write(path, manifest)

    with monkeypatch.context() as patch:
        if failure_phase == "sidecar":
            patch.setattr(tag_file_batch, "write_sidecar", write)
            failure_type = OperationPreflightError
        else:
            mm._index._db().execute("""CREATE TRIGGER block_tag_save BEFORE UPDATE ON mods
                BEGIN SELECT RAISE(ABORT, 'temporary database failure'); END""")
            mm._index._db().commit()
            failure_type = sqlite3.IntegrityError
        with pytest.raises(failure_type):
            mm.commit_tag_edit(draft, {"author": "Saved author"})
    if failure_phase == "sql":
        mm._index._db().execute("DROP TRIGGER block_tag_save")
        mm._index._db().commit()

    assert not mm._index.in_transaction
    assert all(mm.get_mod(mod.mod_id).info.tags == ["Shared"] for mod in mods)
    assert {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods} == original_bytes
    result = mm.commit_tag_edit(draft, {"author": "Saved author"})

    assert result.sidecar_mod_ids == {mod.mod_id for mod in mods}
    assert mm.get_tag_service().list_tags() == []
    assert mm.get_mod(mods[0].mod_id).info.author == "Saved author"
    for mod in mods:
        assert mm.get_mod(mod.mod_id).info.tags == []
        assert json.loads(sidecar_path(mod).read_text(encoding="utf-8"))["info"]["tags"] == []


def test_shared_deletion_retry_still_rejects_external_changes_after_rollback(mm, monkeypatch):
    from src.core import tag_file_batch

    mods = seed_shared_mods(mm, 2)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.remove_candidate(draft.selected_ids[0])
    original_write = tag_file_batch.write_sidecar

    def write(path, manifest):
        if path == mods[1].path:
            raise PermissionError("temporary metadata lock")
        return original_write(path, manifest)

    with monkeypatch.context() as patch:
        patch.setattr(tag_file_batch, "write_sidecar", write)
        with pytest.raises(OperationPreflightError):
            mm.commit_tag_edit(draft, {})

    # 已知 ModInfo 字段之外的变更也应使打开时的版本失效，即使回滚凭据已刷新。
    path = sidecar_path(mods[0])
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata["external_extension"] = {"changed": True}
    path.write_text(json.dumps(metadata), encoding="utf-8")
    external_bytes = path.read_bytes()
    with pytest.raises(OperationPreflightError) as failure:
        mm.commit_tag_edit(draft, {})

    assert failure.value.mod_id == mods[0].mod_id
    assert path.read_bytes() == external_bytes
    assert all(mm.get_mod(mod.mod_id).info.tags == ["Shared"] for mod in mods)


def test_failed_restore_does_not_refresh_the_opened_receipt(mm, monkeypatch):
    from src.core import tag_file_batch

    mods = seed_shared_mods(mm, 2)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.remove_candidate(draft.selected_ids[0])
    opened = draft.file_receipt
    original_write = tag_file_batch.write_sidecar
    original_replace = os.replace

    def write(path, manifest):
        if path == mods[1].path:
            raise PermissionError("temporary metadata lock")
        return original_write(path, manifest)

    def replace(source, destination):
        if Path(source).name.startswith(".info-restore-"):
            raise PermissionError("restore target locked")
        return original_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(tag_file_batch, "write_sidecar", write)
        patch.setattr(os, "replace", replace)
        with pytest.raises(OperationPreflightError):
            mm.commit_tag_edit(draft, {})

    assert draft.file_receipt == opened
    with pytest.raises(OperationPreflightError):
        mm.commit_tag_edit(draft, {})
    assert all(mm.get_mod(mod.mod_id).info.tags == ["Shared"] for mod in mods)
    assert draft.is_dirty


@pytest.mark.parametrize("change_timing", ["after_replace", "during_read"])
def test_rollback_does_not_accept_an_external_write_as_its_own_version(mm, monkeypatch, change_timing):
    from src.core import tag_file_batch
    from src.utils import fs

    mods = seed_shared_mods(mm, 2)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.remove_candidate(draft.selected_ids[0])
    original_write = tag_file_batch.write_sidecar
    original_replace = os.replace
    original_read = fs.read_bytes
    current_path = sidecar_path(mods[0])
    restored = False
    external_bytes = None

    def external_write():
        nonlocal external_bytes
        metadata = json.loads(current_path.read_text(encoding="utf-8"))
        metadata["external_extension"] = {"changed": True}
        current_path.write_text(json.dumps(metadata), encoding="utf-8")
        external_bytes = current_path.read_bytes()

    def write(path, manifest):
        if path == mods[1].path:
            raise PermissionError("temporary metadata lock")
        return original_write(path, manifest)

    def replace(source, destination):
        nonlocal restored
        result = original_replace(source, destination)
        if Path(source).name.startswith(".info-restore-"):
            restored = True
            if change_timing == "after_replace":
                external_write()
        return result

    def read(path):
        data = original_read(path)
        if restored and Path(path) == current_path and change_timing == "during_read":
            external_write()
        return data

    with monkeypatch.context() as patch:
        patch.setattr(tag_file_batch, "write_sidecar", write)
        patch.setattr(os, "replace", replace)
        patch.setattr(fs, "read_bytes", read)
        with pytest.raises(OperationPreflightError):
            mm.commit_tag_edit(draft, {})

    assert external_bytes is not None
    with pytest.raises(OperationPreflightError):
        mm.commit_tag_edit(draft, {})
    assert current_path.read_bytes() == external_bytes
    assert all(mm.get_mod(mod.mod_id).info.tags == ["Shared"] for mod in mods)


def test_shared_rename_still_checks_every_untrusted_source(mm):
    mods = seed_shared_mods(mm, 3)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.rename(draft.selected_ids[0], "Renamed")
    before = {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods}
    mm.invalidate_operation_trust()
    with ZipFile(mods[2].path, "w") as archive:
        archive.writestr("mod.ini", "[Constants]\nvalue=external\n")

    with pytest.raises(OperationPreflightError):
        mm.commit_tag_edit(draft, {})

    assert {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods} == before
    assert [tag.name for tag in mm.get_tag_service().list_tags()] == ["Shared"]


def test_background_check_revalidates_index_source_before_saving(mm):
    mods = seed_shared_mods(mm, 3)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.rename(draft.selected_ids[0], "Renamed")
    before = {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods}

    def run(work):
        result = work()
        mm._index._db().execute("UPDATE mods SET path=? WHERE mod_id=?", ("replaced.zip", mods[0].mod_id))
        mm._index._db().commit()
        return result

    mm.invalidate_operation_trust()
    mm.operation_check_runner = run
    # 通过原始行快照校验，不重新构造全部 Mod。
    with pytest.raises((OperationPreflightError, ValueError)):
        mm.commit_tag_edit(draft, {})

    assert {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods} == before
    assert mm.get_mod(mods[0].mod_id).path == str(Path(mods[0].path).with_name("replaced.zip"))
    assert [tag.name for tag in mm.get_tag_service().list_tags()] == ["Shared"]


def test_deferred_sql_commit_failure_restores_sidecars_and_closes_transaction(mm):
    mods = seed_shared_mods(mm, 2)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.rename(draft.selected_ids[0], "Renamed")
    before = {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods}
    conn = mm._index._db()
    conn.executescript("""
        CREATE TABLE tag_commit_parent (id INTEGER PRIMARY KEY);
        CREATE TABLE tag_commit_guard (
            parent INTEGER REFERENCES tag_commit_parent(id) DEFERRABLE INITIALLY DEFERRED);
        CREATE TRIGGER fail_tag_commit AFTER UPDATE OF info_json ON mods
        BEGIN INSERT INTO tag_commit_guard VALUES (1); END;
    """)

    with pytest.raises(sqlite3.IntegrityError):
        mm.commit_tag_edit(draft, {})

    assert {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods} == before
    assert not mm._index.in_transaction
    assert [tag.name for tag in mm.get_tag_service().list_tags()] == ["Shared"]
    assert all(mm.get_mod(mod.mod_id).info.tags == ["Shared"] for mod in mods)


def test_shared_manifest_preparation_uses_io_runner(mm, monkeypatch):
    from src.core import tag_file_batch

    mods = seed_shared_mods(mm, 3)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.rename(draft.selected_ids[0], "Renamed")
    owner = threading.get_ident()
    threads = []
    original = tag_file_batch.tag_sidecar_payload

    def prepare(*args):
        threads.append(threading.get_ident())
        return original(*args)

    monkeypatch.setattr(tag_file_batch, "tag_sidecar_payload", prepare)
    with ThreadPoolExecutor(max_workers=1) as executor:
        mm.operation_check_runner = lambda work: executor.submit(work).result(timeout=30)
        mm.commit_tag_edit(draft, {})

    assert len(threads) == 3
    assert all(thread != owner for thread in threads), "Manifest preparation must not stall the GUI"
    assert all(mm.get_mod(mod.mod_id).info.tags == ["Renamed"] for mod in mods)


def test_shared_metadata_checks_do_not_repeat_unrelated_path_serialization(mm, monkeypatch):
    from src.models import mod as mod_model

    mods = seed_shared_mods(mm, 3)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.rename(draft.selected_ids[0], "Renamed")
    original = mod_model.to_stored_path
    paths = []

    def serialize(path):
        paths.append(path)
        return original(path)

    monkeypatch.setattr(mod_model, "to_stored_path", serialize)
    mm.commit_tag_edit(draft, {})

    assert len(paths) <= len(mods), "Metadata checks must not keep reserializing each Mod and its previews"
    assert all(mm.get_mod(mod.mod_id).info.tags == ["Renamed"] for mod in mods)


@pytest.mark.parametrize("fail_write", [False, True])
def test_background_update_cannot_commit_pending_tag_sql(mm, monkeypatch, fail_write):
    from src.core import tag_file_batch
    from src.core.mod_update_service import ModUpdateService

    mods = seed_shared_mods(mm, 3)
    unrelated = mm.get_mod(mods[2].mod_id)
    unrelated.info.tags = []
    assert mm.update_mod(unrelated)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.rename(draft.selected_ids[0], "Renamed")
    mm.accept_operation_trust(mm.current_game_id)
    before = {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods[:2]}
    original_write = tag_file_batch.write_sidecar
    writes, observed, events = [], [], []
    mm.add_event_listener(lambda event, **payload: events.append((event, payload)))

    def write(path, manifest):
        writes.append(str(path))
        if fail_write and len(writes) == 2:
            raise PermissionError("injected second tag sidecar failure")
        return original_write(path, manifest)

    def run(work):
        if work.func is tag_file_batch.write_tag_files:
            observed.append((mm._index.in_transaction, mm.get_mod(mods[0].mod_id).info.tags))
            # 使用后台更新检查器实际注册的回调。
            ModUpdateService._write_last_checked(SimpleNamespace(_mm=mm), unrelated.mod_id)
            with sqlite3.connect(mm._index._db_path) as observer:
                info = json.loads(
                    observer.execute("SELECT info_json FROM mods WHERE mod_id=?", (unrelated.mod_id,)).fetchone()[0]
                )
                assert info["custom_data"]["gamebanana"]["last_checked_iso"]
            observed.append((mm._index.in_transaction, mm.get_mod(mods[0].mod_id).info.tags))
        return work()

    monkeypatch.setattr(tag_file_batch, "write_sidecar", write)
    mm.operation_check_runner = run
    if fail_write:
        with pytest.raises(OperationPreflightError):
            mm.commit_tag_edit(draft, {})
        assert {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods[:2]} == before
        assert [mm.get_mod(mod.mod_id).info.tags for mod in mods[:2]] == [["Shared"], ["Shared"]]
        assert [tag.name for tag in mm.get_tag_service().list_tags()] == ["Shared"]
        assert [
            (event, payload.get("mod_id"))
            for event, payload in events
            if event in {"mod_metadata_changed", "tags_changed"}
        ] == [("mod_metadata_changed", unrelated.mod_id)]
    else:
        mm.commit_tag_edit(draft, {})
        assert all(mm.get_mod(mod.mod_id).info.tags == ["Renamed"] for mod in mods[:2])
        assert all(
            json.loads(sidecar_path(mod).read_text(encoding="utf-8"))["info"]["tags"] == ["Renamed"] for mod in mods[:2]
        )
    updated = mm.get_mod(unrelated.mod_id)
    checked = updated.info.custom_data["gamebanana"]["last_checked_iso"]
    assert checked
    assert (
        json.loads(sidecar_path(unrelated).read_text(encoding="utf-8"))["info"]["custom_data"]["gamebanana"][
            "last_checked_iso"
        ]
        == checked
    )
    assert observed == [(False, ["Shared"]), (False, ["Shared"])]
    assert not mm._index.in_transaction


def test_ambient_transaction_rejects_tag_save_without_io_or_rollback(mm):
    mods = seed_shared_mods(mm, 2)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.rename(draft.selected_ids[0], "Renamed")
    before = {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods}
    jobs = []

    def run(work):
        jobs.append(work)
        return work()

    mm.operation_check_runner = run
    conn = mm._index._db()
    conn.execute("UPDATE mods SET name=? WHERE mod_id=?", ("Pending other write", mods[1].mod_id))
    try:
        with pytest.raises((ValueError, sqlite3.OperationalError)):
            mm.commit_tag_edit(draft, {})
        assert jobs == []
        assert conn.in_transaction
        assert mm.get_mod(mods[1].mod_id).name == "Pending other write"
        assert {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods} == before
        assert [tag.name for tag in mm.get_tag_service().list_tags()] == ["Shared"]
    finally:
        conn.rollback()


def test_color_edit_hydration_is_constant_and_event_keeps_all_references(mm, monkeypatch):
    mods = seed_shared_mods(mm, 32)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.set_color(draft.selected_ids[0], "mint")
    hydrated, events = [], []
    original = mm._index._row_to_mod

    def hydrate(row, cache=None):
        hydrated.append(row["mod_id"])
        return original(row, cache)

    monkeypatch.setattr(mm._index, "_row_to_mod", hydrate)
    mm.add_event_listener(lambda event, **payload: events.append((event, payload)))
    result = mm.commit_tag_edit(draft, {})

    assert len(hydrated) <= 4, "A catalog color change must not hydrate every referring Mod"
    assert [mod.mod_id for mod in result.mods] == [mods[0].mod_id]
    assert result.affected_mod_ids == {mod.mod_id for mod in mods}
    assert events == [
        ("tags_changed", {"affected_ids": sorted(mod.mod_id for mod in mods), "game_id": mm.current_game_id})
    ]


def test_shared_rename_does_not_rehydrate_mods_for_snapshot_validation(mm, monkeypatch):
    mods = seed_shared_mods(mm, 8)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.rename(draft.selected_ids[0], "Renamed")
    hydrated = []
    original = mm._index._row_to_mod

    def hydrate(row, cache=None):
        hydrated.append(row["mod_id"])
        return original(row, cache)

    monkeypatch.setattr(mm._index, "_row_to_mod", hydrate)
    result = mm.commit_tag_edit(draft, {})

    assert len(hydrated) <= 3 * len(mods), "Validate raw rows and ancestor identities without rebuilding Mod objects"
    assert result.sidecar_mod_ids == {mod.mod_id for mod in mods}
    assert all(mod.info.tags == ["Renamed"] for mod in result.mods)


@pytest.mark.parametrize("phase", ["check", "write"])
@pytest.mark.parametrize("target_kind", ["node", "ancestor"])
def test_node_chain_move_during_io_rejects_tag_save(mm, phase, target_kind):
    from src.core import tag_file_batch

    ancestor = mm.add_character("Ancestor")
    mods = seed_shared_mods(mm, 3, parent_id=ancestor.id)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.rename(draft.selected_ids[0], "Renamed")
    before = {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods}
    target = mods[0].node_id if target_kind == "node" else ancestor.id
    parent_id = ancestor.id if target_kind == "node" else None
    boundary = tag_file_batch.check_tag_files if phase == "check" else tag_file_batch.write_tag_files

    def run(work):
        result = work()
        if work.func is boundary:
            mm._index.paths.move_node(target, parent_id, "Moved target")
        return result

    mm.operation_check_runner = run
    with pytest.raises(OperationPreflightError):
        mm.commit_tag_edit(draft, {})

    assert mm._index.paths.node_path(target).name == "Moved target"
    assert {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods} == before
    assert [tag.name for tag in mm.get_tag_service().list_tags()] == ["Shared"]
    assert not mm._index.in_transaction


def test_node_chain_snapshot_precedes_target_hydration(mm, monkeypatch):
    ancestor = mm.add_character("Ancestor")
    mods = seed_shared_mods(mm, 3, parent_id=ancestor.id)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.rename(draft.selected_ids[0], "Renamed")
    before = {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods}
    original = mm._index.get_mods_by_ids
    moved = False

    def hydrate(ids):
        nonlocal moved
        result = original(ids)
        if not moved:
            moved = True
            mm._index.paths.move_node(ancestor.id, None, "Moved ancestor")
        return result

    monkeypatch.setattr(mm._index, "get_mods_by_ids", hydrate)
    with pytest.raises(OperationPreflightError):
        mm.commit_tag_edit(draft, {})

    assert moved
    assert {mod.mod_id: sidecar_path(mod).read_bytes() for mod in mods} == before
    assert [tag.name for tag in mm.get_tag_service().list_tags()] == ["Shared"]


@pytest.mark.parametrize("phase", ["check", "write"])
def test_unrelated_subtree_move_does_not_invalidate_tag_edit(mm, phase):
    from src.core import tag_file_batch

    mods = seed_shared_mods(mm, 3)
    unrelated = mm.add_character("Other subtree")
    mm.add_character("Other child", parent_id=unrelated.id)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.rename(draft.selected_ids[0], "Renamed")
    boundary = tag_file_batch.check_tag_files if phase == "check" else tag_file_batch.write_tag_files

    def run(work):
        result = work()
        if work.func is boundary:
            mm._index.paths.move_node(unrelated.id, None, "Moved unrelated subtree")
        return result

    mm.operation_check_runner = run
    result = mm.commit_tag_edit(draft, {})

    assert mm._index.paths.node_path(unrelated.id).name == "Moved unrelated subtree"
    assert result.sidecar_mod_ids == {mod.mod_id for mod in mods}
    assert all(mod.info.tags == ["Renamed"] for mod in result.mods)
