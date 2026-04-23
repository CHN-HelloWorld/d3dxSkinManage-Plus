from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.mod_manager import ModManager
from src.core.mod_package import InvalidManagedMetadataError
from src.utils.constants import CATEGORY_MARKER_FILE
from tests.helpers import register_games, reset_game_service, scan_and_sync
from tests.helpers import registered_games as registered_games

CATEGORY_SCHEMA_VERSION = 2
CATEGORY_KIND = "d3dxSkinManage-Plus.category"


def _make_mod_dir(parent: Path, name: str) -> Path:
    d = parent / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "mod.ini").write_text("[Constants]\n", encoding="utf-8")
    return d


@pytest.fixture
def mm(temp_data_dir):
    """先注册游戏再创建管理器，使重建和资源目录解析使用有效游戏配置。"""
    register_games("game_a")
    m = ModManager()
    m.set_game("game_a")
    yield m
    reset_game_service()


def _current_marker(**overrides) -> dict:
    marker = {
        "schema": CATEGORY_SCHEMA_VERSION,
        "kind": CATEGORY_KIND,
        "id": "category-id",
        "node_type": "character",
        "name": "角色",
        "name_en": "Character",
        "sort_order": 0,
        "thumbnail": None,
    }
    marker.update(overrides)
    return marker


def test_add_character_writes_full_marker(mm):
    parent = mm.add_character(name="示例游戏甲", node_type="category")
    child = mm.add_character(name="示例角色丁", node_type="character", parent_id=parent.id)
    marker = mm._get_character_library(child.id) / CATEGORY_MARKER_FILE
    assert marker.is_file()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data == {
        "schema": CATEGORY_SCHEMA_VERSION,
        "kind": CATEGORY_KIND,
        "id": child.id,
        "node_type": "character",
        "name": "示例角色丁",
        "name_en": "",
        "sort_order": 0,
        "thumbnail": None,
    }


@pytest.mark.parametrize(
    ("name", "node_type"),
    [("", "character"), ("角色", "legacy")],
)
def test_add_character_does_not_write_invalid_category_marker(mm, name, node_type):
    assert mm.add_character(name=name, node_type=node_type) is None
    assert mm.get_characters("game_a") == []
    assert not list(mm._get_library_root("game_a").rglob(CATEGORY_MARKER_FILE))


def test_refresh_markers_writes_current_marker_without_legacy_type(mm):
    char = mm.add_character(name="示例角色己", node_type="character")
    marker = mm._get_character_library(char.id) / CATEGORY_MARKER_FILE

    mm._refresh_markers_from_db("game_a")
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["node_type"] == "character"
    assert data["name"] == "示例角色己"
    assert "type" not in data


def test_refresh_markers_rewrites_invalid_marker_from_healthy_db(mm):
    char = mm.add_character(name="示例角色己", node_type="character")
    marker = mm._get_character_library(char.id) / CATEGORY_MARKER_FILE
    marker.write_text(json.dumps({"type": "category", "id": char.id}), encoding="utf-8")

    assert mm._refresh_markers_from_db("game_a") == 1
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "schema": CATEGORY_SCHEMA_VERSION,
        "kind": CATEGORY_KIND,
        "id": char.id,
        "node_type": "character",
        "name": "示例角色己",
        "name_en": "",
        "sort_order": 0,
        "thumbnail": None,
    }


def test_read_category_marker_missing_returns_none(tmp_path):
    assert ModManager._read_category_marker(tmp_path) is None


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {**_current_marker(), "kind": "legacy.category"},
        {key: value for key, value in _current_marker().items() if key != "kind"},
        {**_current_marker(), "id": ""},
        {**_current_marker(), "id": 123},
        {key: value for key, value in _current_marker().items() if key != "id"},
        {**_current_marker(), "node_type": "legacy"},
        {**_current_marker(), "node_type": 1},
        {**_current_marker(), "node_type": []},
        {key: value for key, value in _current_marker().items() if key != "node_type"},
        {**_current_marker(), "name": 1},
        {key: value for key, value in _current_marker().items() if key != "name"},
    ],
)
def test_read_category_marker_rejects_broken_identity(tmp_path, payload):
    """后仍判废的四项：kind（身份）、id（唯一真相源）、node_type（角色还是类别）、
    name（要拿它拼物理路径，属安全校验）。"""
    marker = tmp_path / CATEGORY_MARKER_FILE
    marker.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(InvalidManagedMetadataError):
        ModManager._read_category_marker(tmp_path)


