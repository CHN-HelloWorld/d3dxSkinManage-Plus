from __future__ import annotations

import pytest

from src.core.config import Config
from src.core.mod_manager import ModManager
from src.core.mod_storage import ModStorage
from tests.helpers import register_games, reset_game_service, scan_and_sync
from tests.helpers import registered_games as registered_games


@pytest.fixture
def mm(temp_data_dir, tmp_path):
    register_games("game_a")
    Config().set("import.format", "zip")
    m = ModManager()
    m.set_game("game_a")
    m.set_deploy_dir(str(tmp_path / "Mods"))
    yield m
    m.stop_ini_sync()
    reset_game_service()


def _enabled_mod(mm: ModManager, name: str = "A"):
    char = mm.add_character(name="示例角色丁", node_type="character")
    lib = mm._get_character_library(char.id)
    mod_dir = lib / name
    mod_dir.mkdir(parents=True, exist_ok=True)
    (mod_dir / "mod.ini").write_text(f"[Constants]\nx={name}\n", encoding="utf-8")
    assert scan_and_sync(mm) == 1
    mod = mm.get_mods()[0]
    assert mm.toggle_mod(mod.mod_id) is not None
    return mm.get_mod(mod.mod_id)


def test_clear_removes_contents_and_disables_mods(mm, tmp_path):
    mod = _enabled_mod(mm)
    deploy_root = tmp_path / "Mods"
    (deploy_root / "用户自己放的").mkdir()  # 目录里不止程序部署的东西
    assert list(deploy_root.iterdir())

    disabled, error = mm.clear_deploy_dir_contents("game_a", deploy_root)

    assert error == ""
    assert disabled == 1
    assert list(deploy_root.iterdir()) == [], "目录内容必须清空"
    after = mm.get_mod(mod.mod_id)
    assert after.is_enabled is False
    assert after.deployed_path == ""
    assert mm.preset_manager.get_current("game_a").character_mods == {}


def test_clear_keeps_source_body_intact(mm, tmp_path):
    mod = _enabled_mod(mm)
    source = mod.path

    mm.clear_deploy_dir_contents("game_a", tmp_path / "Mods")

    from pathlib import Path

    assert Path(source).exists(), "清空部署目录不得动库内的源本体"


def test_clear_failure_leaves_database_untouched(mm, tmp_path, monkeypatch):
    mod = _enabled_mod(mm)
    monkeypatch.setattr(ModStorage, "_remove_deploy", classmethod(lambda cls, p: False))

    disabled, error = mm.clear_deploy_dir_contents("game_a", tmp_path / "Mods")

    assert disabled == 0
    assert "未能删除" in error
    assert mm.get_mod(mod.mod_id).is_enabled is True, "删不掉时状态必须原样保留"
