from __future__ import annotations

import json
import shutil
import uuid
import zipfile
from pathlib import Path

import pytest

from src.core.mod_manager import ModManager
from src.core.mod_package import (
    read_container_marker,
    read_sidecar,
)
from src.models.mod import Mod
from src.utils.constants import CATEGORY_MARKER_FILE
from tests.helpers import register_games, reset_game_service, scan_and_sync
from tests.helpers import registered_games as registered_games


def _make_mod_dir(parent: Path, name: str) -> Path:
    d = parent / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "mod.ini").write_text("[Constants]\n", encoding="utf-8")
    return d


def _make_deployable_mod(parent: Path, name: str) -> Path:
    """使用当前持久化接口创建可部署的归档容器。"""
    from src.core.mod_package import build_local_manifest, local_meta_from_mod, write_container_marker, write_sidecar

    container = parent / name
    container.mkdir(parents=True, exist_ok=True)
    body = container / f"{name}.zip"
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr(f"{name}/mod.ini", "[Constants]\n")
    mod = Mod(name=name, folder_name=name, path=str(body), content_hash=Mod.compute_sha1(str(body)))
    write_container_marker(container, body.name, mod.content_hash, mod_id=mod.mod_id, imported_at=mod.imported_at)
    assert write_sidecar(body, build_local_manifest(local_meta_from_mod(mod)))
    return body


@pytest.fixture
def mm(temp_data_dir):
    """先注册游戏再创建管理器，保证隔离损坏索引后仍能重建该游戏。"""
    register_games("game_a", "other")
    m = ModManager()
    m.set_game("game_a")
    yield m
    reset_game_service()


def test_resolve_character_id_by_name_chain(mm):
    parent = mm.add_character(name="示例游戏甲", node_type="category")
    child = mm.add_character(name="示例角色丁", node_type="character", parent_id=parent.id)
    char_lib = mm._get_character_library(child.id)
    mod_dir = _make_mod_dir(char_lib, "Delta_Skin")

    cid = mm._resolve_character_id_for_path(str(mod_dir), "game_a")
    assert cid == child.id


def test_resolve_falls_back_to_marker_id(mm):
    char = mm.add_character(name="示例角色己", node_type="character")
    char_lib = mm._get_character_library(char.id)
    mod_dir = _make_mod_dir(char_lib, "Zeta_Skin")
    renamed_lib = char_lib.with_name("Zeta")
    char_lib.rename(renamed_lib)

    assert (
        mm._resolve_character_id_for_path(
            str(renamed_lib / mod_dir.name),
            "game_a",
        )
        == char.id
    )


def test_resolve_does_not_use_id_from_invalid_category_marker(mm):
    char = mm.add_character(name="示例角色己", node_type="character")
    char_lib = mm._get_character_library(char.id)
    mod_dir = _make_mod_dir(char_lib, "Zeta_Skin")
    marker = char_lib / CATEGORY_MARKER_FILE
    marker.write_text(json.dumps({"id": char.id}), encoding="utf-8")
    renamed_lib = char_lib.with_name("Zeta")
    char_lib.rename(renamed_lib)

    assert (
        mm._resolve_character_id_for_path(
            str(renamed_lib / mod_dir.name),
            "game_a",
        )
        == ""
    )


def test_discover_handles_nested_categories(mm):
    parent = mm.add_character(name="示例游戏甲", node_type="category")
    child = mm.add_character(name="示例角色丁", node_type="character", parent_id=parent.id)
    _make_mod_dir(mm._get_character_library(child.id), "Delta_A")
    _make_mod_dir(mm._get_character_library(child.id), "Delta_B")

    found = mm._discover_library_mods("game_a")
    by_name = {d["folder_name"]: d for d in found}
    assert "Delta_A" in by_name and "Delta_B" in by_name
    assert by_name["Delta_A"]["node_id"] == child.id
    # 子类别目录「示例角色丁」(在 示例游戏甲 下) 不应作为 mod 出现
    assert "示例角色丁" not in by_name