@pytest.mark.parametrize(
    ("payload", "expect"),
    [
        ({**_current_marker(), "schema": True}, {}),
        ({**_current_marker(), "schema": 1}, {}),
        ({**_current_marker(), "schema": "2"}, {}),
        ({**_current_marker(), "schema": 999}, {}),
        ({key: value for key, value in _current_marker().items() if key != "schema"}, {}),
        ({**_current_marker(), "name_en": 1}, {"name_en": ""}),
        ({key: v for key, v in _current_marker().items() if key != "name_en"}, {"name_en": ""}),
        ({**_current_marker(), "sort_order": True}, {"sort_order": 0}),
        ({**_current_marker(), "sort_order": "0"}, {"sort_order": 0}),
        ({key: v for key, v in _current_marker().items() if key != "sort_order"}, {"sort_order": 0}),
        ({**_current_marker(), "future_field": {"a": 1}}, {"future_field": {"a": 1}}),
    ],
)
def test_read_category_marker_defaults_instead_of_rejecting(tmp_path, payload, expect):
    marker = tmp_path / CATEGORY_MARKER_FILE
    marker.write_text(json.dumps(payload), encoding="utf-8")

    got = ModManager._read_category_marker(tmp_path)
    assert got is not None
    assert got["id"] == "category-id"
    for key, value in expect.items():
        assert got[key] == value


def test_read_category_marker_rejects_malformed_json(tmp_path):
    marker = tmp_path / CATEGORY_MARKER_FILE
    marker.write_text("{broken", encoding="utf-8")

    with pytest.raises(InvalidManagedMetadataError):
        ModManager._read_category_marker(tmp_path)


def test_reconstruct_tree_from_markers(mm):
    parent = mm.add_character(name="示例游戏甲", node_type="category")
    mm.add_character(name="示例角色丁", node_type="character", parent_id=parent.id)

    # 模拟 DB 丢失角色树（物理 marker 仍在）
    mm._index._conn.execute("DELETE FROM nodes")
    mm._index._conn.commit()
    assert mm.get_characters("game_a") == []

    n = mm.rebuild_character_tree_from_markers("game_a")
    assert n == 2
    chars = {c.name: c for c in mm.get_characters("game_a")}
    assert set(chars) == {"示例游戏甲", "示例角色丁"}
    assert chars["示例角色丁"].node_type == "character"
    assert chars["示例角色丁"].parent_id == chars["示例游戏甲"].id  # 父子靠物理嵌套还原
    assert chars["示例游戏甲"].parent_id is None  # 顶层


def test_reconstruct_is_idempotent(mm):
    mm.add_character(name="示例角色己", node_type="character")
    # 树完好时重建应不新增（幂等）
    n = mm.rebuild_character_tree_from_markers("game_a")
    assert n == 0
    assert len(mm.get_characters("game_a")) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "category", "id": "legacy-id"},
        {**_current_marker(), "node_type": "legacy"},
        "{broken",
    ],
)
def test_reconstruct_rejects_invalid_marker_without_mutating_file(mm, payload):
    char = mm.add_character(name="示例角色午", node_type="character")
    marker = mm._get_character_library(char.id) / CATEGORY_MARKER_FILE
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    marker.write_text(raw, encoding="utf-8")
    mm._index._conn.execute("DELETE FROM nodes")
    mm._index._conn.commit()

    assert mm.rebuild_character_tree_from_markers("game_a") == 0
    assert mm.get_characters("game_a") == []
    assert marker.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("parent_marker_state", ["invalid", "missing"])
