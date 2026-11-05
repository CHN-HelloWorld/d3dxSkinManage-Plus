"""用真实资源库验证插件导入、分类创建和任务边界。"""

from __future__ import annotations

import hashlib
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from src import plugin_sdk as sdk
from src.core.mod_intake import ModIntakeService
from src.core.mod_package import read_sidecar
from src.plugin_sdk.facades import GameFacade, ModFacade
from tests.plugins.test_operations import library as library
from tests.plugins.test_operations import service_for as service_for


def source_archive(tmp_path: Path, name: str = "source.zip") -> Path:
    """生成与库内基线不同的真实 Mod 归档，供复制及摘要验证。"""
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("outfit/mod.ini", "[Constants]\nglobal $selection = 7\n")
    return path


def wait_result(qtbot, task):
    """等待宿主事件循环完成任务，不在业务回调中阻塞。"""
    qtbot.waitUntil(lambda: task.done, timeout=10000)
    return task.result


def test_import_preserves_metadata_previews_and_source(library, service_for, tmp_path, qtbot, monkeypatch):
    manager, original = library
    service = service_for(manager)
    facade = ModFacade(manager, frozenset({"mods:write"}), operations=service, owner="migration")
    source = source_archive(tmp_path)
    original_bytes = source.read_bytes()
    preview = tmp_path / "preview.png"
    Image.new("RGB", (12, 12), "blue").save(preview)
    main_thread = threading.get_ident()
    preparation_threads, commit_threads, events = [], [], []
    prepare, commit = ModIntakeService.prepare, ModIntakeService.commit

    def prepare_in_background(self, *args, **kwargs):
        """记录实际文件准备所在的线程，并保留真实导入行为。"""
        preparation_threads.append(threading.get_ident())
        return prepare(self, *args, **kwargs)

    def commit_on_owner(self, *args, **kwargs):
        """记录数据库提交所在的线程，并保留真实索引事务。"""
        commit_threads.append(threading.get_ident())
        return commit(self, *args, **kwargs)

    monkeypatch.setattr(ModIntakeService, "prepare", prepare_in_background)
    monkeypatch.setattr(ModIntakeService, "commit", commit_on_owner)
    manager.add_event_listener(lambda name, **data: events.append((name, data)))
    request = sdk.ModImportRequest(
        str(source),
        original.node_id,
        name="收藏外观",
        author="示例作者",
        description="保留的说明",
        rating="P",
        tags=("轻装", "蓝色"),
        preview_paths=(str(preview),),
        source_url="https://example.invalid/mod/7",
        expected_sha1=hashlib.sha1(original_bytes).hexdigest(),
    )
    task = facade.import_mod(request)
    assert not task.done
    result = wait_result(qtbot, task)
    assert result.ok, result.error
    assert len(result.succeeded_ids) == 1
    imported = manager.get_mod(result.succeeded_ids[0])
    assert imported.name == "收藏外观"
    assert imported.info.author == "示例作者"
    assert imported.info.description == "保留的说明"
    assert imported.info.age_rating.value == "P"
    assert imported.info.tags == ["轻装", "蓝色"]
    assert imported.info.url == "https://example.invalid/mod/7"
    assert not imported.is_enabled
    assert imported.node_id == original.node_id
    assert source.read_bytes() == original_bytes
    assert preview.exists()
    assert imported.preview_images
    copied = Path(imported.preview_images[0])
    assert copied != preview and copied.read_bytes() == preview.read_bytes()
    assert copied.is_relative_to(Path(imported.path).parent)
    sidecar = read_sidecar(imported.path)
    assert sidecar["info"]["author"] == "示例作者"
    assert sidecar["info"]["tags"] == ["轻装", "蓝色"]
    assert sidecar["local"]["preview_references"][0]["scope"] == "container"
    assert preparation_threads and all(value != main_thread for value in preparation_threads)
    assert commit_threads == [main_thread]
    assert [data["mod_id"] for name, data in events if name == "mod_imported"] == [imported.mod_id]


