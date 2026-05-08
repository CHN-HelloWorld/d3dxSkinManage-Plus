"""import_progress 导入路径的分享信封还原测试

覆盖 ImportProgressDialog 规划请求，以及 ModIntakeService prepare/commit 还原元数据的路径。
测试直接执行主线程提交，不启动 QThread，避免影响后续 UI 测试的事件循环。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.mod_exporter import ExportItem, ModExporter
from src.core.mod_intake import IntakeError
from src.core.mod_manager import ModManager
from src.models.mod import AgeRating, Mod
from src.ui.dialogs.import_progress import ImportProgressDialog
from tests.helpers import registered_games as registered_games


def _png(p: Path) -> Path:
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return p


def test_import_progress_restores_envelope_metadata(qtbot, temp_data_dir, tmp_path):
    from src.core.mod_package import (
        META_DIR,
        read_container_marker,
        write_container_marker,
    )

    # 当前格式容器：本体原图不应进入分享信封，手动补充图才进入。
    container = tmp_path / "src_container"
    src_mod = container / "src_mod"
    src_mod.mkdir(parents=True)
    (src_mod / "dummy.ini").write_text("[Key]\nx=1\n", encoding="utf-8")
    original = _png(src_mod / "original.png")
    supplemental_dir = container / META_DIR / "previews"
    supplemental_dir.mkdir(parents=True)
    ext_prev = _png(supplemental_dir / "cover.png")
    original_hash = Mod.compute_sha1(str(src_mod))
    write_container_marker(container, src_mod.name, original_hash, imported_at="2026-10-01T00:00:00")

    # 导出带信封 zip
    dest = tmp_path / "out"
    dest.mkdir()
    meta = {
        "name": "漂亮皮肤",
        "info": {
            "author": "Alice",
            "description": "nice",
            "version": "1.0",
            "url": "https://x",
            "age_rating": "R",
            "tags": ["cute", "blue"],
            "custom_data": {"gamebanana": {"mod_id": 555, "file_id": 777, "last_checked_iso": ""}},
        },
    }
    item = ExportItem(name="漂亮皮肤", src_path=str(src_mod), meta=meta, preview_paths=[str(original), str(ext_prev)])
    res = ModExporter().export_one(item, dest, fmt="zip", include_meta=True)
    assert res.ok
    out_zip = Path(res.out_path)

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色X", node_type="character")

    dlg = ImportProgressDialog([(str(out_zip), "", "", "G")], char.id, mm)
    qtbot.addWidget(dlg)
    request = dlg._plan_intake_requests()[0]
    prepared = dlg._intake_service.prepare(request)
    assert prepared.share_manifest is not None, "统一纳管未提取分享信封"
    assert prepared.import_source.share_previews, "统一纳管未暂存信封预览图"
    assert read_container_marker(prepared.container_path)["content_hash"] == prepared.content_hash

    mod = dlg._register_mod_in_db(prepared)
    assert mod is not None

    # 元数据还原
    assert mod.info.author == "Alice"
    assert mod.info.description == "nice"
    assert mod.info.age_rating == AgeRating.R  # 信封 R 覆盖默认 G
    assert set(mod.info.tags) >= {"cute", "blue"}
    assert mod.display_name == "漂亮皮肤"

    # mod.path 指向容器内本体，sidecar 落**容器级**（本体之外、与本体同级），且容器带 .d3dxmod
    # 正向 marker；内容哈希仍为去信封后的干净值（compute_sha1 跳过 .d3dxmeta）。
    body = Path(mod.path)
    container = body.parent
    assert (container / ".d3dxmod").is_file()
    assert (container / ".d3dxmeta" / "info.json").is_file()
    assert mod.content_hash == original_hash

    # 手动补充封面被带入并还原；本体原图仍由本体扫描得到
    assert any("cover" in p for p in mod.preview_images)
    assert any("original" in p for p in mod.preview_images)
    imported_meta_previews = body.parent / META_DIR / "previews"
    assert (imported_meta_previews / "cover.png").is_file()
    assert not (imported_meta_previews / "original.png").exists()
    assert not (temp_data_dir / "data" / mm._res_dir("game_a") / "previews").exists()

    # GB 更新来源也经 UI 导入路径恢复（重新导入后仍可检测更新）
    assert (mod.info.custom_data.get("gamebanana") or {}).get("mod_id") == 555


def test_import_progress_restores_folder_envelope(qtbot, temp_data_dir, tmp_path):
    """文件夹（非 zip）导入路径也应读取 .d3dxmeta 信封并还原（回归 + 双导入路径一致）。

    不变量同压缩包路径：library 副本无信封、content_hash 为去信封后的干净哈希、
    且绝不删除用户源文件夹里的信封。"""
    from src.core.mod_package import META_DIR, read_container_marker, write_envelope_to_folder

    src = tmp_path / "FolderMod"
    src.mkdir()
    (src / "x.ini").write_text("[K]\na=1\n", encoding="utf-8")
    clean_hash = Mod.compute_sha1(str(src))
    cover = _png(tmp_path / "cover2.png")
    meta = {
        "name": "夹皮",
        "info": {
            "author": "Carol",
            "age_rating": "R",
            "tags": ["t"],
            "url": "https://gamebanana.com/mods/321",
            "custom_data": {"gamebanana": {"mod_id": 321, "last_checked_iso": ""}},
        },
    }
    assert write_envelope_to_folder(src, meta, [str(cover)])

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色FF", node_type="character")

    dlg = ImportProgressDialog([(str(src), "", "", "G")], char.id, mm)
    qtbot.addWidget(dlg)
    request = dlg._plan_intake_requests()[0]
    prepared = dlg._intake_service.prepare(request)
    assert prepared.share_manifest is not None, "文件夹导入未读取信封"
    assert prepared.content_hash == clean_hash, "应为去信封后的干净哈希"
    prepared_body = prepared.body_path
    assert read_container_marker(prepared_body.parent)["content_hash"] == clean_hash

    mod = dlg._register_mod_in_db(prepared)
    assert mod is not None
    assert mod.info.author == "Carol"
    assert mod.info.age_rating == AgeRating.R
    assert (mod.info.custom_data.get("gamebanana") or {}).get("mod_id") == 321
    # 容器级 sidecar + .d3dxmod marker（本体之外）
    body = Path(mod.path)
    assert (body.parent / ".d3dxmod").is_file()
    assert (body.parent / META_DIR / "info.json").is_file()  # 容器级本地 sidecar
    assert mod.content_hash == clean_hash  # hash 仍干净（跳过 .d3dxmeta）
    assert (src / META_DIR).is_dir()  # 不动用户源
    assert any("cover" in p for p in mod.preview_images)
    assert Path(next(p for p in mod.preview_images if "cover" in p)).parent == (body.parent / META_DIR / "previews")
    assert not (temp_data_dir / "data" / mm._res_dir("game_a") / "previews").exists()


def test_import_progress_rolls_back_when_sidecar_write_fails(
    qtbot,
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    src = tmp_path / "SidecarFail"
    src.mkdir()
    (src / "mod.ini").write_text("[K]\n", encoding="utf-8")

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色 sidecar 失败", node_type="character")
    dlg = ImportProgressDialog([(str(src), "", "", "G")], char.id, mm)
    qtbot.addWidget(dlg)
    request = dlg._plan_intake_requests()[0]
    monkeypatch.setattr(
        "src.core.mod_intake.write_sidecar",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(IntakeError, match="sidecar"):
        dlg._intake_service.prepare(request)

    assert not (Path(dlg._dest_parent) / src.name).exists()
    assert mm.get_mods(node_id=char.id) == []
    assert (src / "mod.ini").is_file()
