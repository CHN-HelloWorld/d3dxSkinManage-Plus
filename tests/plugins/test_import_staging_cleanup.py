"""验证无扩展名归档暂存副本的只读清理、持久恢复和来源保护。"""

from __future__ import annotations

import json
import os
import stat
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.mod_intake import ModIntakeService
from src.plugin_sdk import ModImportRequest
from src.plugin_sdk.facades import ModFacade
from src.utils import fs
from tests.plugins.test_import_cleanup_recovery import restart_manager
from tests.plugins.test_library_write_api import source_archive, wait_result
from tests.plugins.test_operations import library as library
from tests.plugins.test_operations import service_for as service_for

pytestmark = pytest.mark.skipif(os.name != "nt", reason="需要 Windows 文件共享删除和只读属性语义")


@contextmanager
def locked_staging(manager, original, service, tmp_path, qtbot, monkeypatch, *, cancel=False):
    """在真实暂存复制边界持有副本句柄，取消场景在首个数据块写入时暂停。"""
    source = source_archive(tmp_path, "a" * 40)
    entered, release = threading.Event(), threading.Event()
    state = SimpleNamespace(source=source, before=source.read_bytes(), body=None, handle=None)
    copy_path = fs.copy_path

    def pause(path):
        """只冻结一次真实复制，等待宿主线程建立共享删除限制。"""
        state.body = Path(path)
        entered.set()
        if not release.wait(5):
            raise TimeoutError("测试未释放暂存复制")

    def observed_copy(source_path, target_path, **kwargs):
        """保持真实文件操作，仅在本次独立暂存副本处设置测试同步点。"""
        target = Path(target_path)
        staging = Path(source_path) == source and target.parent.name.startswith("plugin-import-")
        if staging and cancel:
            report = kwargs.get("progress_cb")

            def copied(fraction):
                """在写入中暂停一次，并保留原复制进度通知。"""
                if fraction > 0 and not entered.is_set():
                    pause(target)
                if report is not None:
                    report(fraction)

            kwargs["progress_cb"] = copied
        result = copy_path(source_path, target_path, **kwargs)
        if staging and not cancel:
            pause(target)
        return result

    with monkeypatch.context() as isolated:
        isolated.setattr(fs, "copy_path", observed_copy)
        facade = ModFacade(manager, frozenset({"mods:write"}), operations=service, owner="staging-test")
        state.task = facade.import_mod(ModImportRequest(str(source), original.node_id))
        try:
            qtbot.waitUntil(entered.is_set, timeout=10000)
            state.handle = open(state.body, "rb")
            if cancel:
                state.task.cancel()
            release.set()
            yield state
        finally:
            release.set()
            if state.handle is not None:
                state.handle.close()


def test_read_only_extensionless_source_leaves_no_staging_copy(library, service_for, tmp_path, qtbot):
    manager, original = library
    source = source_archive(tmp_path, "a" * 40)
    before = source.read_bytes()
    os.chmod(source, stat.S_IREAD)
    facade = ModFacade(manager, frozenset({"mods:write"}), operations=service_for(manager))
    try:
        result = wait_result(qtbot, facade.import_mod(ModImportRequest(str(source), original.node_id)))
        assert result.ok, result.error
        assert source.read_bytes() == before
        assert not source.stat().st_mode & stat.S_IWRITE
        assert Path(manager.get_mod(result.succeeded_ids[0]).path).read_bytes() == before
        assert not list((tmp_path / "data/temp").rglob("source.zip"))
        assert not list((tmp_path / "data/recovery/import-cleanup").glob("*.json"))
    finally:
        # 仅恢复隔离测试数据的属性，便于 pytest 后续删除自己的临时目录。
        for path in tmp_path.rglob("*"):
            if path.is_file():
                os.chmod(path, path.stat().st_mode | stat.S_IWRITE)


