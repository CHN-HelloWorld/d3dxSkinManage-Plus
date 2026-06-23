from __future__ import annotations

import errno
import json
import os
import threading
import uuid
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest

from src.core.mod_manager import ModManager
from src.core.mod_package import read_override, write_override
from src.services.gamebanana.models import GbInstallMetadata
from tests.helpers import register_games, reset_game_service


def _archive(path, payload):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Skin/mod.ini", "[Constants]\nx=1\n")
        archive.writestr("Skin/body.buf", payload)
    return path


def _metadata(version, file_id):
    return GbInstallMetadata(
        mod_id=123,
        file_id=file_id,
        file_md5=version,
        file_ts=file_id,
        page_url="https://gamebanana.com/mods/123",
        version=version,
        submitter_id=1,
        submitter_name="Author",
        last_checked_iso="",
    )


@pytest.fixture
def update_case(qapp, temp_data_dir):
    register_games("game-a", "game-b")
    manager = ModManager()
    manager.set_game("game-a")
    character = manager.add_character(name="Character", node_type="character")
    old = _archive(temp_data_dir / "old.zip", b"old body")
    new = _archive(temp_data_dir / "new.zip", b"new body")
    mod = manager.import_mod(str(old), character.id, gb_meta=_metadata("v1", 10))
    assert mod is not None
    mod.name = "My custom name"
    mod.info.description = "Keep my notes"
    mod.info.tags = ["favorite"]
    assert manager.update_mod(mod)
    yield manager, mod, new
    if manager._ini_sync is not None:
        manager._ini_sync.stop()
    reset_game_service()


@pytest.mark.parametrize("deployment_state", ["absent", "active", "parked"])
def test_failed_update_restores_originals_without_history_backup(
    update_case,
    temp_data_dir,
    monkeypatch,
    deployment_state,
):
    from src.core.mod_update_transaction import cleanup_update, commit_update, make_update_plan
    from src.core.mod_update_worker import prepare_update

    manager, old, archive = update_case
    if deployment_state != "absent":
        manager.set_deploy_dir(str(temp_data_dir / "Mods"))
        assert manager.toggle_mod(old.mod_id).is_enabled
        if deployment_state == "parked":
            manager.preset_manager.create_preset(
                "game-a",
                "Keep enabled",
                snapshot={old.node_id: [old.mod_id]},
            )
            assert not manager.toggle_mod(old.mod_id).is_enabled
    manager.stop_ini_sync()
    old = manager.get_mod(old.mod_id)
    body = Path(old.path)
    files = [body, body.parent / ".d3dxmod"]
    if old.deployed_path:
        files.extend(Path(old.deployed_path) / name for name in ("body.buf", "mod.ini"))
    originals = {path: path.read_bytes() for path in files}
    previous_metadata = deepcopy(old.info.custom_data)
    previous_preset = deepcopy(manager.preset_manager.get_current("game-a").to_dict())
    plan = make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    try:
        prepared = prepare_update(plan, lambda event: None)
        monkeypatch.setattr(manager._index, "update_mod", lambda mod: False)
        with pytest.raises(OSError, match="索引"):
            commit_update(manager, plan, prepared)
        restored = manager.get_mod(old.mod_id)
        assert restored.is_enabled == old.is_enabled
        assert restored.deployed_path == old.deployed_path
        assert restored.mod_id == old.mod_id
        assert restored.info.version == "v1"
        assert restored.content_hash == old.content_hash
        assert restored.info.custom_data == previous_metadata
        assert manager.preset_manager.get_current("game-a").to_dict() == previous_preset
        assert not (Path(plan["work_root"]) / "recovery_required").exists()
    finally:
        cleanup_update(plan)
    assert not Path(plan["work_root"]).exists()
    assert not list((temp_data_dir / "data").glob("*/.mod_backups"))
    for path, expected in originals.items():
        assert path.read_bytes() == expected


