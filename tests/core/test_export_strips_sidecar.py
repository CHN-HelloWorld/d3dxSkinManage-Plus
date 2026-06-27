from __future__ import annotations

import json
import zipfile
from pathlib import Path

from src.core.mod_exporter import ExportItem, ModExporter
from src.core.mod_package import (
    META_DIR,
    build_local_manifest,
    write_container_marker,
    write_sidecar,
)
from src.models.mod import Mod


def _src_with_local_sidecar(tmp_path: Path) -> Path:
    container = tmp_path / "LibMod"
    src = container / "LibMod"
    src.mkdir(parents=True)
    (src / "a.ini").write_text("[K]\n", encoding="utf-8")
    assert write_container_marker(container, src.name, Mod.compute_sha1(src), imported_at="2026-10-05T10:00:00")
    # 模拟库内 Mod 已常驻本地 sidecar（带 local 块）
    write_sidecar(
        src,
        build_local_manifest(
            {"name": "示例角色丁", "info": {"author": "Me"}},
            {"classification": ["示例游戏甲", "示例角色丁"], "status_hint": "enabled"},
        ),
    )
    assert (container / META_DIR / "info.json").is_file()
    return src


def test_export_zip_no_meta_strips_local_sidecar(tmp_path):
    src = _src_with_local_sidecar(tmp_path)
    dest = tmp_path / "out"
    dest.mkdir()
    res = ModExporter().export_one(
        ExportItem(name="示例角色丁", src_path=str(src)), dest, fmt="zip", include_meta=False
    )
    assert res.ok
    with zipfile.ZipFile(res.out_path, "r") as zf:
        names = zf.namelist()
    # include_meta=False 且源含本地 sidecar → 产物里不得带任何 .d3dxmeta
    assert not any(".d3dxmeta" in n for n in names)


def test_export_zip_with_meta_single_clean_envelope(tmp_path):
    src = _src_with_local_sidecar(tmp_path)
    dest = tmp_path / "out"
    dest.mkdir()
    share_meta = {"name": "示例角色丁分享", "info": {"author": "Me", "tags": ["share"]}}
    res = ModExporter().export_one(
        ExportItem(name="示例角色丁", src_path=str(src), meta=share_meta),
        dest,
        fmt="zip",
        include_meta=True,
    )
    assert res.ok
    with zipfile.ZipFile(res.out_path, "r") as zf:
        names = zf.namelist()
        metas = [n for n in names if n.replace("\\", "/").endswith(".d3dxmeta/info.json")]
        assert len(metas) == 1  # 仅一份信封，无重复成员
        manifest = json.loads(zf.read(metas[0]).decode("utf-8"))
    assert manifest["name"] == "示例角色丁分享"  # 写入的是分享信封，非本地的
    assert "local" not in manifest  # local 块不外泄


def test_export_folder_with_meta_strips_then_writes(tmp_path):
    src = _src_with_local_sidecar(tmp_path)
    dest = tmp_path / "out"
    dest.mkdir()
    res = ModExporter().export_one(
        ExportItem(name="示例角色丁", src_path=str(src), meta={"name": "示例角色丁分享", "info": {"author": "Me"}}),
        dest,
        fmt="folder",
        include_meta=True,
    )
    assert res.ok
    manifest = json.loads((Path(res.out_path) / META_DIR / "info.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "示例角色丁分享"
    assert "local" not in manifest
