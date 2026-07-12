"""Mod 分享信封  测试

覆盖：
- 信封读写 round-trip（文件夹 / ZIP，含「保持单顶层」不变量）
- share_meta_from_mod 仅抽取可分享子集
- 端到端：导出（带信封）→ 导入还原元数据/预览图，且 library 内容与哈希保持干净
"""

from __future__ import annotations

import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from src.core.import_format import prepare_import_source
from src.core.mod_package import (
    CONTAINER_KIND,
    CONTAINER_SCHEMA_VERSION,
    META_DIR,
    MOD_CONTAINER_MARKER,
    InvalidManagedMetadataError,
    _detect_single_top,
    append_envelope_to_zip,
    build_manifest,
    read_container_marker,
    read_envelope,
    resolve_body_path,
    resolve_preview_paths,
    share_meta_from_mod,
    strip_envelope,
    write_container_marker,
    write_envelope_to_folder,
)
from tests.helpers import registered_games as registered_games

_VALID_HASH = "a" * 40


def _link_directory(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("当前平台不允许创建 NTFS junction")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前平台不允许创建目录链接: {exc}")


def _png(p: Path) -> Path:
    p.write_bytes(b"\x89PNG\r\n\x1a\n")
    return p


# 纯逻辑 round-trip


def test_folder_envelope_roundtrip(tmp_path):
    mod = tmp_path / "MyMod"
    mod.mkdir()
    (mod / "dummy.ini").write_text("[X]\n", encoding="utf-8")
    prev = _png(tmp_path / "ext.png")
    meta = {"name": "漂亮皮肤", "info": {"author": "Alice", "tags": ["a", "b"], "age_rating": "R"}}

    assert write_envelope_to_folder(mod, meta, [str(prev)])

    env = read_envelope(mod)
    assert env is not None
    m = env["manifest"]
    assert m["name"] == "漂亮皮肤"
    assert m["info"]["author"] == "Alice"
    assert m["info"]["tags"] == ["a", "b"]
    previews = resolve_preview_paths(env["meta_dir"], m)
    assert len(previews) == 1 and previews[0].is_file()

    strip_envelope(mod)
    assert not (mod / META_DIR).exists()
    assert (mod / "dummy.ini").exists()  # 原始内容不受影响


def test_zip_envelope_keeps_single_top(tmp_path):
    z = tmp_path / "mod.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("MyMod/a.ini", "[X]\n")
        zf.writestr("MyMod/sub/b.txt", "y")
    prev = _png(tmp_path / "ext.png")

    assert append_envelope_to_zip(z, {"name": "n", "info": {"author": "Bob"}}, [str(prev)])

    with zipfile.ZipFile(z, "r") as zf:
        names = set(zf.namelist())
    # 不变量：追加信封后仍是单顶层 MyMod
    assert _detect_single_top(names) == "MyMod"
    assert "MyMod/.d3dxmeta/info.json" in names
    assert any(n.startswith("MyMod/.d3dxmeta/previews/") for n in names)


def test_zip_envelope_no_single_top_goes_root(tmp_path):
    z = tmp_path / "flat.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a.ini", "[X]\n")
        zf.writestr("b.ini", "[Y]\n")
    assert append_envelope_to_zip(z, {"name": "n", "info": {}}, None)
    with zipfile.ZipFile(z, "r") as zf:
        names = set(zf.namelist())
    assert ".d3dxmeta/info.json" in names