@pytest.mark.parametrize("failed_restore", ["source", "deployment"])
def test_failed_automatic_restore_preserves_originals_for_recovery(
    update_case,
    temp_data_dir,
    monkeypatch,
    failed_restore,
):
    from src.core.mod_update_transaction import cleanup_update, commit_update, make_update_plan
    from src.core.mod_update_worker import prepare_update
    from src.utils import fs

    manager, old, archive = update_case
    manager.set_deploy_dir(str(temp_data_dir / "Mods"))
    assert manager.toggle_mod(old.mod_id).is_enabled
    manager.stop_ini_sync()
    old = manager.get_mod(old.mod_id)
    body = Path(old.path)
    deployment = Path(old.deployed_path)
    old_body = body.read_bytes()
    old_deployment = (deployment / "body.buf").read_bytes()
    plan = make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    root = Path(plan["work_root"])
    deploy_work = Path(plan["deploy_work"])
    recovery_marker = root / "recovery_required"
    retained_original = root / "rollback" / body.name if failed_restore == "source" else deploy_work / "old_deployment"
    restore_target = body if failed_restore == "source" else deployment
    original_replace = os.replace

    def fail_restore(source, target):
        if Path(fs.normal_path(source)) == retained_original and Path(fs.normal_path(target)) == restore_target:
            raise OSError(errno.EACCES, "simulated restore failure")
        return original_replace(source, target)

    try:
        prepared = prepare_update(plan, lambda event: None)
        with monkeypatch.context() as failures:
            failures.setattr(manager._index, "update_mod", lambda mod: False)
            failures.setattr(os, "replace", fail_restore)
            with pytest.raises(OSError, match="保存更新索引失败"):
                commit_update(manager, plan, prepared)
        manager.stop_ini_sync()
        assert "simulated restore failure" in recovery_marker.read_text(encoding="utf-8")
        cleanup_update(plan)
        assert root.is_dir()
        assert deploy_work.is_dir()
        assert not restore_target.exists()
        assert manager.get_mod(old.mod_id).info.version == "v1"
        if failed_restore == "source":
            assert retained_original.read_bytes() == old_body
            assert (deployment / "body.buf").read_bytes() == old_deployment
        else:
            assert (retained_original / "body.buf").read_bytes() == old_deployment
            assert body.read_bytes() == old_body
    finally:
        manager.stop_ini_sync()
        # 清理临时任务前还原保留的原始文件。
        if retained_original.exists():
            original_replace(fs.native_path(retained_original), fs.native_path(restore_target))
        recovery_marker.unlink(missing_ok=True)
        cleanup_update(plan)
    assert body.read_bytes() == old_body
    assert (deployment / "body.buf").read_bytes() == old_deployment


def test_prepare_does_not_mutate_live_body_and_commit_preserves_user_state(update_case, temp_data_dir):
    from src.core.mod_update_transaction import cleanup_update, commit_update, make_update_plan
    from src.core.mod_update_worker import prepare_update

    manager, old, archive = update_case
    old_bytes = Path(old.path).read_bytes()
    old_marker = (Path(old.path).parent / ".d3dxmod").read_bytes()
    plan = make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    progress = []
    result = prepare_update(plan, progress.append)
    assert Path(old.path).read_bytes() == old_bytes
    assert (Path(old.path).parent / ".d3dxmod").read_bytes() == old_marker
    assert manager.get_mod(old.mod_id).info.version == "v1"
    assert {event["phase"] for event in progress} >= {"verifying", "installing"}

    updated = commit_update(manager, plan, result)
    assert updated.mod_id == old.mod_id
    assert updated.imported_at == old.imported_at == plan["imported_at"]
    assert json.loads((Path(updated.path).parent / ".d3dxmod").read_text())["imported_at"] == old.imported_at
    assert updated.name == "My custom name"
    assert updated.info.description == "Keep my notes"
    assert updated.info.tags == ["favorite"]
    assert updated.info.version == "v2"
    assert manager.get_mod(old.mod_id).info.version == "v2"
    with zipfile.ZipFile(updated.path) as package:
        assert package.read("Skin/body.buf") == b"new body"
    cleanup_update(plan)
    assert not Path(plan["work_root"]).exists()
    assert Path(updated.path).exists()
    assert not list((temp_data_dir / "data").glob("*/.mod_backups"))


def test_game_or_live_content_change_rejects_stale_preparation(update_case):
    from src.core.mod_update_transaction import (
        UpdateContextChanged,
        cleanup_update,
        commit_update,
        make_update_plan,
    )
    from src.core.mod_update_worker import prepare_update

    manager, old, archive = update_case
    before = Path(old.path).read_bytes()
    plan = make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    result = prepare_update(plan, lambda event: None)
    manager.set_game("game-b")
    with pytest.raises(UpdateContextChanged):
        commit_update(manager, plan, result)
    assert Path(old.path).read_bytes() == before
    manager.set_game("game-a")
    write_override(old.path, "mod.ini", "[Constants]\nx=changed_while_preparing\n")
    with pytest.raises(UpdateContextChanged):
        commit_update(manager, plan, result)
    assert Path(old.path).read_bytes() == before
    cleanup_update(plan)


