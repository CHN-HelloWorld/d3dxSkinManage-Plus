"""验证外部目录及归档纳管、各格式导出和容器级补充预览。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from src.core.mod_intake import ModIntakeService
from src.core.mod_manager import ModManager
from src.core.mod_package import (
    MANIFEST_NAME,
    META_DIR,
    MOD_CONTAINER_MARKER,
    is_mod_container,
    read_container_marker,
    read_sidecar,
)
from src.models.mod import Mod
from tests.helpers import registered_games as registered_games
from tests.helpers import scan_and_sync


@pytest.fixture
def mm(temp_data_dir):
    m = ModManager()
    m.set_game("game_a")
    return m


def _drop_folder(parent: Path, name: str) -> Path:
    """模拟外部 Mod 目录进入角色资源库。"""
    d = parent / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "mod.ini").write_text("[Constants]\n", encoding="utf-8")
    (d / "tex.dds").write_bytes(b"DDS ")
    return d


def _drop_zip(parent: Path, name: str, top: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    z = parent / f"{name}.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(f"{top}/mod.ini", "[K]\n")
    return z


def _drop_rar(parent: Path, name: str) -> Path:
    """直接生成含 ThetaSkin/mod.ini 的无压缩 RAR5，无需 rar.exe。"""
    parent.mkdir(parents=True, exist_ok=True)
    archive = parent / f"{name}.rar"
    archive.write_bytes(
        bytes.fromhex(
            "526172211a070100c51a3332030100000be1f7051e02020c040c20151ea34f"
            "0000115468657461536b696e2f6d6f642e696e695b436f6e7374616e74735d"
            "0a19b23a3503050000"
        )
    )
    return archive


# 本体预览原地引用；容器预览目录仅保存补充图片。


def test_body_original_preview_not_duplicated_into_container_meta(mm):
    """目录本体中的原始预览应原地引用。

    构造目录本体以验证现有容器的读取行为；补充预览仍保存在容器元数据目录。
    """
    from src.core.mod_package import write_container_marker

    char = mm.add_character(name="示例角色戊", node_type="character")
    lib = mm._get_character_library(char.id)
    container = lib / "OrigPrevMod"
    d = container / "OrigPrevMod"
    d.mkdir(parents=True)
    (d / "mod.ini").write_text("[K]\n", encoding="utf-8")
    (d / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\n")  # 本体自带预览图
    assert write_container_marker(container, d.name, "0" * 40, imported_at="2026-10-05T10:00:00")

    added = scan_and_sync(mm)
    assert added == 1
    mod = mm.get_mods(node_id=char.id)[0]
    body = Path(mod.path)  # 容器/OrigPrevMod
    assert body.is_dir()

    # 本体图被识别为预览图，且引用仍指向**本体子树内**（原地，未复制）
    assert any("preview.png" in p for p in mod.preview_images)
    cover = next(p for p in mod.preview_images if "preview.png" in p)
    assert str(body) in cover, "本体自带预览图应原地引用（在本体子树内）"

    # .d3dxmeta/previews/ 不该因本体图而产生（无手动/外来图时根本不建，或为空）
    prev_dir = body.parent / ".d3dxmeta" / "previews"
    assert not prev_dir.exists() or not any(prev_dir.iterdir()), "本体自带预览图被错误复制进了 .d3dxmeta/previews/"


# 外部目录导入后应生成包含归档本体和标记文件的容器。


def test_external_folder_drop_gets_containerized(mm):
    char = mm.add_character(name="示例角色戊", node_type="character")
    lib = mm._get_character_library(char.id)
    _drop_folder(lib, "EpsilonSkin")

    added = scan_and_sync(mm)
    assert added == 1

    container = lib / "EpsilonSkin"
    assert is_mod_container(container), "外部拖入的文件夹应被裹进容器（含 .d3dxmod）"
    assert (container / MOD_CONTAINER_MARKER).is_file()
    # 目录本体压缩后保存在容器内。
    body = container / "EpsilonSkin.7z"
    assert body.is_file(), f"容器内应有压缩档本体，实际: {list(container.iterdir())}"
    assert not (container / "EpsilonSkin").exists(), "不得再留下目录本体"
    # 容器级 sidecar（本体之外）
    assert (container / ".d3dxmeta" / "info.json").is_file()

    mod = mm.get_mods(node_id=char.id)[0]
    assert Path(mod.path) == body
    assert mod.node_id == char.id


# 外部 ZIP 归档纳管时保留本体，元数据写入容器。


def test_external_zip_drop_gets_containerized_not_injected(mm):
    mm._config.set("import.format", "zip")
    mm._config.set("import.strict_format", False)
    char = mm.add_character(name="示例角色戊", node_type="character")
    lib = mm._get_character_library(char.id)
    _drop_zip(lib, "EtaSkin", top="EtaSkin")

    added = scan_and_sync(mm)
    assert added == 1

    container = lib / "EtaSkin"
    assert is_mod_container(container)
    body = container / "EtaSkin.zip"
    assert body.is_file()
    # sidecar 落容器级、不注入归档内部（归档零改动）
    assert (container / ".d3dxmeta" / "info.json").is_file()
    with zipfile.ZipFile(body, "r") as zf:
        assert not any(".d3dxmeta" in n.replace("\\", "/").split("/") for n in zf.namelist()), (
            "容器化后 sidecar 不应注入归档内部"
        )

    mod = mm.get_mods(node_id=char.id)[0]
    assert Path(mod.path) == body


def test_external_rar_drop_gets_containerized_without_rewriting_body(mm):
    mm._config.set("import.format", "rar")
    mm._config.set("import.strict_format", False)
    char = mm.add_character(name="示例角色戊", node_type="character")
    lib = mm._get_character_library(char.id)
    source = _drop_rar(lib, "ThetaSkin")
    original_bytes = source.read_bytes()

    assert scan_and_sync(mm) == 1

    container = lib / "ThetaSkin"
    body = container / "ThetaSkin.rar"
    marker = read_container_marker(container)
    assert is_mod_container(container)
    assert marker is not None
    assert marker["body"] == body.name
    assert marker["body_type"] == "archive"
    assert body.read_bytes() == original_bytes
    assert not source.exists()
    assert (container / ".d3dxmeta" / "info.json").is_file()

    mods = mm.get_mods(node_id=char.id)
    assert len(mods) == 1
    assert Path(mods[0].path) == body
    expected_hash = Mod.compute_sha1(str(body))
    assert marker["content_hash"] == expected_hash
    assert mods[0].content_hash == expected_hash
    assert scan_and_sync(mm) == 0
    assert len(mm.get_mods(node_id=char.id)) == 1


def test_scan_skips_container_with_broken_marker_instead_of_guessing_body(mm):
    char = mm.add_character(name="示例角色戊", node_type="character")
    lib = mm._get_character_library(char.id)
    container = lib / "BrokenMarker"
    body = _drop_folder(container, "BrokenMarker")
    (container / MOD_CONTAINER_MARKER).write_text("{broken", encoding="utf-8")

    assert scan_and_sync(mm) == 0
    assert body.is_dir()
    assert mm.get_mods(node_id=char.id) == []


def test_broken_marker_container_is_reported_for_disposal(mm):
    """标记损坏的容器应通过 invalid_metadata 上报，以便界面提供处置入口。"""
    char = mm.add_character(name="示例角色戊", node_type="character")
    lib = mm._get_character_library(char.id)
    container = lib / "BrokenMarkerReported"
    _drop_folder(container, "BrokenMarkerReported")
    (container / MOD_CONTAINER_MARKER).write_text("{broken", encoding="utf-8")

    invalid: list[str] = []
    requests = mm.plan_scan_intakes(invalid_metadata=invalid)

    assert requests == [], "坏 marker 容器不得进入纳管计划"
    assert [Path(p) for p in invalid] == [container], "坏 marker 容器必须上报给调用方处置"


def test_container_with_broken_info_json_is_adopted_with_degradation(mm):
    """结构完整但描述元数据损坏的容器仍可纳管，忽略无效描述。"""
    char = mm.add_character(name="示例角色戊", node_type="character")
    lib = mm._get_character_library(char.id)
    _drop_folder(lib, "DegradedInfo")

    assert scan_and_sync(mm) == 1
    mod = mm.get_mods(node_id=char.id)[0]
    info = Path(mod.path).parent / META_DIR / MANIFEST_NAME
    assert info.is_file()

    # 模拟「剪出库外→写坏 info.json→再投回」：磁盘内容留着，只去掉索引记录
    assert mm._index.delete_mod(mod.mod_id)
    info.write_text("{ not valid json", encoding="utf-8")

    invalid: list[str] = []
    requests = mm.plan_scan_intakes(invalid_metadata=invalid)
    assert invalid == [], "坏 info.json 属描述类，不得判为无效容器"
    assert len(requests) == 1, "坏 info.json 的容器必须仍被发现"

    service = ModIntakeService(mm)
    prepared = service.prepare(requests[0])
    assert prepared.metadata_notice, "描述整份丢弃时必须记降级告知"
    assert service.commit(prepared) is not None

    # 「整份丢弃」必须写入磁盘：容器不能停在「marker 合法、sidecar 坏」的不一致态
    assert read_sidecar(Path(prepared.body_path)) is not None


def test_external_folder_drop_restores_original_when_marker_write_fails(
    mm,
    monkeypatch,
):
    char = mm.add_character(name="示例角色戊", node_type="character")
    lib = mm._get_character_library(char.id)
    source = _drop_folder(lib, "MarkerFailureFolder")
    monkeypatch.setattr(
        "src.core.mod_package.write_container_marker",
        lambda *_args, **_kwargs: False,
    )

    assert scan_and_sync(mm) == 0
    assert (source / "mod.ini").is_file()
    assert not (source / source.name).exists()
    assert not (source / MOD_CONTAINER_MARKER).exists()
    assert mm.get_mods(node_id=char.id) == []


def test_external_archive_drop_restores_original_when_marker_write_fails(
    mm,
    monkeypatch,
):
    char = mm.add_character(name="示例角色戊", node_type="character")
    lib = mm._get_character_library(char.id)
    source = _drop_zip(lib, "MarkerFailureArchive", top="MarkerFailureArchive")
    original_bytes = source.read_bytes()
    monkeypatch.setattr(
        "src.core.mod_package.write_container_marker",
        lambda *_args, **_kwargs: False,
    )

    assert scan_and_sync(mm) == 0
    assert source.read_bytes() == original_bytes
    assert not (lib / "MarkerFailureArchive").exists()
    assert mm.get_mods(node_id=char.id) == []


def test_external_drop_is_idempotent(mm):
    char = mm.add_character(name="示例角色戊", node_type="character")
    lib = mm._get_character_library(char.id)
    _drop_folder(lib, "IdemDrop")
    assert scan_and_sync(mm) == 1
    assert scan_and_sync(mm) == 0
    inner = lib / "IdemDrop" / "IdemDrop" / "IdemDrop"
    assert not inner.exists(), "不应产生嵌套容器"


# 补充预览写入容器元数据目录，不改变归档本体及内容摘要。


def test_save_preview_never_touches_zip_body(mm, tmp_path):
    mm._config.set("import.format", "zip")
    char = mm.add_character(name="示例角色戊", node_type="character")
    lib = mm._get_character_library(char.id)
    _drop_zip(lib, "PrevZip", top="PrevZip")
    scan_and_sync(mm)
    mod = mm.get_mods(node_id=char.id)[0]
    body = Path(mod.path)
    hash_before = Mod.compute_sha1(str(body))
    members_before = set(zipfile.ZipFile(body).namelist())

    img = tmp_path / "cover.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert mm.save_preview(mod.mod_id, str(img))

    # 归档本体一个字节不动（预览图绝不注入归档）
    assert Mod.compute_sha1(str(body)) == hash_before
    assert set(zipfile.ZipFile(body).namelist()) == members_before
    # 补充预览保存在容器的 .d3dxmeta/previews 目录，并登记数据库引用。
    m2 = mm._index.get_mod(mod.mod_id)
    cover = next(p for p in m2.preview_images if "cover" in p)
    assert (body.parent / ".d3dxmeta" / "previews").is_dir()
    assert Path(cover).parent == body.parent / ".d3dxmeta" / "previews"
    assert Path(cover).is_file()


def test_preview_travels_with_container_copy(mm, tmp_path):
    import shutil as _sh

    char = mm.add_character(name="示例角色戊", node_type="character")
    lib = mm._get_character_library(char.id)
    _drop_folder(lib, "TravelMod")
    scan_and_sync(mm)
    mod = mm.get_mods(node_id=char.id)[0]
    img = tmp_path / "cover.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert mm.save_preview(mod.mod_id, str(img))

    # 预览图物理存在于容器内 .d3dxmeta/previews/
    container = Path(mod.path).parent
    prev_dir = container / ".d3dxmeta" / "previews"
    assert prev_dir.is_dir() and any(prev_dir.iterdir())

    # 模拟「用户直接复制整个容器发给对方」：拷贝容器目录，预览图应随之复制
    copied = tmp_path / "TravelMod"
    _sh.copytree(str(container), str(copied))
    assert (copied / ".d3dxmeta" / "previews").is_dir()
    assert any((copied / ".d3dxmeta" / "previews").iterdir()), "复制容器后预览图丢失"


# 压缩格式导出应保留归档本体。


def test_export_7z_container_body_keeps_native_format(mm, tmp_path):
    """压缩格式导出应直接复制 7z 本体，保留扩展名和原始字节。"""
    import py7zr

    from src.core.mod_exporter import ExportItem, ModExporter

    char = mm.add_character(name="示例角色戊", node_type="character")
    lib = mm._get_character_library(char.id)
    container = lib / "ThetaUnit"
    container.mkdir(parents=True)
    body = container / "ThetaUnit.7z"
    with py7zr.SevenZipFile(str(body), "w") as z:
        z.writestr(b"[Constants]\n", "ThetaUnit/mod.ini")
    body_bytes = body.read_bytes()

    dest = tmp_path / "out"
    dest.mkdir()
    item = ExportItem(name="ThetaUnit", src_path=str(body))
    res = ModExporter(library_root=str(mm._get_library_root())).export_one(item, dest, fmt="zip", preserve_tree=False)
    assert res.ok, f"7z 源导出应成功，实际: {res.error}"
    # 保留原生格式（不转 zip）：导出物是 .7z
    assert Path(res.out_path).suffix.lower() == ".7z"
    assert Path(res.out_path).is_file()
    # 直接复制原档、零重压：字节与源完全一致
    assert Path(res.out_path).read_bytes() == body_bytes


def test_export_7z_container_body_as_folder(mm, tmp_path):
    import py7zr

    from src.core.mod_exporter import ExportItem, ModExporter

    char = mm.add_character(name="示例角色戊", node_type="character")
    lib = mm._get_character_library(char.id)
    container = lib / "Iota"
    container.mkdir(parents=True)
    body = container / "Iota.7z"
    with py7zr.SevenZipFile(str(body), "w") as z:
        z.writestr(b"[Constants]\n", "Iota/mod.ini")

    dest = tmp_path / "out"
    dest.mkdir()
    item = ExportItem(name="Iota", src_path=str(body))
    res = ModExporter(library_root=str(mm._get_library_root())).export_one(
        item, dest, fmt="folder", preserve_tree=False
    )
    assert res.ok, f"7z 源导出为文件夹应成功，实际: {res.error}"
    assert Path(res.out_path).is_dir()
