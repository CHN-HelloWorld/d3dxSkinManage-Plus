"""验证类别重命名同步更新数据库名称与磁盘目录。

目录重命名后，资源扫描及反向路径查找仍应指向原类别，避免将资源误判为未分类。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.mod_manager import ModManager
from src.models.mod import AgeRating, Mod, ModInfo, ModStatus
from src.utils.constants import CATEGORY_MARKER_FILE
from tests.helpers import registered_games as registered_games


def _register_mod(mm, mod_dir: Path, node_id: str) -> str:
    """创建受管身份和描述后登记，使用完整记录验证路径同步。"""
    from src.core.mod_package import build_local_manifest, local_meta_from_mod, write_container_marker, write_sidecar

    mod = Mod(
        game_id="game_a",
        name=mod_dir.name,
        folder_name=mod_dir.parent.name,
        node_id=node_id,
        path=str(mod_dir),
        content_hash=Mod.compute_sha1(str(mod_dir)),
        status=ModStatus.DISABLED,
        info=ModInfo(age_rating=AgeRating.G),
    )
    write_container_marker(
        mod_dir.parent, mod_dir.name, mod.content_hash, mod_id=mod.mod_id, imported_at=mod.imported_at
    )
    assert write_sidecar(mod_dir, build_local_manifest(local_meta_from_mod(mod)))
    assert mm._index.add_mod(mod)
    return mod.mod_id


def _make_mod_dir(parent: Path, name: str) -> Path:
    d = parent / name / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "mod.ini").write_text("[Constants]\n", encoding="utf-8")
    return d


@pytest.fixture
def mm(temp_data_dir):
    m = ModManager()
    m.set_game("game_a")
    return m


def test_rename_category_moves_disk_dir(mm):
    char = mm.add_character(name="旧名", node_type="category")
    old_dir = mm._get_character_library(char.id)
    assert old_dir.is_dir()
    assert (old_dir / CATEGORY_MARKER_FILE).is_file()

    assert mm.update_character(
        char.id,
        name="新名",
        name_en="NewName",
        node_type="character",
    )

    new_dir = mm._get_character_library(char.id, create=False)
    assert new_dir.name == "新名"
    assert new_dir.is_dir(), "改名后新名目录必须存在"
    assert not old_dir.exists(), "改名后旧名目录必须已被移走"
    # marker 同步为新名（否则重建树会用旧名回退处理）
    marker = new_dir / CATEGORY_MARKER_FILE
    assert marker.is_file()
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "schema": 2,
        "kind": "d3dxSkinManage-Plus.category",
        "id": char.id,
        "node_type": "character",
        "name": "新名",
        "name_en": "NewName",
        "sort_order": 0,
        "thumbnail": None,
    }


def test_update_character_rolls_back_when_marker_write_fails(mm, monkeypatch):
    char = mm.add_character(
        name="旧名",
        name_en="OldName",
        node_type="category",
    )
    old_dir = mm._get_character_library(char.id, create=False)
    new_dir = old_dir.with_name("新名")
    marker = old_dir / CATEGORY_MARKER_FILE
    original_marker = marker.read_bytes()
    mod_dir = _make_mod_dir(old_dir, "NestedMod")
    mod_id = _register_mod(mm, mod_dir, char.id)
    original_mod_path = mm.get_mod(mod_id).path

    def fail_after_partial_marker_write(path, _text, *, encoding="utf-8"):
        Path(path).write_text("{partial", encoding=encoding)
        raise OSError("injected category marker write failure")

    monkeypatch.setattr(
        "src.core.mod_manager.fs.write_text",
        fail_after_partial_marker_write,
    )

    result = mm.update_character(
        char.id,
        name="新名",
        name_en="NewName",
        node_type="character",
    )

    assert result is False
    restored = mm.get_character(char.id)
    assert restored is not None
    assert (restored.name, restored.name_en, restored.node_type) == (
        "旧名",
        "OldName",
        "category",
    )
    assert old_dir.is_dir()
    assert not new_dir.exists()
    assert marker.read_bytes() == original_marker
    restored_mod = mm.get_mod(mod_id)
    assert restored_mod is not None
    assert restored_mod.path == original_mod_path
    assert Path(restored_mod.path).is_dir()


def test_update_character_does_not_destroy_marker_when_snapshot_read_fails(mm, monkeypatch):
    char = mm.add_character(name="旧名", node_type="category")
    old_dir = mm._get_character_library(char.id, create=False)
    marker = old_dir / CATEGORY_MARKER_FILE
    original_marker = marker.read_bytes()

    def fail_marker_snapshot(path):
        if Path(path) == marker:
            raise OSError("injected marker snapshot failure")
        return Path(path).read_bytes()

    monkeypatch.setattr("src.core.mod_manager.fs.read_bytes", fail_marker_snapshot)
    assert mm.update_character(char.id, name="新名") is False
    assert marker.read_bytes() == original_marker
    assert mm.get_character(char.id).name == "旧名"


def test_update_character_rejects_failed_empty_target_removal(mm, monkeypatch):
    char = mm.add_character(name="旧名", node_type="category")
    old_dir = mm._get_character_library(char.id, create=False)
    new_dir = old_dir.with_name("新名")
    new_dir.mkdir()

    def leave_target_in_place(path):
        if Path(path) == new_dir:
            return None
        return Path(path).rmdir()

    monkeypatch.setattr("src.core.mod_manager.fs.rmdir", leave_target_in_place)
    assert mm.update_character(char.id, name="新名") is False
    assert old_dir.is_dir()
    assert new_dir.is_dir()


def test_rename_category_not_misclassified_as_unclassified(mm):
    char = mm.add_character(name="空", node_type="category")
    char_dir = mm._get_character_library(char.id)
    # 类别目录下放一个真实 Mod 目录
    _make_mod_dir(char_dir, "SomeMod")

    mm.update_character(char.id, name="改名后")

    discovered = mm._discover_library_mods("game_a")
    # 该 Mod 应归到该类别（node_id == char.id），且没有任何未归类项
    mine = [d for d in discovered if Path(d["path"]).name == "SomeMod"]
    assert len(mine) == 1, "改名后类别下的 Mod 应被发现"
    assert mine[0]["node_id"] == char.id, "Mod 应归属改名后的类别，而非未归类"
    unclassified = [d for d in discovered if d["node_id"] == ""]
    assert not unclassified, f"不应有未归类 Mod，实际: {unclassified}"


def test_rename_syncs_child_mod_paths(mm):
    parent = mm.add_character(name="父", node_type="category")
    child = mm.add_character(name="子", node_type="character", parent_id=parent.id)
    child_dir = mm._get_character_library(child.id)
    mod_dir = _make_mod_dir(child_dir, "ChildMod")
    _register_mod(mm, mod_dir, child.id)

    mm.update_character(parent.id, name="父改名")

    # 子 Mod 的物理路径应已重写到新前缀，且文件真实存在
    mods = mm.get_mods(node_id=child.id)
    assert len(mods) == 1
    assert "父改名" in mods[0].path
    assert "父改名" not in "旧" and Path(mods[0].path).exists(), "改名后子 Mod 路径应指向真实存在的新目录"