@pytest.mark.parametrize(
    "changed_identity",
    [
        "source",
        "staged_source",
        "deployment",
        "staged_deployment",
    ],
)
def test_changed_metadata_identity_rejects_update_before_moving_live_files(
    update_case, temp_data_dir, changed_identity
):
    from src.core.mod_update_transaction import (
        UpdateContextChanged,
        cleanup_update,
        commit_update,
        make_update_plan,
    )
    from src.core.mod_update_worker import prepare_update

    manager, old, archive = update_case
    manager.set_deploy_dir(str(temp_data_dir / "Mods"))
    enabled = manager.toggle_mod(old.mod_id)
    manager.stop_ini_sync()
    original_body = Path(old.path).read_bytes()
    deployed = Path(enabled.deployed_path)
    plan = make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    try:
        prepared = prepare_update(plan, lambda event: None)
        if changed_identity == "source":
            marker = Path(old.path).parent / ".d3dxmod"
        elif changed_identity == "staged_source":
            marker = Path(prepared["body"]).parent / ".d3dxmod"
        else:
            directory = deployed if changed_identity == "deployment" else Path(prepared["deployment"])
            marker = directory / ".d3dxmod"
        data = json.loads(marker.read_text(encoding="utf-8"))
        identity_data = data
        identity_data["mod_id"] = uuid.uuid4().hex
        marker.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(UpdateContextChanged, match="身份"):
            commit_update(manager, plan, prepared)
        assert Path(old.path).read_bytes() == original_body
        assert (deployed / "body.buf").read_bytes() == b"old body"
        assert manager.get_mod(old.mod_id).info.version == "v1"
    finally:
        cleanup_update(plan)


def test_enabled_update_prepares_deployment_without_removing_old_copy(update_case, temp_data_dir):
    from src.core.mod_update_transaction import cleanup_update, commit_update, make_update_plan
    from src.core.mod_update_worker import prepare_update

    manager, old, archive = update_case
    manager.set_deploy_dir(str(temp_data_dir / "Mods"))
    assert manager.toggle_mod(old.mod_id) is not None
    old = manager.get_mod(old.mod_id)
    target = Path(old.deployed_path)
    assert (target / "body.buf").read_bytes() == b"old body"
    assert write_override(old.path, "mod.ini", "[Constants]\nx=custom\n")
    plan = make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    progress = []
    result = prepare_update(plan, progress.append)
    assert (target / "body.buf").read_bytes() == b"old body"
    assert any(event["phase"] == "extracting" for event in progress)
    updated = commit_update(manager, plan, result)
    assert updated.is_enabled
    assert Path(updated.deployed_path) == target
    assert (target / "body.buf").read_bytes() == b"new body"
    assert read_override(updated.path, "mod.ini") == "[Constants]\nx=custom\n"
    assert (target / "mod.ini").read_text() == "[Constants]\nx=custom\n"
    cleanup_update(plan)
    assert (target / "mod.ini").read_text() == "[Constants]\nx=custom\n"


def test_cleanup_rejects_an_unowned_path(update_case, tmp_path):
    from src.core.mod_update_transaction import cleanup_update, make_update_plan

    manager, old, archive = update_case
    plan = make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    foreign = tmp_path / "unrelated"
    foreign.mkdir()
    sentinel = foreign / "keep.txt"
    sentinel.write_text("keep")
    corrupt = deepcopy(plan)
    corrupt["work_root"] = str(foreign)
    with pytest.raises(ValueError):
        cleanup_update(corrupt)
    assert sentinel.read_text() == "keep"
    cleanup_update(plan)


@pytest.mark.parametrize("key", ["work_root", "deploy_work"])
def test_cleanup_rejects_owned_directory_redirected_outside_task(update_case, tmp_path, key):
    from src.core.mod_update_transaction import cleanup_update, make_update_plan

    manager, old, archive = update_case
    manager.set_deploy_dir(str(tmp_path / "Mods"))
    assert manager.toggle_mod(old.mod_id).is_enabled
    plan = make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    foreign = tmp_path / "unrelated"
    foreign.mkdir()
    sentinel = foreign / "keep.txt"
    sentinel.write_text("keep")
    linked = Path(plan[key])
    if linked.exists():
        linked.rmdir()  # 规划仅创建自己的空工作目录。
    linked.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(foreign), str(linked))
    else:
        linked.symlink_to(foreign, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match=key):
            cleanup_update(plan)
        assert sentinel.read_text() == "keep"
        assert Path(old.path).is_file()
    finally:
        if os.name == "nt":
            linked.rmdir()
        else:
            linked.unlink()
        cleanup_update(plan)