def test_reconstruct_does_not_promote_child_with_unmanaged_parent(
    mm,
    parent_marker_state,
):
    parent = mm.add_character(name="父分类", node_type="category")
    child = mm.add_character(
        name="合法子角色",
        node_type="character",
        parent_id=parent.id,
    )
    parent_dir = mm._get_character_library(parent.id, create=False)
    child_dir = mm._get_character_library(child.id, create=False)
    parent_marker = parent_dir / CATEGORY_MARKER_FILE
    child_marker = child_dir / CATEGORY_MARKER_FILE
    child_marker_before = child_marker.read_bytes()

    if parent_marker_state == "invalid":
        parent_marker_before = b'{"type": "category", "id": "legacy-parent"}'
        parent_marker.write_bytes(parent_marker_before)
    else:
        parent_marker_before = None
        parent_marker.unlink()

    mm._index._conn.execute("DELETE FROM nodes")
    mm._index._conn.commit()

    assert mm.rebuild_character_tree_from_markers("game_a") == 0
    assert mm.get_characters("game_a") == []
    assert parent_dir.is_dir()
    assert child_dir.is_dir()
    assert child_marker.read_bytes() == child_marker_before
    if parent_marker_before is None:
        assert not parent_marker.exists()
    else:
        assert parent_marker.read_bytes() == parent_marker_before


def test_recover_rebuilds_tree_and_classifies(mm):
    parent = mm.add_character(name="示例游戏甲", node_type="category")
    child = mm.add_character(name="示例角色丁", node_type="character", parent_id=parent.id)
    _make_mod_dir(mm._get_character_library(child.id), "Delta_A")
    scan_and_sync(mm)
    assert len(mm.get_mods()) == 1

    result = mm.recover_corrupt_index()
    assert result["quarantined"]

    chars = {c.name: c for c in mm.get_characters("game_a")}
    assert set(chars) >= {"示例游戏甲", "示例角色丁"}
    mods = mm.get_mods(node_id=chars["示例角色丁"].id)
    assert len(mods) == 1 and mods[0].folder_name == "Delta_A"


# 启动检测：区分 ok / stale(库被清空) / corrupt(无法解析)


def test_needs_recovery_empty_on_fresh_install(mm):
    assert mm.index_needs_recovery() == ""


def test_needs_recovery_stale_when_disk_has_data_but_db_empty(mm):
    parent = mm.add_character(name="示例游戏甲", node_type="category")
    child = mm.add_character(name="示例角色丁", node_type="character", parent_id=parent.id)
    _make_mod_dir(mm._get_character_library(child.id), "A")
    scan_and_sync(mm)
    # 模拟"索引被清空但 DB 仍可打开"
    mm._index._conn.execute("DELETE FROM mods")
    mm._index._conn.execute("DELETE FROM nodes")
    mm._index._conn.commit()

    assert mm.index_healthy is True  # 空库 = 健康（health_check 漏判）
    assert mm.index_needs_recovery() == "stale"


def test_needs_recovery_corrupt_when_unhealthy(mm, monkeypatch):
    monkeypatch.setattr(mm._index, "health_check", lambda: False)
    assert mm.index_needs_recovery() == "corrupt"


def test_rebuild_all_games_repopulates_from_disk(mm):
    parent = mm.add_character(name="示例游戏甲", node_type="category")
    child = mm.add_character(name="示例角色丁", node_type="character", parent_id=parent.id)
    _make_mod_dir(mm._get_character_library(child.id), "A")
    scan_and_sync(mm)
    mm._index._conn.execute("DELETE FROM mods")
    mm._index._conn.execute("DELETE FROM nodes")
    mm._index._conn.commit()

    reports = mm.rebuild_all_games()
    assert "game_a" in reports
    chars = {c.name: c for c in mm.get_characters("game_a")}
    assert "示例角色丁" in chars
    assert len(mm.get_mods(node_id=chars["示例角色丁"].id)) == 1
