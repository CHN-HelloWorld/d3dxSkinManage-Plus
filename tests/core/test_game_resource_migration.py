"""验证资源目录迁移同步重绑受管部署链接，并在失败时恢复路径与部署记录。"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.mod_identity import new_mod_id, write_deployment_identity
from src.core.mod_package import override_path_for, write_container_marker
from src.core.mod_storage import ModStorage
from src.models.deployment import DeploymentRecord, DeploymentState
from src.models.mod import Mod
from src.utils import fs
from tests.core.test_library_node_operations import manager as manager

_INI = "[KeySwap]\nkey = ALT H\n"
_EDITED_INI = "[KeySwap]\nkey = ALT J\n"


@pytest.fixture
def migration_case(manager, tmp_path, monkeypatch):
    """使用真实索引、容器标记和部署存储，隔离部署目录及监听启动。"""
    manager._storage = ModStorage()
    old_root = manager._index.paths.roots["g"].parent

    def unexpected_registration(*args, **kwargs):
        """拒绝在游戏配置尚未保存时重新登记旧资源路径。"""
        pytest.fail("资源目录迁移不得重新启动 INI 同步")

    monkeypatch.setattr(manager, "_register_ini_sync", unexpected_registration)
    monkeypatch.setattr(manager, "_register_existing_deployments", unexpected_registration)
    return SimpleNamespace(
        manager=manager, old_root=old_root, new_root=old_root.with_name("RenamedGame"), root=tmp_path
    )


def _deployment(case, name: str, *, state=DeploymentState.ACTIVE, mode="symlink", inside=False):
    """创建带真实身份的归档 Mod、INI 覆盖和活动或停放部署，按需使用不同文件关联方式。"""
    container = case.old_root / "mods" / name
    container.mkdir(parents=True)
    body = container / "body.zip"
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("settings.ini", _INI)
    mod = Mod(
        mod_id=new_mod_id(),
        game_id="g",
        name=name,
        folder_name=name,
        path=str(body),
        content_hash=Mod.compute_sha1(body),
        imported_at="2026-10-31T10:00:00",
    )
    write_container_marker(container, body.name, mod.content_hash, mod_id=mod.mod_id, imported_at=mod.imported_at)
    assert case.manager._index.add_mod(mod)
    override = override_path_for(body, "settings.ini")
    assert override is not None
    override.parent.mkdir(parents=True)
    override.write_text(_INI, encoding="utf-8")
    root = case.old_root / "deployments" if inside else case.root / "runtime" / "Mods"
    deployed_name = name if state == DeploymentState.ACTIVE else f"DISABLED_{mod.mod_id[:12]}_{name}"
    deployed = root / deployed_name
    deployed.mkdir(parents=True)
    write_deployment_identity(deployed, mod.mod_id, mod.content_hash)
    ini = deployed / "settings.ini"
    foreign = None
    if mode == "copy":
        ini.write_text(_EDITED_INI, encoding="utf-8")
    elif mode == "hardlink":
        try:
            os.link(override, ini)
        except OSError as error:
            pytest.skip(f"当前文件系统无法创建硬链接：{error}")
    else:
        target = override
        if mode == "foreign":
            foreign = case.old_root.with_name("g-other") / f"{name}.ini"
            foreign.parent.mkdir(parents=True, exist_ok=True)
            foreign.write_text(_EDITED_INI, encoding="utf-8")
            target = foreign
        try:
            ini.symlink_to(target)
        except OSError as error:
            pytest.skip(f"当前文件系统无法创建符号链接：{error}")
    case.manager._index.upsert_deployment(DeploymentRecord(mod.mod_id, state, str(deployed)))
    return SimpleNamespace(
        mod=mod, body=body, override=override, deployed=deployed, ini=ini, foreign=foreign, state=state
    )


def _assert_original(case, deployments) -> None:
    """检查迁移失败后资源根、索引位置、部署状态及文件内容都保持原状。"""
    assert case.old_root.is_dir()
    assert not case.new_root.exists()
    assert case.manager._index.paths.roots["g"] == case.old_root / "mods"
    for item in deployments:
        assert case.manager._index.paths.body_path(item.mod.mod_id) == item.body
        record = case.manager._index.get_deployment(item.mod.mod_id)
        assert record.actual_path == str(item.deployed)
        assert record.state is item.state
        assert item.ini.read_text(encoding="utf-8") == _INI


@pytest.mark.parametrize("state", [DeploymentState.ACTIVE, DeploymentState.DISABLED])
def test_resource_migration_retargets_managed_symlinks_without_changing_state(migration_case, state):
    case = migration_case
    item = _deployment(case, "Sample", state=state)

    assert case.manager.migrate_game_resource_dir("g", "g", "RenamedGame") == "RenamedGame"

    source = case.new_root / item.override.relative_to(case.old_root)
    assert item.ini.is_symlink()
    assert item.ini.read_text(encoding="utf-8") == _INI
    assert item.ini.resolve() == source.resolve()
    source.write_text(_EDITED_INI, encoding="utf-8")
    assert item.ini.read_text(encoding="utf-8") == _EDITED_INI
    record = case.manager._index.get_deployment(item.mod.mod_id)
    assert record.state is state
    assert record.actual_path == str(item.deployed)
    assert not case.old_root.exists()
    assert case.manager._ini_sync is None


@pytest.mark.parametrize("mode", ["copy", "hardlink", "foreign"])
def test_resource_migration_preserves_independent_and_foreign_files(migration_case, mode):
    case = migration_case
    item = _deployment(case, "Preserved", mode=mode)
    before = item.ini.read_bytes()
    identity = item.ini.lstat().st_ino
    link_target = os.readlink(item.ini) if item.ini.is_symlink() else None

    assert case.manager.migrate_game_resource_dir("g", "g", "RenamedGame") == "RenamedGame"

    assert item.ini.read_bytes() == before
    assert item.ini.lstat().st_ino == identity
    if link_target is not None:
        assert os.readlink(item.ini) == link_target
    if mode == "hardlink":
        source = case.new_root / item.override.relative_to(case.old_root)
        source.write_text(_EDITED_INI, encoding="utf-8")
        assert item.ini.read_text(encoding="utf-8") == _EDITED_INI


@pytest.mark.parametrize("state", [DeploymentState.ACTIVE, DeploymentState.DISABLED])
def test_resource_migration_rebases_deployment_records_inside_resource_root(migration_case, state):
    case = migration_case
    item = _deployment(case, "Nested", state=state, inside=True)

    assert case.manager.migrate_game_resource_dir("g", "g", "RenamedGame") == "RenamedGame"

    deployed = case.new_root / item.deployed.relative_to(case.old_root)
    record = case.manager._index.get_deployment(item.mod.mod_id)
    assert record.actual_path == str(deployed)
    assert record.state is state
    assert (deployed / "settings.ini").read_text(encoding="utf-8") == _INI


def test_resource_migration_rejects_foreign_deployment_identity(migration_case):
    case = migration_case
    item = _deployment(case, "Sample")
    write_deployment_identity(item.deployed, new_mod_id())

    assert case.manager.migrate_game_resource_dir("g", "g", "RenamedGame") == "g"

    _assert_original(case, [item])


def test_resource_migration_rename_failure_keeps_source_and_index(migration_case, monkeypatch):
    case = migration_case
    item = _deployment(case, "Sample")

    def locked_directory(source, destination):
        """模拟资源根被外部句柄占用，拒绝实际目录移动。"""
        raise PermissionError("资源目录被占用")

    monkeypatch.setattr(os, "rename", locked_directory)
    assert case.manager.migrate_game_resource_dir("g", "g", "RenamedGame") == "g"
    _assert_original(case, [item])


def test_resource_migration_link_failure_rolls_back_all_paths(migration_case, monkeypatch):
    case = migration_case
    first = _deployment(case, "First")
    second = _deployment(case, "Second", state=DeploymentState.DISABLED)
    before = [os.readlink(item.ini) for item in (first, second)]
    replace = os.replace
    failed = False

    def fail_second_link(source, destination):
        """让第二条部署链接的原子替换失败一次，保留真实回滚文件操作。"""
        nonlocal failed
        if Path(fs.normal_path(destination)) == second.ini and not failed:
            failed = True
            raise PermissionError("第二条部署链接被占用")
        return replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_second_link)
    assert case.manager.migrate_game_resource_dir("g", "g", "RenamedGame") == "g"
    assert failed
    _assert_original(case, [first, second])
    assert [os.readlink(item.ini) for item in (first, second)] == before


def test_resource_migration_database_failure_restores_moved_files(migration_case):
    case = migration_case
    item = _deployment(case, "Nested", inside=True)
    case.manager._index._db().execute(
        "CREATE TRIGGER reject_deployment_rebase BEFORE UPDATE OF actual_path ON deployment_records "
        "BEGIN SELECT RAISE(ABORT, 'deployment path update rejected'); END"
    )
    case.manager._index._db().commit()

    assert case.manager.migrate_game_resource_dir("g", "g", "RenamedGame") == "g"

    _assert_original(case, [item])


def test_resource_migration_reports_actual_path_if_rollback_is_blocked(migration_case, monkeypatch):
    """链接替换与目录回滚同时失败时报告实际资源位置，不返回与磁盘状态不符的旧名称。"""
    case = migration_case
    item = _deployment(case, "Sample")
    rename, replace = os.rename, os.replace
    failed = False

    def fail_link(source, destination):
        """仅阻止新链接安装，允许还原已移开的原始链接。"""
        nonlocal failed
        if Path(fs.normal_path(destination)) == item.ini and not failed:
            failed = True
            raise PermissionError("部署链接被占用")
        return replace(source, destination)

    def fail_rollback(source, destination):
        """允许资源根移往新位置，但模拟外部句柄阻止移动回来。"""
        if Path(fs.normal_path(source)) == case.new_root:
            raise PermissionError("资源目录回滚被占用")
        return rename(source, destination)

    monkeypatch.setattr(os, "replace", fail_link)
    monkeypatch.setattr(os, "rename", fail_rollback)
    with pytest.raises(OSError) as failed_migration:
        case.manager.migrate_game_resource_dir("g", "g", "RenamedGame")

    assert failed_migration.value.actual_resource_dir == "RenamedGame"
    assert Path(failed_migration.value.actual_path) == case.new_root
    assert case.new_root.is_dir() and not case.old_root.exists()
    assert case.manager._index.paths.roots["g"] == case.new_root / "mods"