def test_parked_deployment_contains_new_version_when_enabled_again(update_case, temp_data_dir):
    from src.core.mod_update_transaction import cleanup_update, commit_update, make_update_plan
    from src.core.mod_update_worker import prepare_update

    manager, old, archive = update_case
    manager.set_deploy_dir(str(temp_data_dir / "Mods"))
    manager.toggle_mod(old.mod_id)
    current = manager.get_mod(old.mod_id)
    parked = manager._storage.park_deployment(current.deployed_path, current.mod_id)
    assert parked is not None
    current.disable()
    current.remember_deployment(parked)
    manager.update_mod(current)
    from src.models.deployment import DeploymentRecord, DeploymentState

    manager._index.upsert_deployment(DeploymentRecord(current.mod_id, DeploymentState.DISABLED, str(parked)))
    plan = make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    prepared = prepare_update(plan, lambda event: None)
    assert (Path(parked) / "body.buf").read_bytes() == b"old body"
    updated = commit_update(manager, plan, prepared)
    assert not updated.is_enabled
    cleanup_update(plan)
    manager.toggle_mod(updated.mod_id)
    enabled = manager.get_mod(updated.mod_id)
    assert enabled.is_enabled
    assert (Path(enabled.deployed_path) / "body.buf").read_bytes() == b"new body"


def test_periodic_orphan_cleanup_does_not_delete_staged_update(update_case):
    from src.core.mod_update_transaction import cleanup_update, commit_update, make_update_plan
    from src.core.mod_update_worker import prepare_update

    manager, old, archive = update_case
    plan = make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    prepared = prepare_update(plan, lambda event: None)
    assert manager._cleanup_orphaned_managed_artifacts(manager.current_game_id)
    assert Path(prepared["body"]).is_file(), "正常对账不能删除在途更新文件"
    updated = commit_update(manager, plan, prepared)
    cleanup_update(plan)
    assert manager.get_mod(updated.mod_id).info.version == "v2"
    with zipfile.ZipFile(updated.path) as package:
        assert package.read("Skin/body.buf") == b"new body"


def test_editor_save_to_live_deployment_after_prepare_rejects_stale_commit(update_case, temp_data_dir):
    from src.core.mod_update_transaction import UpdateContextChanged, cleanup_update, commit_update, make_update_plan
    from src.core.mod_update_worker import prepare_update

    manager, old, archive = update_case
    manager.set_deploy_dir(str(temp_data_dir / "Mods"))
    manager.toggle_mod(old.mod_id)
    manager.stop_ini_sync()
    current = manager.get_mod(old.mod_id)
    target = Path(current.deployed_path)
    plan = make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    prepared = prepare_update(plan, lambda event: None)
    saved = target / "mod.ini.tmp"
    saved.write_text("[Constants]\nx=late_user_value\n")
    os.replace(saved, target / "mod.ini")
    with pytest.raises(UpdateContextChanged, match="配置"):
        commit_update(manager, plan, prepared)
    cleanup_update(plan)
    assert manager.get_mod(old.mod_id).info.version == "v1"
    assert (target / "body.buf").read_bytes() == b"old body"
    assert (target / "mod.ini").read_text() == "[Constants]\nx=late_user_value\n"


def test_future_override_links_survive_a_storage_extension_change(update_case, tmp_path, monkeypatch):
    from src.core.mod_package import override_path_for, write_container_marker
    from src.core.mod_update_worker import _link_staged_overrides
    from src.utils import fs

    _, old, archive = update_case
    container = tmp_path / "stage"
    container.mkdir()
    body = container / "next.7z"
    body.write_bytes(archive.read_bytes())
    assert write_container_marker(container, body.name, "a" * 40, mod_id=old.mod_id, imported_at=old.imported_at)
    assert write_override(body, "mod.ini", "[Constants]\nx=preserved\n")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    future = Path(old.path).with_suffix(".7z")
    assert override_path_for(future, "mod.ini") is None, "测试需覆盖 live marker 尚未改成新后缀"

    def different_volume(*args, **kwargs):
        raise OSError(errno.EXDEV, "different volumes")

    monkeypatch.setattr(os, "link", different_volume)
    _link_staged_overrides(body, deploy, future)
    # 没有符号链接权限时允许使用副本。
    target = deploy / "mod.ini"
    if target.is_symlink():
        assert os.path.normcase(os.path.normpath(fs.normal_path(os.readlink(target)))) == os.path.normcase(
            str(Path(old.path).parent / ".d3dxmeta" / "ini_overrides" / "mod.ini")
        )
    else:
        assert target.read_text() == "[Constants]\nx=preserved\n"