def test_duplicate_import_returns_existing_identity_without_overwriting(library, service_for, tmp_path, qtbot):
    manager, original = library
    facade = ModFacade(manager, frozenset({"mods:write"}), operations=service_for(manager), owner="migration")
    source = source_archive(tmp_path)
    first = wait_result(qtbot, facade.import_mod(sdk.ModImportRequest(str(source), original.node_id, name="原名")))
    assert first.ok
    second = wait_result(
        qtbot,
        facade.import_mod(sdk.ModImportRequest(str(source), original.node_id, name="不应覆盖", author="其他作者")),
    )
    assert second.ok, second.error
    assert second.skipped_ids == first.succeeded_ids
    assert second.succeeded_ids == ()
    assert manager.get_mod(first.succeeded_ids[0]).name == "原名"
    assert len(manager.get_mods()) == 2
    assert source.exists()


def test_cancel_after_preparation_rolls_back_new_container(library, service_for, tmp_path, qtbot, monkeypatch):
    manager, original = library
    service = service_for(manager)
    source = source_archive(tmp_path)
    prepared_paths = []
    prepare = ModIntakeService.prepare
    release = threading.Event()

    def blocked_prepare(self, *args, **kwargs):
        """让取消发生在真实文件准备完成、尚未提交索引的边界。"""
        prepared = prepare(self, *args, **kwargs)
        prepared_paths.append(prepared.container_path)
        if not release.wait(5):
            raise TimeoutError("test did not release preparation")
        return prepared

    monkeypatch.setattr(ModIntakeService, "prepare", blocked_prepare)
    facade = ModFacade(manager, frozenset({"mods:write"}), operations=service, owner="migration")
    task = facade.import_mod(sdk.ModImportRequest(str(source), original.node_id))
    try:
        qtbot.waitUntil(lambda: bool(prepared_paths), timeout=10000)
        task.cancel()
    finally:
        release.set()
    result = wait_result(qtbot, task)
    assert not result.ok and not result.succeeded_ids
    assert result.cancel_requested
    assert len(manager.get_mods()) == 1
    assert not prepared_paths[0].exists()
    assert source.exists()


def test_archive_checksum_mismatch_leaves_source_and_library_unchanged(library, service_for, tmp_path, qtbot):
    manager, original = library
    source = source_archive(tmp_path)
    facade = ModFacade(manager, frozenset({"mods:write"}), operations=service_for(manager))
    result = wait_result(
        qtbot, facade.import_mod(sdk.ModImportRequest(str(source), original.node_id, expected_sha1="0" * 40))
    )
    assert not result.ok and result.error
    assert len(manager.get_mods()) == 1
    assert source.exists()


def test_extensionless_archive_uses_its_header_without_changing_legacy_file(library, service_for, tmp_path, qtbot):
    manager, original = library
    source = source_archive(tmp_path, "a" * 40)
    before = source.read_bytes()
    facade = ModFacade(manager, frozenset({"mods:write"}), operations=service_for(manager))
    result = wait_result(
        qtbot,
        facade.import_mod(
            sdk.ModImportRequest(
                str(source), original.node_id, name="无后缀收藏", expected_sha1=hashlib.sha1(before).hexdigest()
            )
        ),
    )
    assert result.ok, result.error
    imported = manager.get_mod(result.succeeded_ids[0])
    with zipfile.ZipFile(imported.path) as archive:
        assert archive.read("outfit/mod.ini") == b"[Constants]\nglobal $selection = 7\n"
    assert source.read_bytes() == before
    assert not list((tmp_path / "data/temp").glob("plugin-import-*"))


def test_missing_preview_does_not_leave_a_partial_import(library, service_for, tmp_path, qtbot):
    manager, original = library
    source = source_archive(tmp_path)
    facade = ModFacade(manager, frozenset({"mods:write"}), operations=service_for(manager))
    result = wait_result(
        qtbot,
        facade.import_mod(
            sdk.ModImportRequest(str(source), original.node_id, preview_paths=(str(tmp_path / "missing.png"),))
        ),
    )
    assert not result.ok and result.error
    assert len(manager.get_mods()) == 1
    assert source.exists()
    category = Path(original.path).parent.parent
    assert [path.name for path in category.iterdir() if path.is_dir() and path.name != ".d3dxmeta"] == ["skin"]


