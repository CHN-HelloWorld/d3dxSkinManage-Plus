"""用真实 Windows 文件占用验证插件导入撤销和持久恢复边界。"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer

from src.core.mod_intake import ModIntakeService
from src.plugin_sdk import ModImportRequest
from src.plugin_sdk.facades import ModFacade
from src.utils import fs
from tests.plugins.test_library_write_api import source_archive, wait_result
from tests.plugins.test_operations import library as library
from tests.plugins.test_operations import service_for as service_for

pytestmark = pytest.mark.skipif(os.name != "nt", reason="需要 Windows 文件共享删除语义")


@contextmanager
def cancelled_import_with_lock(manager, original, service, tmp_path, qtbot, monkeypatch, *, during_copy=False):
    """在真实复制或准备完成边界持有新归档，允许事件循环继续处理取消。"""
    source = source_archive(tmp_path)
    source_bytes = source.read_bytes()
    release, entered = threading.Event(), threading.Event()
    state = SimpleNamespace(container=None, body=None, marker=b"", source=source, source_bytes=source_bytes)
    category = Path(original.path).parent.parent
    prepare, copy_path = ModIntakeService.prepare, fs.copy_path

    def pause(container, body):
        """冻结新容器所在阶段，直到测试已获得会阻止删除的文件句柄。"""
        state.container, state.body = Path(container), Path(body)
        marker = state.container / ".d3dxmod"
        state.marker = marker.read_bytes() if marker.exists() else b""
        entered.set()
        if not release.wait(5):
            raise TimeoutError("测试未释放导入准备")

    def paused_prepare(self, *args, **kwargs):
        """只暂停真实准备结果，保留格式、复制和身份校验流程。"""
        prepared = prepare(self, *args, **kwargs)
        pause(prepared.container_path, prepared.body_path)
        return prepared

    def paused_copy(source_path, target_path, **kwargs):
        """在库内容器首个数据块写入后暂停，覆盖准备函数尚未返回的取消。"""
        target = Path(target_path)
        if target.parent.parent == category and target != Path(original.path):
            report = kwargs.get("progress_cb")

            def copied(fraction):
                """转发真实复制进度，并在文件仍打开时建立取消边界。"""
                if fraction > 0 and not entered.is_set():
                    pause(target.parent, target)
                if report is not None:
                    report(fraction)

            kwargs["progress_cb"] = copied
        return copy_path(source_path, target_path, **kwargs)

    with monkeypatch.context() as isolated:
        if during_copy:
            isolated.setattr(fs, "copy_path", paused_copy)
        else:
            isolated.setattr(ModIntakeService, "prepare", paused_prepare)
        facade = ModFacade(manager, frozenset({"mods:write"}), operations=service, owner="cleanup-test")
        state.task = facade.import_mod(ModImportRequest(str(source), original.node_id))
        state.handle = None
        try:
            qtbot.waitUntil(entered.is_set, timeout=10000)
            state.handle = open(state.body, "rb")
            state.task.cancel()
            release.set()
            yield state
        finally:
            release.set()
            if state.handle is not None:
                state.handle.close()


def remaining_container(state):
    """按本轮独有身份寻找仍完整的容器，允许其已被移入同卷隔离目录。"""
    identity = json.loads(state.marker)["mod_id"]
    for marker in state.container.parent.rglob(".d3dxmod"):
        if json.loads(marker.read_bytes()).get("mod_id") == identity:
            return marker.parent
    raise AssertionError("没有找到应保留的容器身份")


def restart_manager(manager, service):
    """重新打开真实索引及管理器，模拟仅持久化文件能跨越的进程边界。"""
    from src.core.mod_manager import ModManager

    game_id = manager.current_game_id
    assert service.shutdown()
    manager.stop_ini_sync()
    manager._index.close()
    ModManager._instance = None
    restarted = ModManager()
    restarted.set_game(game_id)
    return restarted


@pytest.mark.parametrize("during_copy", [False, True])
def test_cancel_retries_after_temporary_file_lock(library, service_for, tmp_path, qtbot, monkeypatch, during_copy):
    manager, original = library
    service = service_for(manager)
    with cancelled_import_with_lock(
        manager, original, service, tmp_path, qtbot, monkeypatch, during_copy=during_copy
    ) as state:
        timer = QTimer()
        timer.setSingleShot(True)
        released = []

        def unlock():
            """由界面事件循环释放占用，同时证明重试未阻塞界面。"""
            state.handle.close()
            released.append(True)

        timer.timeout.connect(unlock)
        timer.start(150)
        try:
            result = wait_result(qtbot, state.task)
            assert released, "任务应在后台等待短暂占用解除"
            assert not state.container.exists()
            assert not list(
                category for category in state.container.parent.rglob("*.zip") if category != Path(original.path)
            )
            assert not result.ok
            assert not service.busy
            assert state.source.read_bytes() == state.source_bytes
            assert len(manager.get_mods()) == 1
        finally:
            timer.stop()


def test_persistent_lock_preserves_identity_and_reports_remaining_path(
    library, service_for, tmp_path, qtbot, monkeypatch
):
    manager, original = library
    service = service_for(manager)
    with cancelled_import_with_lock(manager, original, service, tmp_path, qtbot, monkeypatch) as state:
        result = wait_result(qtbot, state.task)
        identity = json.loads(state.marker)["mod_id"]
        remaining = []
        for marker in state.container.parent.rglob(".d3dxmod"):
            if json.loads(marker.read_bytes()).get("mod_id") == identity:
                remaining.append(marker.parent)
                assert marker.read_bytes() == state.marker
        assert remaining, "持续占用时应保留完整身份标记"
        assert any(str(path) in result.error for path in remaining), "结果必须明确指出尚待清理的绝对路径"
        assert not service.busy
        assert state.source.read_bytes() == state.source_bytes
        assert len(manager.get_mods()) == 1


@pytest.mark.parametrize("during_copy", [False, True])
def test_restart_protects_cancelled_import_and_next_import_recovers_it(
    library, service_for, tmp_path, qtbot, monkeypatch, during_copy
):
    manager, original = library
    service = service_for(manager)
    with cancelled_import_with_lock(
        manager, original, service, tmp_path, qtbot, monkeypatch, during_copy=during_copy
    ) as state:
        result = wait_result(qtbot, state.task)
        assert not result.ok
        cancelled_path = state.container
        restarted = restart_manager(manager, service)
        try:
            assert restarted.is_library_path_protected(cancelled_path)
            assert all(
                not Path(item["path"]).is_relative_to(cancelled_path)
                for item in restarted._discover_library_mods("plugin-test")
            )
            restarted.rebuild_index_from_filesystem("plugin-test")
            assert [mod.mod_id for mod in restarted.get_mods()] == [original.mod_id]
            assert state.source.read_bytes() == state.source_bytes
        finally:
            state.handle.close()
    facade = ModFacade(restarted, frozenset({"mods:write"}), operations=service_for(restarted), owner="retry-test")
    try:
        imported = wait_result(qtbot, facade.import_mod(ModImportRequest(str(state.source), original.node_id)))
        assert imported.ok, imported.error
        assert len(restarted.get_mods()) == 2
        assert not restarted.is_library_path_protected(cancelled_path)
        assert not list((tmp_path / "data/recovery/import-cleanup").glob("*.json"))
        archives = list(cancelled_path.parent.rglob("*.zip"))
        assert len(archives) == 2
        assert state.source.read_bytes() == state.source_bytes
    finally:
        restarted.stop_ini_sync()


@pytest.mark.parametrize("changed", ["directory", "marker", "unknown_file"])
def test_retry_preserves_replaced_or_modified_pending_container(
    library, service_for, tmp_path, qtbot, monkeypatch, changed
):
    manager, original = library
    service = service_for(manager)
    with cancelled_import_with_lock(manager, original, service, tmp_path, qtbot, monkeypatch) as state:
        wait_result(qtbot, state.task)
        container = remaining_container(state)
    if changed == "directory":
        saved = tmp_path / "preserved-cancelled-container"
        container.rename(saved)
        container.mkdir()
        (container / ".d3dxmod").write_bytes(state.marker)
        (container / "user-data.txt").write_text("应保留的其他数据", encoding="utf-8")
    elif changed == "marker":
        marker = json.loads(state.marker)
        marker["mod_id"] = "f" * 32
        (container / ".d3dxmod").write_text(json.dumps(marker), encoding="utf-8")
    else:
        (container / ".d3dxmeta" / "user-data.txt").write_text("应保留的其他数据", encoding="utf-8")
    before = {str(path.relative_to(container)): path.read_bytes() for path in container.rglob("*") if path.is_file()}
    facade = ModFacade(manager, frozenset({"mods:write"}), operations=service, owner="retry-test")
    result = wait_result(qtbot, facade.import_mod(ModImportRequest(str(state.source), original.node_id)))
    assert not result.ok and str(container) in result.error
    assert {
        str(path.relative_to(container)): path.read_bytes() for path in container.rglob("*") if path.is_file()
    } == before
    assert len(manager.get_mods()) == 1
    assert state.source.read_bytes() == state.source_bytes


def test_restart_never_deletes_mod_committed_before_receipt_cleanup(library, service_for, tmp_path, qtbot, monkeypatch):
    manager, original = library
    service = service_for(manager)
    source = source_archive(tmp_path)
    facade = ModFacade(manager, frozenset({"mods:write"}), operations=service, owner="committed-test")

    def interrupted_cleanup(self, prepared):
        """模拟索引已提交、后台尚未移除恢复凭据时退出，保留真实导入结果。"""
        raise OSError("测试模拟已提交清理中断")

    with monkeypatch.context() as isolated:
        isolated.setattr(ModIntakeService, "cleanup_committed", interrupted_cleanup)
        result = wait_result(qtbot, facade.import_mod(ModImportRequest(str(source), original.node_id)))
    assert result.succeeded_ids and not result.ok
    imported = manager.get_mod(result.succeeded_ids[0])
    body = Path(imported.path)
    before = body.read_bytes()
    restarted = restart_manager(manager, service)
    try:
        assert body.read_bytes() == before
        assert restarted.get_mod(imported.mod_id) is not None
        later = ModFacade(restarted, frozenset({"mods:write"}), operations=service_for(restarted))
        duplicate = wait_result(qtbot, later.import_mod(ModImportRequest(str(source), original.node_id)))
        assert duplicate.ok and duplicate.skipped_ids == (imported.mod_id,)
        assert body.read_bytes() == before
        assert not list((tmp_path / "data/recovery/import-cleanup").glob("*.json"))
    finally:
        restarted.stop_ini_sync()


def test_missing_index_record_cannot_authorize_deleting_a_committed_import(
    library, service_for, tmp_path, qtbot, monkeypatch
):
    manager, original = library
    service = service_for(manager)
    source = source_archive(tmp_path)
    facade = ModFacade(manager, frozenset({"mods:write"}), operations=service, owner="committed-test")

    def interrupted_cleanup(self, prepared):
        """在真实提交后保留凭据，模拟清理失败或进程在此阶段退出。"""
        raise OSError("测试模拟已提交清理中断")

    with monkeypatch.context() as isolated:
        isolated.setattr(ModIntakeService, "cleanup_committed", interrupted_cleanup)
        result = wait_result(qtbot, facade.import_mod(ModImportRequest(str(source), original.node_id)))
    assert result.succeeded_ids
    imported = manager.get_mod(result.succeeded_ids[0])
    body = Path(imported.path)
    before = body.read_bytes()
    # 仅移除索引记录以模拟索引损坏后的不完整恢复，真实资源及恢复凭据均保持原样。
    manager._index._db().execute("DELETE FROM mods WHERE mod_id=?", (imported.mod_id,))
    manager._index._db().commit()
    restarted = restart_manager(manager, service)
    try:
        later_source = source_archive(tmp_path, "later.zip")
        later = ModFacade(restarted, frozenset({"mods:write"}), operations=service_for(restarted))
        result = wait_result(qtbot, later.import_mod(ModImportRequest(str(later_source), original.node_id)))
        assert body.exists() and body.read_bytes() == before, "索引缺失不能证明已有资源未提交"
        assert not result.ok and str(body.parent) in result.error
        assert list((tmp_path / "data/recovery/import-cleanup").glob("*.json"))
    finally:
        restarted.stop_ini_sync()


def test_commit_intent_is_durable_before_index_insertion(library, service_for, tmp_path, qtbot, monkeypatch):
    manager, original = library
    source = source_archive(tmp_path)
    insert = manager._index.add_mod
    observed = []

    def observed_insert(mod, **kwargs):
        """在真实索引写入边界读取磁盘凭据，防止提交意向被移到事务成功之后。"""
        receipts = (tmp_path / "data/recovery/import-cleanup").glob("*.json")
        observed.append(
            any(
                data["mod_id"] == mod.mod_id and data.get("commit_started") is True
                for data in (json.loads(path.read_bytes()) for path in receipts)
            )
        )
        return insert(mod, **kwargs)

    monkeypatch.setattr(manager._index, "add_mod", observed_insert)
    facade = ModFacade(manager, frozenset({"mods:write"}), operations=service_for(manager))
    result = wait_result(qtbot, facade.import_mod(ModImportRequest(str(source), original.node_id)))
    assert result.ok, result.error
    assert observed == [True]


def test_shutdown_timeout_does_not_wait_indefinitely_for_locked_cleanup(
    library, service_for, tmp_path, qtbot, monkeypatch
):
    manager, original = library
    service = service_for(manager)
    with cancelled_import_with_lock(manager, original, service, tmp_path, qtbot, monkeypatch) as state:
        started = time.monotonic()
        assert not service.shutdown(timeout_ms=10)
        assert time.monotonic() - started < 0.5
        result = wait_result(qtbot, state.task)
        assert not result.ok and not service.busy
        assert remaining_container(state).is_dir()


def test_default_intake_does_not_create_recovery_receipts(library, tmp_path):
    manager, original = library
    source = source_archive(tmp_path)
    imported = manager.import_mod(source_path=str(source), node_id=original.node_id)
    assert imported is not None
    assert not (tmp_path / "data/recovery/import-cleanup").exists()


def test_cancel_can_remove_own_copy_of_read_only_source(library, tmp_path):
    manager, original = library
    source = source_archive(tmp_path)
    before = source.read_bytes()
    os.chmod(source, stat.S_IREAD)
    intake = ModIntakeService(manager)
    plan = intake.plan_request(
        source_path=source,
        game_id=manager.current_game_id,
        node_id=original.node_id,
        planning_snapshot=intake.planning_snapshot(manager.current_game_id),
        recoverable_cleanup=True,
    )
    try:
        prepared = intake.prepare(plan)
        intake.rollback(prepared)
        assert not prepared.container_path.exists()
        assert not list((tmp_path / "data/recovery/import-cleanup").glob("*.json"))
        assert source.read_bytes() == before
        assert not source.stat().st_mode & stat.S_IWRITE
    finally:
        os.chmod(source, stat.S_IREAD | stat.S_IWRITE)
