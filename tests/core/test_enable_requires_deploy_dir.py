from __future__ import annotations

import pytest

from src.core.mod_manager import ModManager
from tests.helpers import apply_preset, register_games, reset_game_service, scan_and_sync
from tests.helpers import registered_games as registered_games


@pytest.fixture
def mm(temp_data_dir):
    register_games("game_a")
    m = ModManager()
    m.set_game("game_a")
    yield m
    m.stop_ini_sync()
    reset_game_service()


def _drop_mod(mm: ModManager, name: str = "A"):
    """创建并纳管角色目录中的外部 Mod，返回登记结果。"""
    char = mm.add_character(name="示例角色丁", node_type="character")
    lib = mm._get_character_library(char.id)
    mod_dir = lib / name
    mod_dir.mkdir(parents=True, exist_ok=True)
    (mod_dir / "mod.ini").write_text(f"[Constants]\nx={name}\n", encoding="utf-8")
    assert scan_and_sync(mm) == 1
    return mm.get_mods()[0]


def test_enable_refused_without_deploy_dir(mm):
    mod = _drop_mod(mm)
    assert mm.has_deploy_dir() is False

    assert mm.toggle_mod(mod.mod_id) is None, "没配部署目录时不得启用"

    after = mm.get_mod(mod.mod_id)
    assert after.is_enabled is False
    assert not after.deployed_path, "拒绝启用时不得把源路径记成部署路径"


def test_enable_allowed_once_deploy_dir_configured(mm, tmp_path):
    mod = _drop_mod(mm)
    mm.set_deploy_dir(str(tmp_path / "Mods"))
    assert mm.has_deploy_dir() is True

    assert mm.toggle_mod(mod.mod_id) is not None
    assert mm.get_mod(mod.mod_id).is_enabled is True


def test_disable_still_allowed_without_deploy_dir(mm, tmp_path):
    mod = _drop_mod(mm)
    mm.set_deploy_dir(str(tmp_path / "Mods"))
    assert mm.toggle_mod(mod.mod_id) is not None
    mm.set_deploy_dir("")

    assert mm.toggle_mod(mod.mod_id) is not None
    assert mm.get_mod(mod.mod_id).is_enabled is False


def test_finalize_enable_refused_without_deploy_dir(mm):
    mod = _drop_mod(mm)

    assert mm.finalize_enable(mod.mod_id, True) is None
    assert mm.get_mod(mod.mod_id).is_enabled is False


def test_apply_preset_without_deploy_dir_changes_nothing(mm):
    mod = _drop_mod(mm)
    pm = mm.preset_manager
    target = pm.create_preset("game_a", "t", snapshot={mod.node_id: [mod.mod_id]})

    assert apply_preset(pm, target.id) == []
    assert mm.get_mod(mod.mod_id).is_enabled is False


def test_switch_to_empty_preset_without_deploy_dir_succeeds(mm):
    """未配置部署目录时，仍可执行不涉及文件操作的预设切换。"""
    _drop_mod(mm)
    pm = mm.preset_manager
    empty = pm.create_preset("game_a", "空预设")

    assert apply_preset(pm, empty.id) == []
    assert pm.get_current("game_a").id == empty.id, "当前预设必须真的切过去"