def test_request_freezes_mutable_metadata_sequences():
    tags = ["蓝色"]
    previews = ["preview.png"]
    request = sdk.ModImportRequest("source.zip", "node", tags=tags, preview_paths=previews)
    tags.append("变更")
    previews.clear()
    assert request.tags == ("蓝色",)
    assert request.preview_paths == ("preview.png",)


def test_successful_import_cleans_temporary_files_off_the_owner_thread(
    library, service_for, tmp_path, qtbot, monkeypatch
):
    from src.core.import_format import PreparedImportSource

    manager, original = library
    service = service_for(manager)
    facade = ModFacade(manager, frozenset({"mods:write"}), operations=service)
    cleanup_threads = []
    cleanup = PreparedImportSource.cleanup
    main_thread = threading.get_ident()

    def cleanup_with_thread(self):
        """记录实际临时树清理所在的线程，并执行原有资源释放。"""
        cleanup_threads.append(threading.get_ident())
        return cleanup(self)

    monkeypatch.setattr(PreparedImportSource, "cleanup", cleanup_with_thread)
    source = source_archive(tmp_path, "b" * 40)
    result = wait_result(qtbot, facade.import_mod(sdk.ModImportRequest(str(source), original.node_id)))
    assert result.ok, result.error
    assert cleanup_threads and all(value != main_thread for value in cleanup_threads)
    assert not service.busy
    assert source.exists()


def test_queued_import_cannot_follow_game_switch(library, service_for, tmp_path, qtbot):
    manager, original = library
    facade = ModFacade(manager, frozenset({"mods:write"}), operations=service_for(manager))
    task = facade.import_mod(sdk.ModImportRequest(str(source_archive(tmp_path)), original.node_id))
    manager.set_game("other")
    result = wait_result(qtbot, task)
    assert not result.ok and result.game_id == "plugin-test"
    assert manager.get_mods() == []
    manager.set_game("plugin-test")
    assert len(manager.get_mods()) == 1


def test_category_creation_reuses_only_matching_parent_and_type(library, service_for, qtbot):
    manager, original = library
    facade = GameFacade(manager, frozenset({"mods:write"}), operations=service_for(manager), owner="migration")
    first = wait_result(qtbot, facade.create_category("服饰", parent_id=original.node_id))
    assert first.ok and first.node_id
    reused = wait_result(qtbot, facade.create_category("服饰", parent_id=original.node_id, exist_ok=True))
    assert reused.ok and reused.node_id == first.node_id
    conflict = wait_result(
        qtbot, facade.create_category("服饰", parent_id=original.node_id, node_type="character", exist_ok=True)
    )
    assert not conflict.ok
    root = wait_result(qtbot, facade.create_category("服饰"))
    assert root.ok and root.node_id != first.node_id
    assert manager.get_character(first.node_id).parent_id == original.node_id


def test_new_write_apis_enforce_permissions_thread_and_lifetime(library, service_for, tmp_path, qtbot):
    manager, original = library
    service = service_for(manager)
    request = sdk.ModImportRequest(str(source_archive(tmp_path)), original.node_id)
    forbidden = ModFacade(manager, frozenset({"mods:read"}), operations=service)
    with pytest.raises(sdk.PermissionDenied):
        forbidden.import_mod(request)
    forbidden_game = GameFacade(manager, frozenset({"mods:read"}), operations=service)
    with pytest.raises(sdk.PermissionDenied):
        forbidden_game.create_category("服饰")
    allowed = ModFacade(manager, frozenset({"mods:write"}), operations=service)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(sdk.PluginError):
            pool.submit(allowed.import_mod, request).result(timeout=3)
    allowed._close()
    assert not wait_result(qtbot, allowed.import_mod(request)).ok
    assert not service.busy
