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
