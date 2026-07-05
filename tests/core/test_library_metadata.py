import json
import threading
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from src.core.mod_package import read_container_marker
from src.ui.library_metadata import LibraryMetadataController, capture_request, publish_metadata, read_metadata
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def change_body(mod, value):
    with zipfile.ZipFile(mod.path, "w") as archive:
        archive.writestr("mod.ini", "[Constants]\nx=" + value)


def test_self_written_metadata_does_not_write_or_publish_again(mm, tmp_path):
    mod = _import_mod(mm, mm.add_character("Metadata").id, tmp_path, "self_edit")
    renamed = mm.rename_mod(mod.mod_id, "Renamed")
    assert renamed is not None
    result = read_metadata(capture_request(mm, mod.mod_id), mm._find_previews)
    before = mm._index._db().total_changes
    assert not publish_metadata(mm, result)
    assert mm._index._db().total_changes == before


def test_content_and_sidecar_refresh_keeps_identity_and_import_time(mm, tmp_path):
    mod = _import_mod(mm, mm.add_character("Metadata").id, tmp_path, "changed")
    request = capture_request(mm, mod.mod_id)
    change_body(mod, "different")
    sidecar = Path(request.container) / ".d3dxmeta" / "info.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["name"] = "External name"
    data["info"]["author"] = "External author"
    data["info"]["tags"] = []
    sidecar.write_text(json.dumps(data), encoding="utf-8")
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(read_metadata, request, mm._find_previews).result()
    assert publish_metadata(mm, result)
    changed = mm.get_mod(mod.mod_id)
    assert (changed.mod_id, changed.imported_at) == (mod.mod_id, mod.imported_at)
    assert changed.content_hash != mod.content_hash
    assert changed.name == "External name" and changed.info.author == "External author"
    assert changed.info.tags == []
    assert read_container_marker(request.container)["content_hash"] == changed.content_hash


def test_late_metadata_result_cannot_publish_after_source_changes(mm, tmp_path):
    mod = _import_mod(mm, mm.add_character("Metadata").id, tmp_path, "stale")
    request = capture_request(mm, mod.mod_id)
    change_body(mod, "first")
    result = read_metadata(request, mm._find_previews)
    change_body(mod, "second")
    assert not publish_metadata(mm, result)
    assert mm.get_mod(mod.mod_id).content_hash == mod.content_hash


def test_controller_hashes_off_owner_and_publishes_on_owner(mm, tmp_path, qtbot, monkeypatch):
    from src.models.mod import Mod

    mod = _import_mod(mm, mm.add_character("Metadata").id, tmp_path, "async")
    change_body(mod, "async_changed")
    owner = threading.get_ident()
    hash_threads, published = [], []
    compute = Mod.compute_sha1

    def compute_tracked(*args, **kwargs):
        hash_threads.append(threading.get_ident())
        return compute(*args, **kwargs)

    monkeypatch.setattr(Mod, "compute_sha1", compute_tracked)
    controller = LibraryMetadataController(mm, lambda delta: published.append((threading.get_ident(), delta)))
    controller.schedule({mod.mod_id})
    qtbot.waitUntil(lambda: bool(published), timeout=5000)
    assert hash_threads and all(t != owner for t in hash_threads)
    assert published[0][0] == owner
    assert published[0][1].changed_ids == {mod.mod_id}
    controller.pool.shutdown(wait=True)
    controller.deleteLater()


def test_stop_rejects_running_worker_publication(mm, tmp_path, qtbot, monkeypatch):
    mod = _import_mod(mm, mm.add_character("Metadata").id, tmp_path, "closing")
    change_body(mod, "changed_before_close")
    entered, release = threading.Event(), threading.Event()
    original = mm._find_previews

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(mm, "_find_previews", held)
    published = []
    controller = LibraryMetadataController(mm, published.append)
    controller.schedule({mod.mod_id})
    qtbot.waitUntil(entered.is_set)
    future = controller.future
    controller.stop()
    release.set()
    qtbot.waitUntil(future.done)
    qtbot.wait(20)
    assert published == []
    assert mm.get_mod(mod.mod_id).content_hash == mod.content_hash
    controller.deleteLater()


