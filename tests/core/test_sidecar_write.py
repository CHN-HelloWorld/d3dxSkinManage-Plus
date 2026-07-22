from __future__ import annotations

from pathlib import Path

import pytest

from src.core.mod_manager import ModManager
from src.core.mod_package import read_sidecar, write_container_marker
from src.models.mod import AgeRating, Mod, ModStatus
from tests.helpers import registered_games as registered_games


@pytest.fixture
def mm(temp_data_dir):
    m = ModManager()
    m.set_game("game_a")
    return m


def _make_mod_dir(parent: Path, name: str) -> Path:
    d = parent / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "mod.ini").write_text("[Constants]\n", encoding="utf-8")
    return d


def test_character_breadcrumb_root_to_leaf(mm):
    parent = mm.add_character(name="示例游戏甲", node_type="category")
    child = mm.add_character(name="示例角色丁", node_type="character", parent_id=parent.id)
    assert mm._character_breadcrumb(child.id) == ["示例游戏甲", "示例角色丁"]
    assert mm._character_breadcrumb("") == []


def test_match_character_chain_resolves_leaf(mm):
    parent = mm.add_character(name="示例游戏甲", node_type="category")
    child = mm.add_character(name="示例角色丁", node_type="character", parent_id=parent.id)
    assert mm._match_character_chain(["示例游戏甲", "示例角色丁"]) == child.id
    assert mm._match_character_chain(["示例游戏甲", "不存在"]) == ""
    assert mm._match_character_chain([]) == ""


def test_write_mod_sidecar_folder(mm):
    char = mm.add_character(name="示例角色丁", node_type="character")
    container = mm._get_character_library(char.id) / "Delta_Qipao"
    mod_dir = _make_mod_dir(container, "Delta_Qipao")
    assert write_container_marker(container, mod_dir.name, "a" * 40, imported_at="2026-10-05T10:00:00")
    mod = Mod(
        game_id="game_a",
        name="示例角色丁·旗袍",
        folder_name="Delta_Qipao",
        path=str(mod_dir),
        node_id=char.id,
        status=ModStatus.ENABLED,
    )
    mod.info.age_rating = AgeRating.X
    mod.info.tags = ["旗袍"]

    assert mm._write_mod_sidecar(mod) is True
    sc = read_sidecar(mod_dir)
    assert sc is not None
    assert sc["schema"] == 2
    assert sc["name"] == "示例角色丁·旗袍"
    assert sc["info"]["age_rating"] == "X"
    assert sc["info"]["tags"] == ["旗袍"]
    assert "classification" not in sc["local"]
    assert "status_hint" not in sc["local"]
    assert sc["previews"] == []  # 不带预览图


def test_write_mod_sidecar_best_effort_on_missing_path(mm):
    mod = Mod(game_id="game_a", name="N", folder_name="N", path=str(mm._get_library_root() / "ghost"))
    assert mm._write_mod_sidecar(mod) is False  # 路径不存在 → False，不抛异常