def test_share_meta_from_mod_extracts_shareable_subset():
    from src.models.mod import AgeRating, Mod, ModInfo

    mod = Mod(
        game_id="game_a",
        name="显示名",
        folder_name="folder",
        path="/x",
        content_hash="h",
        info=ModInfo(
            author="Alice",
            description="d",
            tags=["t"],
            age_rating=AgeRating.R,
            custom_data={
                "gamebanana": {
                    "mod_id": 555,
                    "file_id": 777,
                    "last_checked_iso": "2026-01-01T00:00:00",
                    "ignored": True,
                    "ignored_file_ids": [1, 2],
                }
            },
        ),
    )
    meta = share_meta_from_mod(mod)
    assert meta["name"] == "显示名"
    assert meta["info"]["author"] == "Alice"
    assert meta["info"]["age_rating"] == "R"
    assert meta["info"]["tags"] == ["t"]
    # 不泄漏私有状态字段
    assert "id" not in meta
    assert "path" not in meta["info"] and "content_hash" not in meta["info"]
    # 携带 GameBanana 更新来源标识，但重置检测历史、不带本地忽略偏好
    gb = meta["info"]["custom_data"]["gamebanana"]
    assert gb["mod_id"] == 555 and gb["file_id"] == 777
    assert gb["last_checked_iso"] == ""
    assert "ignored" not in gb and "ignored_file_ids" not in gb


def test_read_envelope_absent(tmp_path):
    assert read_envelope(tmp_path) is None