def test_sidecar_explicit_preview_is_retained_without_auto_discovery(mm, tmp_path):
    mod = _import_mod(mm, mm.add_character("Metadata").id, tmp_path, "manual_preview")
    container = Path(mod.path).parent
    preview = container / ".d3dxmeta" / "custom.png"
    preview.write_bytes(b"preview")
    sidecar = container / ".d3dxmeta" / "info.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["local"]["preview_references"] = [{"scope": "container", "path": ".d3dxmeta/custom.png"}]
    sidecar.write_text(json.dumps(data), encoding="utf-8")
    result = read_metadata(capture_request(mm, mod.mod_id), lambda *args, **kwargs: [])
    assert publish_metadata(mm, result)
    assert mm.get_mod(mod.mod_id).preview_images == [str(preview)]


def test_metadata_completion_during_shared_tag_save_waits_for_commit(mm, qtbot):
    from src.core import tag_file_batch
    from tests.core.test_tag_edit_batch import seed_shared_mods, sidecar_path

    mods = seed_shared_mods(mm, 2)
    draft = mm.get_tag_service().begin_edit(mods[0])
    draft.remove_candidate(draft.selected_ids[0])
    published = []
    controller = LibraryMetadataController(mm, published.append)
    started_stamp = mm.library_probe_stamp

    def runner(work):
        result = work()
        if work.func is tag_file_batch.write_tag_files:
            # 文件读取可能在检查的嵌套事件循环中完成，此时元数据已写入而索引事务尚未提交。
            future = Future()
            future.metadata_generation = controller.generation
            future.metadata_stamp = started_stamp
            future.set_result(read_metadata(capture_request(mm, mods[0].mod_id), mm._find_previews))
            controller.active, controller.future = mods[0].mod_id, future
            controller._finish(future)
        return result

    mm.operation_check_runner = runner
    try:
        result = mm.commit_tag_edit(draft, {})
        assert result.deleted_tag_ids
        assert not published, "Background refresh must not publish a partially saved tag edit"
        qtbot.waitUntil(lambda: not controller.pending and controller.active is None)
        for mod in mods:
            assert mm.get_mod(mod.mod_id).info.tags == []
            assert json.loads(sidecar_path(mod).read_text(encoding="utf-8"))["info"]["tags"] == []
        assert not published, "The completed edit must not be published a second time"
    finally:
        controller.stop()
        controller.pool.shutdown(wait=True)
        controller.deleteLater()


def test_metadata_reader_waits_until_owned_mutation_finishes(mm, tmp_path, qtbot):
    from src.core.library_snapshot import library_mutation

    mod = _import_mod(mm, mm.add_character("Metadata").id, tmp_path, "owned")
    change_body(mod, "new_body")
    published = []
    controller = LibraryMetadataController(mm, published.append)

    @library_mutation
    def save(manager):
        controller.schedule({mod.mod_id})
        controller._start()
        assert controller.future is None, "Do not read intermediate files during an owned save"
        assert not published

    try:
        save(mm)
        qtbot.waitUntil(lambda: bool(published), timeout=5000)
        assert mm.get_mod(mod.mod_id).content_hash != mod.content_hash
    finally:
        controller.stop()
        controller.pool.shutdown(wait=True)
        controller.deleteLater()


def test_metadata_result_spanning_finished_mutation_is_read_again(mm, tmp_path, qtbot):
    from src.core.library_snapshot import library_mutation

    mod = _import_mod(mm, mm.add_character("Metadata").id, tmp_path, "across_save")
    request = capture_request(mm, mod.mod_id)
    published = []
    controller = LibraryMetadataController(mm, published.append)
    future = Future()
    future.metadata_generation = controller.generation
    future.metadata_stamp = mm.library_probe_stamp
    future.set_result(read_metadata(request, mm._find_previews))
    controller.active, controller.future = mod.mod_id, future

    @library_mutation
    def change_sidecar(manager):
        sidecar = Path(request.container) / ".d3dxmeta" / "info.json"
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        data["info"]["author"] = "Completed write"
        sidecar.write_text(json.dumps(data), encoding="utf-8")

    try:
        change_sidecar(mm)
        controller._finish(future)
        qtbot.waitUntil(lambda: bool(published), timeout=5000)
        assert mm.get_mod(mod.mod_id).info.author == "Completed write"
        assert len(published) == 1
    finally:
        controller.stop()
        controller.pool.shutdown(wait=True)
        controller.deleteLater()