@pytest.mark.parametrize("cancel", [False, True])
def test_locked_staging_survives_restart_and_retries_without_deleting_imported_mod(
    library, service_for, tmp_path, qtbot, monkeypatch, cancel
):
    manager, original = library
    service = service_for(manager)
    restarted = None
    imported = None
    try:
        with locked_staging(manager, original, service, tmp_path, qtbot, monkeypatch, cancel=cancel) as state:
            result = wait_result(qtbot, state.task)
            assert not result.ok
            assert result.cancel_requested == cancel
            if cancel:
                assert not result.succeeded_ids
                assert len(manager.get_mods()) == 1
            else:
                assert result.succeeded_ids
                imported = manager.get_mod(result.succeeded_ids[0])
                assert Path(imported.path).read_bytes() == state.before
            remaining = list((tmp_path / "data/temp").rglob("source.zip"))
            assert len(remaining) == 1
            assert str(remaining[0].parent) in result.error
            assert not service.busy
            assert list((tmp_path / "data/recovery/import-cleanup").glob("*.json"))
            restarted = restart_manager(manager, service)
            assert restarted.is_library_path_protected(remaining[0])
            assert state.source.read_bytes() == state.before
            if imported is not None:
                assert restarted.get_mod(imported.mod_id) is not None
                assert Path(imported.path).read_bytes() == state.before
        later = ModFacade(restarted, frozenset({"mods:write"}), operations=service_for(restarted))
        result = wait_result(qtbot, later.import_mod(ModImportRequest(str(state.source), original.node_id)))
        assert result.ok, result.error
        assert len(restarted.get_mods()) == 2
        if imported is not None:
            assert result.skipped_ids == (imported.mod_id,)
            assert Path(imported.path).read_bytes() == state.before
        else:
            assert result.succeeded_ids
        assert not list((tmp_path / "data/temp").rglob("source.zip"))
        assert not list((tmp_path / "data/recovery/import-cleanup").glob("*.json"))
        assert state.source.read_bytes() == state.before
    finally:
        if restarted is not None:
            restarted.stop_ini_sync()


def test_staging_ownership_is_durable_before_copying_source(library, service_for, tmp_path, qtbot, monkeypatch):
    manager, original = library
    source = source_archive(tmp_path, "a" * 40)
    observed = []
    copy_path = fs.copy_path

    def observed_copy(source_path, target_path, **kwargs):
        """在真正复制开始前核对持久凭据，不接受只保存在工作线程中的清理记录。"""
        target = Path(target_path)
        if Path(source_path) == source and target.parent.name.startswith("plugin-import-"):
            receipts = (tmp_path / "data/recovery/import-cleanup").glob("*.json")
            observed.append(
                any(
                    Path(data["root"]) / data["relative"] == target.parent
                    for data in (json.loads(path.read_bytes()) for path in receipts)
                )
            )
        return copy_path(source_path, target_path, **kwargs)

    monkeypatch.setattr(fs, "copy_path", observed_copy)
    facade = ModFacade(manager, frozenset({"mods:write"}), operations=service_for(manager))
    result = wait_result(qtbot, facade.import_mod(ModImportRequest(str(source), original.node_id)))
    assert result.ok, result.error
    assert observed == [True]


def test_cancel_reports_locked_library_and_staging_paths(library, service_for, tmp_path, qtbot, monkeypatch):
    manager, original = library
    source = source_archive(tmp_path, "a" * 40)
    entered, release = threading.Event(), threading.Event()
    prepared_paths = []
    prepare = ModIntakeService.prepare

    def paused_prepare(self, *args, **kwargs):
        """在真实库内准备完成后暂停，以便同时锁定库内归档和输入暂存副本。"""
        prepared = prepare(self, *args, **kwargs)
        prepared_paths.append(prepared.body_path)
        entered.set()
        if not release.wait(5):
            raise TimeoutError("测试未释放库内导入准备")
        return prepared

    monkeypatch.setattr(ModIntakeService, "prepare", paused_prepare)
    facade = ModFacade(manager, frozenset({"mods:write"}), operations=service_for(manager))
    task = facade.import_mod(ModImportRequest(str(source), original.node_id))
    try:
        qtbot.waitUntil(entered.is_set, timeout=10000)
        stage_body = next((tmp_path / "data/temp").glob("plugin-import-*/source.zip"))
        with ExitStack() as handles:
            handles.enter_context(open(stage_body, "rb"))
            handles.enter_context(open(prepared_paths[0], "rb"))
            task.cancel()
            release.set()
            result = wait_result(qtbot, task)
            assert result.cancel_requested and not result.ok
            assert str(stage_body.parent) in result.error
            assert str(prepared_paths[0].parent) in result.error
            assert len(list((tmp_path / "data/recovery/import-cleanup").glob("*.json"))) == 2
            assert len(manager.get_mods()) == 1
    finally:
        release.set()


def test_modified_pending_staging_copy_is_preserved(library, service_for, tmp_path, qtbot, monkeypatch):
    manager, original = library
    service = service_for(manager)
    with locked_staging(manager, original, service, tmp_path, qtbot, monkeypatch) as state:
        result = wait_result(qtbot, state.task)
        assert result.succeeded_ids and not result.ok
        imported = manager.get_mod(result.succeeded_ids[0])
    remaining = next((tmp_path / "data/temp").rglob("source.zip"))
    remaining.write_bytes(b"modified staging copy")
    facade = ModFacade(manager, frozenset({"mods:write"}), operations=service)
    retry = wait_result(qtbot, facade.import_mod(ModImportRequest(str(state.source), original.node_id)))
    assert not retry.ok and str(remaining.parent) in retry.error
    assert remaining.read_bytes() == b"modified staging copy"
    assert Path(imported.path).read_bytes() == state.before
    assert state.source.read_bytes() == state.before