@pytest.mark.parametrize(
    "manifest",
    [
        [],
        {"schema": 2, "kind": "wrong", "name": "n", "info": {}, "previews": []},
        {"schema": 2, "name": "n", "info": {}, "previews": []},
        {"schema": 2, "kind": "d3dxSkinManage-Plus.mod-share", "name": "n", "info": {}, "previews": ["../secret.png"]},
    ],
)
def test_read_envelope_rejects_invalid_current_manifest(tmp_path, manifest):
    meta_dir = tmp_path / META_DIR
    meta_dir.mkdir()
    (meta_dir / "info.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(InvalidManagedMetadataError):
        read_envelope(tmp_path)


@pytest.mark.parametrize(
    "manifest",
    [
        {"schema": 1, "kind": "d3dxSkinManage-Plus.mod-share", "name": "n", "info": {}, "previews": []},
        {"schema": 999, "kind": "d3dxSkinManage-Plus.mod-share", "name": "n", "info": {}, "previews": []},
        {"kind": "d3dxSkinManage-Plus.mod-share", "info": {}, "previews": []},
        {"schema": 2, "kind": "d3dxSkinManage-Plus.mod-share", "name": "n", "future": 1},
    ],
)
def test_read_envelope_accepts_other_schema_and_missing_fields(tmp_path, manifest):
    meta_dir = tmp_path / META_DIR
    meta_dir.mkdir()
    (meta_dir / "info.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    got = read_envelope(tmp_path)
    assert got is not None
    assert isinstance(got["manifest"]["name"], str)
    assert isinstance(got["manifest"]["info"], dict)
    assert isinstance(got["manifest"]["previews"], list)


def test_read_envelope_accepts_current_manifest(tmp_path):
    meta_dir = tmp_path / META_DIR
    meta_dir.mkdir()
    manifest = build_manifest({"name": "n", "info": {}}, [])
    (meta_dir / "info.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert read_envelope(tmp_path)["manifest"] == manifest


# 端到端：导出（带信封）→ 导入还原


def test_export_with_meta_then_import_restores(temp_data_dir, tmp_path):
    from src.core.mod_exporter import ExportItem, ModExporter
    from src.core.mod_manager import ModManager
    from src.core.mod_package import META_DIR, write_container_marker
    from src.models.mod import AgeRating, Mod

    # 当前格式容器：本体原图 + 容器级手动补充图。
    container = tmp_path / "src_container"
    src_mod = container / "src_mod"
    src_mod.mkdir(parents=True)
    (src_mod / "dummy.ini").write_text("[Key]\nx=1\n", encoding="utf-8")
    original = _png(src_mod / "original.png")
    supplemental_dir = container / META_DIR / "previews"
    supplemental_dir.mkdir(parents=True)
    supplemental = _png(supplemental_dir / "cover.png")
    write_container_marker(container, src_mod.name, "0" * 40, imported_at="2026-10-05T10:00:00")
    original_hash = Mod.compute_sha1(str(src_mod))

    # 导出为带信封 ZIP
    dest = tmp_path / "out"
    dest.mkdir()
    meta = {
        "name": "漂亮皮肤",
        "info": {
            "author": "Alice",
            "description": "very nice",
            "version": "1.0",
            "url": "https://x",
            "age_rating": "R",
            "tags": ["cute", "blue"],
        },
    }
    item = ExportItem(
        name="漂亮皮肤", src_path=str(src_mod), meta=meta, preview_paths=[str(original), str(supplemental)]
    )
    res = ModExporter().export_one(item, dest, fmt="zip", include_meta=True)
    assert res.ok
    out_zip = Path(res.out_path)
    assert out_zip.suffix == ".zip"
    with zipfile.ZipFile(out_zip, "r") as zf:
        envelope_previews = [n for n in zf.namelist() if "/.d3dxmeta/previews/" in n.replace("\\", "/")]
    assert any(n.endswith("/cover.png") for n in envelope_previews)
    assert not any(n.endswith("/original.png") for n in envelope_previews)

    # 导入产物
    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色X", node_type="character")
    mod = mm.import_mod(source_path=str(out_zip), node_id=char.id)
    assert mod is not None

    # 元数据还原
    assert mod.info.author == "Alice"
    assert mod.info.description == "very nice"
    assert mod.info.age_rating == AgeRating.R
    assert set(mod.info.tags) >= {"cute", "blue"}
    # 未自定义显示名时采用信封名
    assert mod.display_name == "漂亮皮肤"

    # library 副本进容器；sidecar 落**容器级**（本体之外、与本体同级）。
    # mod.path 指向容器内本体，其父目录即容器；容器内含 .d3dxmod marker + .d3dxmeta。
    lib_path = Path(mod.path)
    container = lib_path.parent
    assert (container / ".d3dxmod").is_file()
    assert (container / META_DIR / "info.json").is_file()
    # 内容哈希仍与原始 mod 一致（compute_sha1 跳过 .d3dxmeta，且只算本体）
    assert mod.content_hash == original_hash
    assert read_container_marker(container)["content_hash"] == original_hash

    # 手动补充封面被带入并还原；本体原图仍由本体扫描得到
    assert any("cover" in p for p in mod.preview_images)
    assert any("original" in p for p in mod.preview_images)
    imported_meta_previews = Path(mod.path).parent / META_DIR / "previews"
    assert (imported_meta_previews / "cover.png").is_file()
    assert not (imported_meta_previews / "original.png").exists()
    assert not (temp_data_dir / "data" / mm._res_dir("game_a") / "previews").exists()


def test_import_current_container_preserves_supplemental_previews(
    temp_data_dir,
    tmp_path,
):
    from src.core.mod_manager import ModManager
    from src.core.mod_package import (
        META_DIR,
        build_local_manifest,
        write_container_marker,
        write_sidecar,
    )

    container = tmp_path / "shared-container"
    body = container / "SharedMod"
    body.mkdir(parents=True)
    (body / "mod.ini").write_text("[Key]\nx=1\n", encoding="utf-8")
    write_container_marker(container, body.name, "1" * 40, imported_at="2026-10-05T10:00:00")
    preview_dir = container / META_DIR / "previews"
    preview_dir.mkdir(parents=True)
    _png(preview_dir / "manual.png")
    assert write_sidecar(container, build_local_manifest({"name": "Shared Mod", "info": {}}))

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色容器", node_type="character")
    mod = mm.import_mod(str(container), node_id=char.id)
    assert mod is not None
    imported_preview_dir = Path(mod.path).parent / META_DIR / "previews"
    assert (imported_preview_dir / "manual.png").is_file()
    assert any(Path(p).name == "manual.png" for p in mod.preview_images)
    assert not (temp_data_dir / "data" / mm._res_dir("game_a") / "previews").exists()


def test_import_rejects_current_container_with_broken_marker(
    temp_data_dir,
    tmp_path,
):
    from src.core.mod_manager import ModManager

    container = tmp_path / "BrokenContainer"
    body = container / "BrokenContainer"
    body.mkdir(parents=True)
    (body / "mod.ini").write_text("[Key]\nx=1\n", encoding="utf-8")
    marker = container / MOD_CONTAINER_MARKER
    marker.write_text("{broken", encoding="utf-8")

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色损坏容器", node_type="character")

    assert mm.import_mod(str(container), node_id=char.id) is None
    assert (body / "mod.ini").is_file()
    assert marker.read_text(encoding="utf-8") == "{broken"
    assert mm.get_mods(node_id=char.id) == []


def test_import_degrades_current_container_without_sidecar(
    temp_data_dir,
    tmp_path,
):
    """结构有效但缺少描述时按普通 Mod 导入，描述元数据不决定资源能否部署。"""
    from src.core.mod_manager import ModManager
    from src.models.mod import Mod

    container = tmp_path / "MissingSidecar"
    body = container / "MissingSidecar"
    body.mkdir(parents=True)
    (body / "mod.ini").write_text("[Key]\nx=1\n", encoding="utf-8")
    assert write_container_marker(container, body.name, Mod.compute_sha1(body), imported_at="2026-10-05T10:00:00")

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色缺 sidecar", node_type="character")

    mod = mm.import_mod(str(container), node_id=char.id)
    assert mod is not None, "结构完好只缺描述不应拒绝导入"
    assert not mod.info.author  # 描述整份丢弃，不猜不补
    assert (body / "mod.ini").is_file()  # 不动用户源
    assert mm.get_mods(node_id=char.id) == [mod]
    # 落库后由程序重写本地 sidecar，库内容器仍是完整的当前格式
    imported_body = Path(mod.path)
    assert (imported_body.parent / MOD_CONTAINER_MARKER).is_file()
    assert (imported_body.parent / ".d3dxmeta" / "info.json").is_file()


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("坏 JSON", "{not json"),
        ("非本程序信封", '{"schema": 2, "kind": "someone-else.mod-share", "name": "x", "info": {}, "previews": []}'),
    ],
)
def test_import_degrades_when_share_envelope_unusable(
    temp_data_dir,
    tmp_path,
    label,
    payload,
):
    """分享元数据无法解析或 kind 不匹配时，应舍弃元数据并继续导入本体。

    schema 版本差异不触发降级；JSON 结构错误及来源标识不匹配时报告降级原因。
    """
    from src.core.import_format import DEGRADED_METADATA_NOTICE
    from src.core.mod_manager import ModManager

    source = tmp_path / f"Envelope_{label}"
    source.mkdir()
    (source / "mod.ini").write_text("[Key]\nx=1\n", encoding="utf-8")
    meta_dir = source / ".d3dxmeta"
    meta_dir.mkdir()
    (meta_dir / "info.json").write_text(payload, encoding="utf-8")

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name=f"角色{label}", node_type="character")

    mod = mm.import_mod(str(source), node_id=char.id)
    assert mod is not None, f"{label} 信封不应导致整条导入失败"
    assert not mod.info.author
    assert (meta_dir / "info.json").read_text(encoding="utf-8") == payload  # 不动源

    # 坏信封绝不进入 library
    imported_meta = Path(mod.path).parent / ".d3dxmeta" / "info.json"
    assert imported_meta.read_text(encoding="utf-8") != payload

    # 降级理由随准备结果上报，UI 才能在明细行显示「无元数据」
    prepared = prepare_import_source(
        source,
        mm._archive,
        "folder",
        False,
        temp_data_dir / "data" / "temp",
    )
    assert prepared is not None
    assert prepared.metadata_notice == DEGRADED_METADATA_NOTICE
    prepared.cleanup()