def test_wrong_game_id_cannot_migrate_a_mod_to_the_current_game(update_case):
    from src.core.mod_update_transaction import UpdateContextChanged, make_update_plan

    manager, old, archive = update_case
    manager.set_game("game-b")
    with pytest.raises(UpdateContextChanged, match="游戏"):
        make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    assert manager.get_mods() == []
    manager.set_game("game-a")
    assert manager.get_mod(old.mod_id).info.version == "v1"


def test_pending_preset_apply_prevents_committing_a_prepared_update(update_case):
    from src.core.mod_update_transaction import UpdateContextChanged, cleanup_update, commit_update, make_update_plan
    from src.core.mod_update_worker import prepare_update
    from src.models.deployment import PresetApplyJournal

    manager, old, archive = update_case
    before = Path(old.path).read_bytes()
    plan = make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    prepared = prepare_update(plan, lambda event: None)
    manager._index.save_journal(PresetApplyJournal("applying", "game-a", "old", "new", "planned", "{}"))
    with pytest.raises(UpdateContextChanged, match="预设"):
        commit_update(manager, plan, prepared)
    cleanup_update(plan)
    assert Path(old.path).read_bytes() == before
    assert manager.get_mod(old.mod_id).info.version == "v1"


def test_shared_physical_source_is_rejected_before_mutating_any_reference(update_case):

    manager, old, archive = update_case
    alias = deepcopy(old)
    alias.mod_id = uuid.uuid4().hex
    before = Path(old.path).read_bytes()
    # 位置索引在写入前拒绝无效别名。
    assert not manager._index.add_mod(alias)
    assert Path(old.path).read_bytes() == before
    assert manager.get_mod(alias.mod_id) is None


def test_update_ownership_check_does_not_scan_unrelated_library(update_case, monkeypatch):
    from src.core.mod_update_transaction import _check_ownership

    manager, old, archive = update_case

    def forbidden(*args, **kwargs):
        raise AssertionError("update ownership performed a full library query")

    monkeypatch.setattr(manager._index, "get_all_mods", forbidden)
    steps = []
    conn = manager._index._db()
    for size in (500, 5000):
        conn.executemany(
            "INSERT OR IGNORE INTO mods(mod_id,game_id,folder_name,path,imported_at) VALUES(?,?,?,?,?)",
            [(f"unrelated-{i}", "game-a", f"unrelated-{i}", "body.zip", "2026") for i in range(size)],
        )
        conn.commit()
        count = [0]

        def tick():
            count[0] += 1
            return 0

        conn.set_progress_handler(tick, 1)
        _check_ownership(manager, old.mod_id, "game-a")
        conn.set_progress_handler(None, 0)
        steps.append(count[0])
    assert steps[1] <= steps[0] + 2


def test_corrupt_download_is_rejected_before_replacing_original(update_case):
    from src.core.mod_update_transaction import cleanup_update, make_update_plan
    from src.core.mod_update_worker import prepare_update

    manager, old, archive = update_case
    before = Path(old.path).read_bytes()
    archive.write_bytes(b"this is not a ZIP archive")
    plan = make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    with pytest.raises(ValueError, match="校验"):
        prepare_update(plan, lambda event: None)
    cleanup_update(plan)
    assert Path(old.path).read_bytes() == before
    assert manager.get_mod(old.mod_id).info.version == "v1"


def test_commit_uses_prepared_ini_baseline_without_reading_ini_contents(update_case, temp_data_dir, monkeypatch):
    import src.core.ini_sync as ini_sync
    from src.core.mod_update_transaction import cleanup_update, commit_update, make_update_plan
    from src.core.mod_update_worker import prepare_update

    manager, old, archive = update_case
    manager.set_deploy_dir(str(temp_data_dir / "Mods"))
    manager.toggle_mod(old.mod_id)
    plan = make_update_plan(manager, old.mod_id, archive, _metadata("v2", 11))
    prepared = prepare_update(plan, lambda event: None)
    reads = []
    original = ini_sync._hash_file

    def observe_read(path):
        if threading.current_thread() is threading.main_thread():
            reads.append(path)
        return original(path)

    monkeypatch.setattr(ini_sync, "_hash_file", observe_read)
    updated = commit_update(manager, plan, prepared)
    assert reads == [], "更新提交阶段不能在 GUI 线程重新读取所有 INI 内容"
    monkeypatch.setattr(ini_sync, "_hash_file", original)
    target = Path(updated.deployed_path)
    edited = target / "mod.ini.tmp"
    edited.write_bytes(b"[Constants]\nx=after_update\n")
    os.replace(edited, target / "mod.ini")
    manager._ini_sync.reconcile_mod(updated.mod_id)
    assert read_override(updated.path, "mod.ini") == "[Constants]\nx=after_update\n"
    cleanup_update(plan)