def test_rebuild_accepts_complete_current_container(mm):
    char = mm.add_character(name="示例角色丁", node_type="character")
    char_lib = mm._get_character_library(char.id)
    container = char_lib / "CurrentSkin"
    body = _make_mod_dir(container, "CurrentSkin")
    content_hash = Mod.compute_sha1(str(body))
    marker = {
        "schema": 1,
        "kind": "d3dxSkinManage-Plus.mod-container",
        "body": body.name,
        "body_type": "folder",
        "content_hash": content_hash,
        "mod_id": uuid.uuid4().hex,
    }
    manifest = {
        "schema": 2,
        "kind": "d3dxSkinManage-Plus.mod-share",
        "name": "Current Skin Display",
        "info": {"tags": ["current-schema"]},
        "previews": [],
    }
    (container / ".d3dxmod").write_text(
        json.dumps(marker),
        encoding="utf-8",
    )
    meta_dir = container / ".d3dxmeta"
    meta_dir.mkdir()
    (meta_dir / "info.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    assert read_container_marker(container) == marker
    assert read_sidecar(body) == manifest

    report = mm.rebuild_index_from_filesystem("game_a")

    assert report.registered == 1
    assert report.classified == 1
    rebuilt = mm.get_mods(node_id=char.id)
    assert len(rebuilt) == 1
    assert Path(rebuilt[0].path) == body
    assert rebuilt[0].name == "Current Skin Display"
    assert rebuilt[0].content_hash == content_hash


def test_rebuild_rejects_container_with_invalid_sidecar(mm):
    char = mm.add_character(name="示例角色丁", node_type="character")
    char_lib = mm._get_character_library(char.id)
    container = char_lib / "BrokenSidecar"
    body = _make_mod_dir(container, "BrokenSidecar")
    content_hash = Mod.compute_sha1(str(body))
    marker = {
        "schema": 1,
        "kind": "d3dxSkinManage-Plus.mod-container",
        "body": body.name,
        "body_type": "folder",
        "content_hash": content_hash,
    }
    (container / ".d3dxmod").write_text(json.dumps(marker), encoding="utf-8")
    meta_dir = container / ".d3dxmeta"
    meta_dir.mkdir()
    (meta_dir / "info.json").write_text(
        json.dumps({"schema": 1}),
        encoding="utf-8",
    )

    report = mm.rebuild_index_from_filesystem("game_a")
    assert report.registered == 1
    assert body.is_dir()
    assert len(mm.get_mods(node_id=char.id)) == 1


def test_rebuild_rejects_container_without_sidecar(mm):
    char = mm.add_character(name="示例角色丁", node_type="character")
    char_lib = mm._get_character_library(char.id)
    container = char_lib / "MissingSidecar"
    body = _make_mod_dir(container, "MissingSidecar")
    content_hash = Mod.compute_sha1(str(body))
    marker = {
        "schema": 1,
        "kind": "d3dxSkinManage-Plus.mod-container",
        "body": body.name,
        "body_type": "folder",
        "content_hash": content_hash,
    }
    (container / ".d3dxmod").write_text(json.dumps(marker), encoding="utf-8")

    report = mm.rebuild_index_from_filesystem("game_a")
    assert report.registered == 1
    assert body.is_dir()
    assert len(mm.get_mods(node_id=char.id)) == 1


def test_rebuild_rejects_container_with_invalid_marker(mm):
    char = mm.add_character(name="示例角色丁", node_type="character")
    char_lib = mm._get_character_library(char.id)
    container = char_lib / "BrokenCurrentContainer"
    body = _make_mod_dir(container, "BrokenCurrentContainer")
    (container / ".d3dxmod").write_text("{broken", encoding="utf-8")

    report = mm.rebuild_index_from_filesystem("game_a")
    assert report.invalid_metadata

    assert body.is_dir()
    assert mm.get_mods(node_id=char.id) == []


def test_rebuild_preserves_count_classification_and_enabled(mm, tmp_path):
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    mm._storage.set_migoto_mods_dir(str(deploy))

    char = mm.add_character(name="示例角色丁", node_type="character")
    # 用例执行实际部署，需要使用归档本体。
    _make_deployable_mod(mm._get_character_library(char.id), "A")
    _make_deployable_mod(mm._get_character_library(char.id), "B")

    # 初次重建：登记并分类 A、B（仅依赖已实现的 _discover；不依赖 Task6 的 scan 分类）
    first = mm.rebuild_index_from_filesystem("game_a")
    assert first.registered == 2
    assert first.classified == 2
    mods = mm.get_mods(node_id=char.id)
    assert len(mods) == 2
    mod_a = next(m for m in mods if m.folder_name == "A")
    mm.toggle_mod(mod_a.mod_id)  # 部署 A → enabled
    assert mm.get_mod(mod_a.mod_id).is_enabled

    # 再次重建：数 / 分类 / 启用态都应保持
    report = mm.rebuild_index_from_filesystem("game_a")
    assert report.registered == 2
    assert report.classified == 2
    assert report.unclassified == 0

    rebuilt = {m.folder_name: m for m in mm.get_mods(node_id=char.id)}
    assert set(rebuilt) == {"A", "B"}
    assert rebuilt["A"].is_enabled
    assert not rebuilt["B"].is_enabled


def test_rebuild_unclassified_when_character_missing(mm):
    library_root = mm._get_library_root("game_a")
    _make_deployable_mod(library_root / "Stray", "Orphan")

    report = mm.rebuild_index_from_filesystem("game_a")
    assert report.invalid_metadata
    assert report.registered == 0
    assert mm.get_mods() == []


def test_rebuild_preserves_preset_membership_by_identity(mm, tmp_path):
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    mm._storage.set_migoto_mods_dir(str(deploy))

    char = mm.add_character(name="示例角色丁", node_type="character")
    # 用例执行实际部署，需要使用归档本体。
    _make_deployable_mod(mm._get_character_library(char.id), "A")
    _make_deployable_mod(mm._get_character_library(char.id), "B")
    mm.rebuild_index_from_filesystem("game_a")  # 初次登记并分类

    mods = {m.folder_name: m for m in mm.get_mods(node_id=char.id)}
    old_a_id = mods["A"].mod_id
    mm.toggle_mod(old_a_id)  # 启用 A → 写进当前预设

    pm = mm.preset_manager
    assert old_a_id in pm.get_current("game_a").enabled_mod_ids

    mm.rebuild_index_from_filesystem("game_a")

    rebuilt = {m.folder_name: m for m in mm.get_mods(node_id=char.id)}
    new_a_id = rebuilt["A"].mod_id
    assert new_a_id == old_a_id
    cur2 = pm.get_current("game_a")
    assert new_a_id in cur2.enabled_mod_ids
    assert old_a_id in cur2.enabled_mod_ids
    assert rebuilt["B"].mod_id not in cur2.enabled_mod_ids  # B 未启用


def test_remap_retains_missing_source_intent_until_it_reappears(mm, tmp_path):
    char = mm.add_character(name="示例角色己", node_type="character")
    _make_deployable_mod(mm._get_character_library(char.id), "Only")
    missing_body = _make_deployable_mod(mm._get_character_library(char.id), "Missing")
    mm.rebuild_index_from_filesystem("game_a")

    mods = {mod.folder_name: mod for mod in mm.get_mods(node_id=char.id)}
    only, missing = mods["Only"], mods["Missing"]
    pm = mm.preset_manager
    cur = pm.get_current("game_a")
    cur.add_mod_for_character(char.id, missing.mod_id)
    cur.add_mod_for_character(char.id, only.mod_id)
    pm._save()
    saved = tmp_path / "saved-container"
    shutil.copytree(missing_body.parent, saved)
    shutil.rmtree(missing_body.parent)

    mm.rebuild_index_from_filesystem("game_a")
    assert missing.mod_id in pm.get_current("game_a").enabled_mod_ids
    persisted = json.loads(pm._presets_file.read_text(encoding="utf-8"))
    record = next(p for p in persisted["presets"] if p["id"] == cur.id)
    assert missing.mod_id in record["character_mods"][char.id]
    shutil.copytree(saved, missing_body.parent)
    mm.rebuild_index_from_filesystem("game_a")
    restored = mm._index.get_mod(missing.mod_id)
    assert restored.mod_id in pm.get_current("game_a").enabled_mod_ids


def test_index_healthy_true_normally(mm):
    assert mm.index_healthy is True


def test_recover_corrupt_index_requarantines_and_rebuilds(mm):
    """隔离当前数据库后，从文件库重新登记 Mod 并保留原数据库备份。

    验证恢复后仍能查询原有 Mod，且数据库隔离过程成功完成。
    """
    char = mm.add_character(name="示例角色丁", node_type="character")
    _make_mod_dir(mm._get_character_library(char.id), "A")
    scan_and_sync(mm)
    assert len(mm.get_mods()) == 1

    result = mm.recover_corrupt_index()
    assert result["quarantined"]
    assert Path(result["quarantined"]).exists()

    all_mods = mm.get_mods()
    assert any(m.folder_name == "A" for m in all_mods)


def test_rebuild_reports_progress(mm):
    char = mm.add_character(name="示例角色丁", node_type="character")
    _make_deployable_mod(mm._get_character_library(char.id), "A")
    _make_deployable_mod(mm._get_character_library(char.id), "B")

    calls: list[tuple[int, int]] = []
    report = mm.rebuild_index_from_filesystem("game_a", progress_cb=lambda done, total: calls.append((done, total)))
    assert report.registered == 2
    assert calls, "progress_cb 未被回调"
    assert calls[-1] == (2, 2)
    assert (1, 2) in calls and calls[-1] == (2, 2)  # 逐个递增


def test_rebuild_skips_and_reports_mod_whose_content_cannot_be_hashed(mm, monkeypatch):
    """无法读取的单个 Mod 应被跳过并报告，其余资源继续重建。"""
    char = mm.add_character(name="示例角色丁", node_type="character")
    char_lib = mm._get_character_library(char.id)
    good = _make_deployable_mod(char_lib, "Good")
    bad = _make_deployable_mod(char_lib, "Bad")

    real_sha1 = Mod.compute_sha1

    def explode_on_bad(path, **kwargs):
        if Path(path) == bad:
            raise OSError("archive backend exploded")
        return real_sha1(path, **kwargs)

    monkeypatch.setattr(Mod, "compute_sha1", staticmethod(explode_on_bad))

    report = mm.rebuild_index_from_filesystem("game_a")

    assert report.registered == 1
    rebuilt = mm.get_mods(node_id=char.id)
    assert [m.folder_name for m in rebuilt] == ["Good"]
    assert Path(rebuilt[0].path) == good
    assert str(bad) in report.invalid_metadata


def test_rebuild_skips_mod_whose_path_vanished_after_planning(mm):
    """路径在规划后消失时不得落"空内容摘要"记录（compute_sha1 对不存在路径返回
    da39a3ee…，40 位合法但等价于空，多条这种记录还会互相被判为重复内容）。"""
    char = mm.add_character(name="示例角色己", node_type="character")
    char_lib = mm._get_character_library(char.id)
    doomed = _make_deployable_mod(char_lib, "Doomed")

    descriptors = mm._discover_library_mods("game_a")
    assert len(descriptors) == 1

    doomed.unlink()
    unreadable: list[str] = []
    enriched = mm._hash_rebuild_descriptors(descriptors, unreadable=unreadable)

    assert enriched == []
    assert unreadable == [str(doomed)]
    report = mm.rebuild_index_from_filesystem("game_a")
    assert report.registered == 0


def test_rebuild_phases_compose_equivalently(mm):
    from src.core.mod_identity import read_source_mod_id
    from src.core.rebuild_session import RebuildSession

    char = mm.add_character(name="Zeta", node_type="character")
    body = _make_deployable_mod(mm._get_character_library(char.id), "Zeta")
    identity = read_source_mod_id(body)
    session = RebuildSession(mm, "game_a")
    try:
        session.prepare()
        assert session.enriched[0]["mod_id"] == identity
        while not session.stage_batch():
            pass
        session.validate()
        report = session.commit()
        assert report.registered == 1
        assert mm.get_mods(node_id=char.id)[0].mod_id == identity
    finally:
        session.close()


def test_rebuild_restores_metadata_from_sidecar(mm, tmp_path):
    from src.models.mod import AgeRating

    char = mm.add_character(name="示例角色丁", node_type="character")
    _make_mod_dir(mm._get_character_library(char.id), "Skin")
    scan_and_sync(mm)
    mod = mm.get_mods(node_id=char.id)[0]

    # 编辑 → 经 update_mod 写 sidecar
    mod.name = "示例角色丁·旗袍"
    mod.info.tags = ["旗袍", "4K"]
    mod.info.age_rating = AgeRating.X
    mod.info.author = "原作者"
    assert mm.update_mod(mod)

    report = mm.rebuild_index_from_filesystem("game_a")
    assert report.registered == 1

    rebuilt = mm.get_mods(node_id=char.id)[0]
    assert rebuilt.name == "示例角色丁·旗袍"
    assert set(rebuilt.info.tags) == {"旗袍", "4K"}
    assert rebuilt.info.age_rating == AgeRating.X
    assert rebuilt.info.author == "原作者"
    assert rebuilt.node_id == char.id


def test_rebuild_classifies_via_sidecar_breadcrumb_when_path_fails(mm, tmp_path):
    from src.core.mod_package import build_local_manifest, write_sidecar

    mm.add_character(name="示例角色丁", node_type="character")
    # 放在库根的「散落」目录（路径段不匹配任何角色名链）
    container = mm._get_library_root("game_a") / "_stray" / "Skin"
    stray = container / "Skin"
    stray.mkdir(parents=True)
    (stray / "mod.ini").write_text("[K]\n", encoding="utf-8")
    from src.core.mod_package import write_container_marker

    assert write_container_marker(container, stray.name, "a" * 40, imported_at="2026-10-05T10:00:00")
    assert write_sidecar(
        stray,
        build_local_manifest(
            {"name": "示例角色丁皮", "info": {}}, {"classification": ["示例角色丁"], "mod_id": uuid.uuid4().hex}
        ),
    )

    report = mm.rebuild_index_from_filesystem("game_a")
    assert report.invalid_metadata
    assert report.registered == 0
    assert mm.get_mods() == []