def test_import_adopts_envelope_from_other_schema(temp_data_dir, tmp_path):
    """版本标记和未知字段不影响分享元数据中有效字段的读取。"""
    from src.core.mod_manager import ModManager

    source = tmp_path / "FutureEnvelope"
    source.mkdir()
    (source / "mod.ini").write_text("[Key]\nx=1\n", encoding="utf-8")
    meta_dir = source / ".d3dxmeta"
    meta_dir.mkdir()
    (meta_dir / "info.json").write_text(
        json.dumps(
            {
                "schema": 999,
                "kind": "d3dxSkinManage-Plus.mod-share",
                "name": "未来包",
                "info": {"author": "Alice", "description": "来自新版"},
                "previews": [],
                "future_field": {"whatever": 1},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色未来信封", node_type="character")

    mod = mm.import_mod(str(source), node_id=char.id)
    assert mod is not None
    assert mod.info.author == "Alice"
    assert mod.info.description == "来自新版"


@pytest.mark.parametrize("lib_format", ["folder", "7z"])
def test_import_unwraps_recompressed_container_body(
    temp_data_dir,
    tmp_path,
    lib_format,
):
    """把库内整个容器压成包再导入 → 按 marker 脱壳，不套娃、hash 不漂移。

    容器是存储外壳、不是 Mod 内容。不脱壳的话外壳会被当普通内容重新打包，本体里凭空
    多出一层同名目录、content_hash 随之漂移（查重失效：同一个 Mod 导两次算两份）。
    """
    from src.core.mod_manager import ModManager
    from src.models.mod import Mod

    container = tmp_path / "Recompressed"
    body = container / "Recompressed"
    body.mkdir(parents=True)
    (body / "mod.ini").write_text("[Key]\nx=1\n", encoding="utf-8")
    (body / "tex.dds").write_bytes(b"DDS payload")
    original_hash = Mod.compute_sha1(str(body))
    assert write_container_marker(container, body.name, original_hash, imported_at="2026-10-05T10:00:00")
    assert write_envelope_to_folder(container, {"name": "原名", "info": {"author": "Alice"}}, [])

    # 用户在资源管理器里把整个容器压成 zip
    archive = tmp_path / "Recompressed.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in container.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(container.parent).as_posix())

    mm = ModManager()
    mm.set_game("game_a")
    mm._config.set("import.format", lib_format)
    char = mm.add_character(name=f"角色脱壳{lib_format}", node_type="character")

    mod = mm.import_mod(str(archive), node_id=char.id)
    assert mod is not None
    # 关键不变量：内容哈希与原始本体逐位相同（脱壳成功 → 无多余目录层）
    assert mod.content_hash == original_hash
    assert mod.info.author == "Alice"  # 容器级信封仍被还原

    imported = Path(mod.path)
    if imported.is_dir():
        names = sorted(p.relative_to(imported).as_posix() for p in imported.rglob("*"))
        assert names == ["mod.ini", "tex.dds"], f"本体多了目录层: {names}"


def test_import_rejects_archived_container_with_broken_marker(
    temp_data_dir,
    tmp_path,
):
    from src.core.mod_manager import ModManager

    archive = tmp_path / "BrokenContainer.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("BrokenContainer/.d3dxmod", "{broken")
        zf.writestr("BrokenContainer/BrokenContainer/mod.ini", "[Key]\nx=1\n")
    original_bytes = archive.read_bytes()

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色损坏归档容器", node_type="character")

    assert mm.import_mod(str(archive), node_id=char.id) is None
    assert archive.read_bytes() == original_bytes
    assert mm.get_mods(node_id=char.id) == []


def test_import_rolls_back_when_local_sidecar_write_fails(
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    from src.core import mod_intake
    from src.core.mod_manager import ModManager

    source = tmp_path / "SidecarFail"
    source.mkdir()
    (source / "mod.ini").write_text("[Key]\nx=1\n", encoding="utf-8")

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色 sidecar 失败", node_type="character")
    # 在纳管事务实际使用的元数据写入点注入故障。
    monkeypatch.setattr(mod_intake, "write_sidecar", lambda *_a, **_kw: False)

    assert mm.import_mod(str(source), node_id=char.id) is None
    assert (source / "mod.ini").is_file()
    assert mm.get_mods(node_id=char.id) == []
    assert not (mm._get_character_library(char.id) / "SidecarFail").exists()


def test_import_rejects_container_whose_body_link_escapes_source(
    temp_data_dir,
    tmp_path,
):
    from src.core.mod_manager import ModManager

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.ini").write_text("[Private]\n", encoding="utf-8")
    container = tmp_path / "LinkedContainer"
    container.mkdir()
    body = container / "LinkedContainer"
    _link_directory(body, outside)
    _write_raw_container_marker(container, _current_marker(body))
    marker_text = (container / MOD_CONTAINER_MARKER).read_text(encoding="utf-8")

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色越界容器", node_type="character")

    assert mm.import_mod(str(container), node_id=char.id) is None
    assert container.is_dir()
    assert body.is_dir()
    assert (container / MOD_CONTAINER_MARKER).read_text(encoding="utf-8") == marker_text
    assert (outside / "private.ini").read_text(encoding="utf-8") == "[Private]\n"
    assert mm.get_mods(node_id=char.id) == []


def test_import_plain_zip_has_no_envelope_metadata(temp_data_dir, tmp_path):
    from src.core.mod_manager import ModManager

    z = tmp_path / "plain.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("PlainMod/dummy.ini", "[X]\n")

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色Y", node_type="character")
    mod = mm.import_mod(source_path=str(z), node_id=char.id)
    assert mod is not None
    # 向后兼容：无信封 → 元数据保持空
    assert mod.info.author == ""
    assert mod.info.tags == []


def test_gb_update_source_survives_export_import(qtbot, temp_data_dir, tmp_path):
    import zipfile as _zip
    from unittest.mock import MagicMock

    from src.core.config import Config
    from src.core.mod_exporter import ExportItem, ModExporter
    from src.core.mod_manager import ModManager
    from src.core.mod_update_service import ModUpdateService
    from src.services.gamebanana.models import GbInstallMetadata

    src_zip = tmp_path / "orig.zip"
    with _zip.ZipFile(src_zip, "w") as zf:
        zf.writestr("GBMod/dummy.ini", "[X]\n")

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色G", node_type="character")
    gb_meta = GbInstallMetadata(
        mod_id=555,
        file_id=777,
        file_md5="abc",
        file_ts=1700000000,
        page_url="https://gamebanana.com/mods/555",
        version="1.0",
        submitter_id=9,
        submitter_name="Auth",
        last_checked_iso="2026-01-01T00:00:00",
    )
    orig = mm.import_mod(source_path=str(src_zip), node_id=char.id, gb_meta=gb_meta)
    assert orig is not None
    assert orig.info.custom_data["gamebanana"]["mod_id"] == 555

    # 导出（带信封）
    dest = tmp_path / "out"
    dest.mkdir()
    item = ExportItem(
        name=orig.display_name,
        src_path=orig.path,
        meta=share_meta_from_mod(orig),
        preview_paths=list(orig.preview_images or []),
    )
    res = ModExporter(library_root=str(mm._get_library_root())).export_one(
        item, dest, fmt="zip", preserve_tree=False, include_meta=True
    )
    assert res.ok

    # 删除原 Mod，再导入产物
    mm.delete_mod(orig.mod_id)
    reimported = mm.import_mod(source_path=res.out_path, node_id=char.id)
    assert reimported is not None

    gb = reimported.info.custom_data.get("gamebanana") or {}
    assert gb.get("mod_id") == 555
    assert gb.get("file_id") == 777
    assert gb.get("page_url") == "https://gamebanana.com/mods/555"
    assert gb.get("last_checked_iso") == ""  # 重置：接收方从未检测开始
    assert "ignored" not in gb  # 不继承本地忽略偏好

    # 核心：更新检测会把它纳入候选（_collect_jobs 靠 custom_data["gamebanana"]["mod_id"]）
    svc = ModUpdateService(mm, Config(), MagicMock())
    jobs, _ = svc._collect_jobs(force=True)
    assert reimported.mod_id in {mid for mid, _m in jobs}


def test_folder_import_reads_envelope_keeps_source_clean_hash(temp_data_dir, tmp_path):
    """目录导入应恢复分享元数据，并保留源目录中的信封。

    库内副本移除信封后计算内容哈希，与压缩包导入和不含信封的输入保持一致。
    """
    from src.core.mod_manager import ModManager
    from src.models.mod import AgeRating, Mod

    src = tmp_path / "MyFolderMod"
    src.mkdir()
    (src / "mod.ini").write_text("[X]\nk=v\n", encoding="utf-8")
    clean_hash = Mod.compute_sha1(str(src))  # 写信封前的干净哈希
    cover = _png(tmp_path / "cover.png")
    meta = {
        "name": "文件夹皮",
        "info": {
            "author": "Bob",
            "age_rating": "R",
            "tags": ["x"],
            "url": "https://gamebanana.com/mods/999",
            "custom_data": {
                "gamebanana": {"mod_id": 999, "file_id": 5, "file_md5": "m", "file_ts": 100, "last_checked_iso": ""}
            },
        },
    }
    assert write_envelope_to_folder(src, meta, [str(cover)])
    assert (src / META_DIR).is_dir()

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色F", node_type="character")
    mod = mm.import_mod(source_path=str(src), node_id=char.id)
    assert mod is not None

    # 元数据还原
    assert mod.info.author == "Bob"
    assert mod.info.age_rating == AgeRating.R
    assert mod.display_name == "文件夹皮"
    assert (mod.info.custom_data.get("gamebanana") or {}).get("mod_id") == 999
    # 库副本落入容器；sidecar 在**容器级**（本体之外、与本体同级），哈希仍为去信封后的
    # 干净值（compute_sha1 跳过 .d3dxmeta）。mod.path 指向容器内本体、其父目录即容器。
    container = Path(mod.path).parent
    assert (container / ".d3dxmod").is_file()
    assert (container / META_DIR / "info.json").is_file()
    assert mod.content_hash == clean_hash
    # 绝不删除用户源目录里的信封
    assert (src / META_DIR).is_dir()
    # 外部封面随信封还原
    assert any("cover" in p for p in mod.preview_images)


# 当前容器标记格式。


def _write_raw_container_marker(container: Path, payload) -> None:
    container.mkdir(parents=True, exist_ok=True)
    (container / MOD_CONTAINER_MARKER).write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _current_marker(body_path: Path, **overrides) -> dict:
    marker = {
        "schema": CONTAINER_SCHEMA_VERSION,
        "kind": CONTAINER_KIND,
        "body": body_path.name,
        "body_type": "archive" if body_path.is_file() else "folder",
        "content_hash": _VALID_HASH,
    }
    marker.update(overrides)
    return marker


def test_current_container_marker_roundtrip_resolves_declared_body(tmp_path):
    container = tmp_path / "Current"
    body = container / "Current"
    body.mkdir(parents=True)

    assert write_container_marker(container, body.name, _VALID_HASH, imported_at="2026-10-05T10:00:00")

    marker = read_container_marker(container)
    assert marker == _current_marker(body, mod_id=marker["mod_id"], imported_at="2026-10-05T10:00:00")
    from src.core.mod_identity import validate_mod_id

    assert validate_mod_id(marker["mod_id"]) == marker["mod_id"]
    assert resolve_body_path(container) == body


def test_read_container_marker_missing_returns_none(tmp_path):
    container = tmp_path / "NoMarker"
    container.mkdir()

    assert read_container_marker(container) is None


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {
            "schema": CONTAINER_SCHEMA_VERSION,
            "kind": "wrong-kind",
            "body": "Body",
            "body_type": "folder",
            "content_hash": _VALID_HASH,
        },
    ],
)
def test_read_container_marker_rejects_broken_identity(tmp_path, payload):
    container = tmp_path / "Invalid"
    (container / "Body").mkdir(parents=True)
    _write_raw_container_marker(container, payload)

    with pytest.raises(InvalidManagedMetadataError):
        read_container_marker(container)


@pytest.mark.parametrize("schema", [2, True, 1.0, 999, None, "1"])
def test_read_container_marker_accepts_other_schema(tmp_path, schema):
    container = tmp_path / f"Schema_{schema!r}"
    body = container / "Body"
    body.mkdir(parents=True)
    payload = {
        "schema": schema,
        "kind": CONTAINER_KIND,
        "body": "Body",
        "body_type": "folder",
        "content_hash": _VALID_HASH,
    }
    _write_raw_container_marker(container, payload)

    marker = read_container_marker(container)
    assert marker is not None
    assert marker["content_hash"] == _VALID_HASH
    assert resolve_body_path(container) == body


@pytest.mark.parametrize("body_type", [None, "vhd", 1])
def test_read_container_marker_infers_unknown_body_type(tmp_path, body_type):
    container = tmp_path / "InferType"
    body = container / "Body"
    body.mkdir(parents=True)
    payload = {
        "schema": CONTAINER_SCHEMA_VERSION,
        "kind": CONTAINER_KIND,
        "body": "Body",
        "content_hash": _VALID_HASH,
    }
    if body_type is not None:
        payload["body_type"] = body_type
    _write_raw_container_marker(container, payload)

    assert read_container_marker(container)["body_type"] == "folder"


@pytest.mark.parametrize("content_hash", ["", "a" * 39, "A" * 40, "g" * 40, 123])
def test_write_container_marker_rejects_invalid_content_hash(tmp_path, content_hash):
    container = tmp_path / "InvalidHash"
    body = container / "Body"
    body.mkdir(parents=True)

    with pytest.raises(ValueError):
        write_container_marker(container, body.name, content_hash, imported_at="2026-10-05T10:00:00")

    assert not (container / MOD_CONTAINER_MARKER).exists()


@pytest.mark.parametrize("content_hash", ["", "a" * 39, "A" * 40, "g" * 40, 123])
def test_read_container_marker_rejects_invalid_content_hash(tmp_path, content_hash):
    container = tmp_path / "InvalidHash"
    body = container / "Body"
    body.mkdir(parents=True)
    _write_raw_container_marker(
        container,
        _current_marker(body, content_hash=content_hash),
    )

    with pytest.raises(InvalidManagedMetadataError):
        read_container_marker(container)


@pytest.mark.parametrize(
    "body_name",
    [".", "..", "../outside", "nested/Body", "nested\\Body", "C:\\outside"],
)
def test_read_container_marker_rejects_non_basename_body(tmp_path, body_name):
    container = tmp_path / "Traversal"
    (container / "Body").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_raw_container_marker(
        container,
        _current_marker(outside, body=body_name),
    )

    with pytest.raises(InvalidManagedMetadataError):
        resolve_body_path(container)


def test_read_container_marker_rejects_body_link_outside_container(tmp_path):
    container = tmp_path / "LinkedBody"
    container.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "mod.ini").write_text("[Key]\n", encoding="utf-8")
    body = container / "Body"
    _link_directory(body, outside)
    _write_raw_container_marker(container, _current_marker(body))

    with pytest.raises(InvalidManagedMetadataError):
        read_container_marker(container)


def test_resolve_body_path_does_not_guess_without_marker(tmp_path):
    container = tmp_path / "Unmanaged"
    (container / "OnlyCandidate").mkdir(parents=True)

    assert resolve_body_path(container) is None


def test_resolve_body_path_does_not_guess_after_broken_json(tmp_path):
    container = tmp_path / "Broken"
    (container / "OnlyCandidate").mkdir(parents=True)
    (container / MOD_CONTAINER_MARKER).write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError):
        resolve_body_path(container)


def test_read_container_marker_rejects_missing_body(tmp_path):
    container = tmp_path / "MissingBody"
    body = container / "MissingBody"
    _write_raw_container_marker(container, _current_marker(body))

    with pytest.raises(ValueError):
        read_container_marker(container)


@pytest.mark.parametrize(
    ("body_name", "body_type"),
    [("FolderBody", "archive"), ("Body.zip", "folder"), ("Body.txt", "archive")],
)
def test_read_container_marker_rejects_body_type_mismatch(
    tmp_path,
    body_name,
    body_type,
):
    container = tmp_path / "WrongType"
    body = container / body_name
    if body_name == "FolderBody":
        body.mkdir(parents=True)
    else:
        container.mkdir(parents=True)
        body.write_bytes(b"body")
    _write_raw_container_marker(
        container,
        _current_marker(body, body_type=body_type),
    )

    with pytest.raises(ValueError):
        read_container_marker(container)
