"""验证整目录复制或迁移后，游戏配置、部署和命名外观共同使用当前应用目录。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import uuid
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from src.core import migoto_user_ini as mui
from src.core.config import Config
from src.core.game_config_store import GameConfigStore
from src.core.library_schema import register_connection
from src.core.mod_manager import ModManager
from src.core.mod_package import build_local_manifest, override_path_for, seed_ini_overrides, write_sidecar
from src.models.mod import Mod
from tests.helpers import apply_preset, reset_game_service

_GAME_ID = "portable_game"
_MOD_INI = "[Constants]\nglobal persist $swapvar = 0\n[KeySwap]\nkey = ALT H\n"
_EDITED_INI = _MOD_INI.replace("ALT H", "ALT J")
_KEY = r"$\mods\sampleskin\merged.ini\swapvar"


def _bind_app_root(root: Path, monkeypatch) -> None:
    """重绑已导入及后续消费者的路径入口，模拟另一台设备重新启动应用。"""
    from src.utils import paths

    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    replacements = {
        "get_app_dir": lambda: root,
        "get_data_dir": lambda: root / "data",
        "get_config_dir": lambda: root / "config",
    }
    for name, replacement in replacements.items():
        monkeypatch.setattr(paths, name, replacement)
    for module_name, module in list(sys.modules.items()):
        if module is not None and module_name.startswith("src."):
            for name, replacement in replacements.items():
                if hasattr(module, name):
                    monkeypatch.setattr(module, name, replacement)


def _reset_runtime() -> None:
    """清空持有上一位置的配置与管理器单例，索引及监听由所有者预先关闭。"""
    Config._instance = None
    ModManager._instance = None
    reset_game_service()


def _close_manager(manager: ModManager) -> None:
    """释放文件监听及数据库句柄，保证复制的是完整、静止的应用数据。"""
    manager.stop_ini_sync()
    if manager._variants is not None:
        manager.variants.stop_watch()
    manager._index.close()


def _write_user_ini(path: Path, value: str) -> None:
    """模拟加载器将当前外观整文件写回运行时偏好。"""
    path.write_text(f"[Constants]\n{_KEY} = {value}\n", encoding="utf-8")


def _user_values(path: Path) -> dict[str, str]:
    """按加载器格式读取持久变量，验证真正写回的外观状态。"""
    return mui.parse_persisted_values(path.read_text(encoding="mbcs", errors="surrogateescape"))


def _file_snapshot(root: Path) -> dict[str, tuple[bytes | str, int]]:
    """记录文件内容、链接本身和修改时间，不解引用可能已失效的旧链接。"""
    snapshot = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            content = os.readlink(path)
        elif path.is_file():
            content = path.read_bytes()
        else:
            continue
        snapshot[path.relative_to(root).as_posix()] = (content, path.lstat().st_mtime_ns)
    return snapshot


def _write_legacy_absolute_paths(root: Path, mod_id: str) -> None:
    """在已关闭的临时库中还原旧版绝对路径，模拟搬迁前没有运行过新版程序。"""
    resolved_root = root.resolve(strict=True)
    config_file = root / "config" / "games.json"
    database_file = root / "data" / "mod_index.db"
    assert config_file.resolve(strict=True).is_relative_to(resolved_root)
    assert database_file.resolve(strict=True).is_relative_to(resolved_root)
    config = json.loads(config_file.read_text(encoding="utf-8"))
    game = next(game for game in config["games"] if game["id"] == _GAME_ID)
    game.update(
        exe_path=str(resolved_root / "Sample.exe"),
        migoto_path=str(resolved_root / "loader"),
        mods_path=str(resolved_root / "loader" / "Mods"),
    )
    config_file.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    with closing(sqlite3.connect(database_file)) as connection, connection:
        register_connection(connection)
        updated = connection.execute(
            "UPDATE deployment_records SET actual_path = ? WHERE mod_id = ?",
            (str(resolved_root / "loader" / "Mods" / "SampleSkin"), mod_id),
        )
        assert updated.rowcount == 1


@pytest.fixture
def portable_environment(temp_data_dir, monkeypatch):
    """创建真实归档、部署链接与命名外观，并集中释放搬迁前后的所有管理器。"""
    root = temp_data_dir / "original"
    _bind_app_root(root, monkeypatch)
    _reset_runtime()
    loader = root / "loader"
    (loader / "Mods").mkdir(parents=True)
    (loader / "d3dx.ini").write_text(
        "[Include]\ninclude_recursive = Mods\nexclude_recursive = DISABLED*\n", encoding="utf-8"
    )
    (root / "Sample.exe").write_bytes(b"sample executable")
    assert GameConfigStore().set_games(
        [
            {
                "id": _GAME_ID,
                "name": "示例游戏",
                "name_en": "SampleGame",
                "resource_dir": "sample",
                "exe_path": str(root / "Sample.exe"),
                "migoto_path": str(loader),
                "mods_path": str(loader / "Mods"),
            }
        ]
    )
    manager = ModManager()
    managers = [manager]
    try:
        manager.set_game(_GAME_ID)
        manager.set_deploy_dir(manager._game_service.get_current_game().mods_path)
        character = manager.add_character(name="示例角色", node_type="character")
        container = manager._get_character_library(character.id) / "SampleSkin"
        container.mkdir(parents=True)
        body = container / "SampleSkin.zip"
        with zipfile.ZipFile(body, "w") as archive:
            archive.writestr("SampleSkin/merged.ini", _MOD_INI)
        (container / ".d3dxmod").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "kind": "d3dxSkinManage-Plus.mod-container",
                    "body": body.name,
                    "body_type": "archive",
                    "content_hash": Mod.compute_sha1(str(body)),
                    "mod_id": uuid.uuid4().hex,
                    "imported_at": "2026-01-01T00:00:00",
                }
            ),
            encoding="utf-8",
        )
        manager.rebuild_index_from_filesystem(_GAME_ID)
        mod = manager.get_mods(node_id=character.id)[0]
        assert write_sidecar(mod.path, build_local_manifest(mod.to_dict()))
        assert seed_ini_overrides(mod.path)
        preset = manager.preset_manager.create_preset(_GAME_ID, "保存的组合", snapshot={character.id: [mod.mod_id]})
        apply_preset(manager.preset_manager, preset.id)
        assert manager._index.get_mod(mod.mod_id).is_enabled
        assert manager._ini_sync is not None
        deployed_ini = loader / "Mods" / "SampleSkin" / "merged.ini"
        if not deployed_ini.is_symlink():
            pytest.skip("当前文件系统无法建立迁移场景需要的符号链接")
        assert not Path(os.readlink(deployed_ini)).is_absolute()
        assert deployed_ini.resolve(strict=True) == override_path_for(mod.path, "merged.ini").resolve(strict=True)
        _write_user_ini(loader / "d3dx_user.ini", "4")
        variant = manager.variants.save_variant(mod.mod_id, "保存的外观")
        assert preset.mod_variants[mod.mod_id] == variant.variant_id
        yield {
            "root": root,
            "managers": managers,
            "mod_id": mod.mod_id,
            "preset_id": preset.id,
            "variant_id": variant.variant_id,
        }
    finally:
        for current in managers:
            _close_manager(current)
        _reset_runtime()


@pytest.mark.parametrize("keep_original", [True, False], ids=["old_copy_present", "old_location_unavailable"])
def test_relocated_environment_restores_saved_variant_without_writing_old_copy(
    portable_environment, temp_data_dir, monkeypatch, keep_original
):
    """重启后从新根读取配置并完成外观恢复、启停和组合重应用，旧文件始终保持原样。"""
    env = portable_environment
    old_root = env["root"]
    new_root = temp_data_dir / "relocated"
    _close_manager(env["managers"][0])
    if not keep_original:
        _write_legacy_absolute_paths(old_root, env["mod_id"])
    # 模拟升级前遗留的绝对链接；先停监听，避免复制前的自动核对提前修复它。
    original_ini = old_root / "loader" / "Mods" / "SampleSkin" / "merged.ini"
    assert original_ini.is_symlink()
    original_override = original_ini.resolve(strict=True)
    original_ini.unlink()
    original_ini.symlink_to(original_override)
    assert Path(os.readlink(original_ini)).is_absolute()
    shutil.copytree(old_root, new_root, symlinks=True)
    preserved_root = old_root
    if not keep_original:
        preserved_root = temp_data_dir / "offline"
        # Windows 目录移动前确认两个绝对路径均在本用例临时目录内。
        sandbox = temp_data_dir.resolve()
        assert old_root.resolve().is_relative_to(sandbox)
        assert preserved_root.resolve().is_relative_to(sandbox)
        old_root.rename(preserved_root)
    old_files = _file_snapshot(preserved_root)
    user_ini = new_root / "loader" / "d3dx_user.ini"
    _write_user_ini(user_ini, "8")

    _bind_app_root(new_root, monkeypatch)
    _reset_runtime()
    manager = ModManager()
    env["managers"].append(manager)
    manager.set_game(_GAME_ID)
    game = manager._game_service.get_current_game()
    assert Path(game.exe_path) == new_root / "Sample.exe"
    assert Path(game.migoto_path) == new_root / "loader"
    assert Path(game.mods_path) == new_root / "loader" / "Mods"
    manager.set_deploy_dir(game.mods_path)
    assert manager._storage.get_deploy_dir() == new_root / "loader" / "Mods"
    assert manager.variants.user_ini_path() == user_ini
    mod = manager._index.get_mod(env["mod_id"])
    assert mod is not None and mod.is_enabled
    assert Path(mod.path).is_relative_to(new_root)
    deployed = new_root / "loader" / "Mods" / "SampleSkin"
    assert Path(mod.deployed_path) == deployed

    # 首次注册后立即编辑，避免重新部署时重建链接掩盖旧绝对目标。
    (deployed / "merged.ini").write_text(_EDITED_INI, encoding="utf-8")
    assert manager._ini_sync is not None
    manager._ini_sync.reconcile_mod(mod.mod_id)
    override = override_path_for(mod.path, "merged.ini")
    assert override.read_text(encoding="utf-8") == _EDITED_INI

    report = manager.variants.collect(trigger="portable_environment")
    assert report.attributed[mod.mod_id] == {_KEY: "8"}
    outcome = manager.variants.apply_variant(mod.mod_id, env["variant_id"])
    assert outcome.applied == (mod.mod_id,)
    assert outcome.skipped == () and outcome.errors == ()
    assert _user_values(user_ini) == {_KEY: "4"}

    disabled = manager.toggle_mod(mod.mod_id)
    assert disabled is not None and not disabled.is_enabled
    assert not deployed.exists()
    _write_user_ini(user_ini, "9")
    restore_outcomes = []
    restore = manager.variants.restore

    def record_restore(*args, **kwargs):
        """仅记录真实恢复调用的结果，保证基础启用没有通过跳过外观掩盖路径错误。"""
        result = restore(*args, **kwargs)
        restore_outcomes.append(result)
        return result

    monkeypatch.setattr(manager.variants, "restore", record_restore)
    enabled = manager.toggle_mod(mod.mod_id)
    assert enabled is not None and enabled.is_enabled
    assert len(restore_outcomes) == 1
    assert restore_outcomes[0].applied == (mod.mod_id,)
    assert restore_outcomes[0].skipped == () and restore_outcomes[0].errors == ()
    assert _user_values(user_ini) == {_KEY: "4"}

    _write_user_ini(user_ini, "6")
    apply_preset(manager.preset_manager, env["preset_id"])
    assert manager.preset_manager.get_current(_GAME_ID).id == env["preset_id"]
    assert not manager.preset_manager.has_pending_apply(_GAME_ID)
    assert _user_values(user_ini) == {_KEY: "4"}

    _close_manager(manager)
    assert _file_snapshot(preserved_root) == old_files
    if not keep_original:
        assert not old_root.exists()
