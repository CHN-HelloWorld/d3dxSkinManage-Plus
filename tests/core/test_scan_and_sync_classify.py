"""验证增量纳管的路径分类、重名处理和嵌套类别发现。"""

from __future__ import annotations

import base64
import zipfile
from pathlib import Path

import pytest

from src.core.mod_manager import ModManager
from tests.helpers import registered_games as registered_games
from tests.helpers import scan_and_sync

_RAR_SHARE_FIXTURE = base64.b64decode(
    "UmFyIRoHAQDz4YLrCwEFBwAGAQGAgIAATPNtlDkCAwv/AASEASA2ya6HgAMAHVNoYXJlZFJh"
    "ci8uZDNkeG1ldGEvaW5mby5qc29uCgMC2NS2yhgx3QHB53wlREQk9lBE4rhW7FwMzW684Agi"
    "7EdFjVPwLd2jb8gj3pCbUHj9TUVFTymZ0TpmID3uP7k6mxFqKL77K61hlER/j2mfONTFVpBJ"
    "fm6FbTgdYrFCDgVac5utj9S/5XjC1N5CnIfHDM5dvFFHA5YCGlqa9m8n8pbomMODK3tAx0JW"
    "oEICAwuLAASLACDGTGZQgAAAJlNoYXJlZFJhci8uZDNkeG1ldGEvcHJldmlld3MvY292ZXIu"
    "cG5nCgMCW662yhgx3QFQTkctUFJFVklFV4YbFukuAgMLjAAEjAAgzPOTB4AAABJTaGFyZWRS"
    "YXIvLmQzZHhtb2QKAwJbrrbKGDHdAWlubmVyIG1hcmtlcpf5lnEtAgMLjQAEjQAgodcEEIAA"
    "ABFTaGFyZWRSYXIvbW9kLmluaQoDAluutsoYMd0BW0NvbnN0YW50c10NChqMPJUyAgMLAAEA"
    "EIAAABxTaGFyZWRSYXIvLmQzZHhtZXRhL3ByZXZpZXdzCgMCW662yhgx3QGQbtkuKQIDCwAB"
    "ABCAAAATU2hhcmVkUmFyLy5kM2R4bWV0YQoDAtjUtsoYMd0BK3H3nh8CAwsAAQAQgAAACVNo"
    "YXJlZFJhcgoDAluutsoYMd0BHXdWUQMFBAA="
)


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


def test_take_extracted_rar_share_envelope_preserves_staged_previews(tmp_path):
    from src.core.mod_package import META_DIR, MOD_CONTAINER_MARKER, take_envelope
    from src.services.archive import ArchiveService

    archive = tmp_path / "SharedRar.rar"
    archive.write_bytes(_RAR_SHARE_FIXTURE)

    extracted = ArchiveService().extract(archive, tmp_path / "extracted")
    assert extracted is not None
    candidates = [extracted, extracted / "SharedRar"]
    manifest, previews = take_envelope(candidates, tmp_path / "staged")

    assert manifest["name"] == "Shared RAR"
    assert manifest["info"]["tags"] == ["shared"]
    assert len(previews) == 1
    assert previews[0].read_bytes() == b"PNG-PREVIEW"
    for directory in candidates:
        assert not (directory / META_DIR).exists()
        assert not (directory / MOD_CONTAINER_MARKER).exists()


def test_scan_and_sync_rejects_rar_claiming_invalid_managed_identity(mm, monkeypatch):
    from src.services import archive as archive_module

    monkeypatch.setattr(archive_module, "find_winrar_rar_exe", lambda: None)
    char = mm.add_character(name="Target", node_type="character")
    library = mm._get_character_library(char.id)
    archive = library / "SharedRar.rar"
    archive.write_bytes(_RAR_SHARE_FIXTURE)
    original = archive.read_bytes()
    assert scan_and_sync(mm) == 0
    assert archive.read_bytes() == original
    assert mm.get_mods(node_id=char.id) == []
    assert not (library / "SharedRar").exists()


def test_scan_and_sync_rolls_back_when_rar_writer_fails(mm, monkeypatch):
    mm._config.set("import.format", "rar")
    monkeypatch.setattr(mm._archive, "compress", lambda *_args, **_kwargs: None)
    char = mm.add_character(name="示例角色丁", node_type="character")
    library = mm._get_character_library(char.id)
    archive = library / "SharedRar.rar"
    archive.write_bytes(_RAR_SHARE_FIXTURE)
    original = archive.read_bytes()

    assert scan_and_sync(mm) == 0

    assert archive.read_bytes() == original
    assert not (library / "SharedRar").exists()
    assert mm.get_mods(node_id=char.id) == []


def test_scan_and_sync_restores_direct_7z_share_envelope(mm, tmp_path):
    from src.core.mod_package import (
        META_DIR,
        read_sidecar,
        write_envelope_to_folder,
    )
    from src.services.archive import ArchiveService

    char = mm.add_character(name="示例角色丁", node_type="character")
    source = _make_mod_dir(tmp_path, "Shared7z")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert write_envelope_to_folder(
        source,
        {"name": "Shared 7z", "info": {"tags": ["shared"]}},
        [str(cover)],
    )
    archive = ArchiveService().compress(
        source,
        mm._get_character_library(char.id) / "Shared7z",
        "7z",
    )
    assert archive is not None

    assert scan_and_sync(mm) == 1

    mods = mm.get_mods(node_id=char.id)
    assert len(mods) == 1
    mod = mods[0]
    body = Path(mod.path)
    assert body == archive.parent / "Shared7z" / "Shared7z.7z"
    assert mod.name == "Shared 7z"
    assert mod.info.tags == ["shared"]
    assert read_sidecar(body)["name"] == "Shared 7z"
    assert (body.parent / META_DIR / "previews" / "cover.png").is_file()
    members = ArchiveService().list_contents(body)
    assert not any(META_DIR in name.replace("\\", "/").split("/") for name in members)
    assert not any(name.replace("\\", "/").rsplit("/", 1)[-1] == ".d3dxmod" for name in members)
    assert scan_and_sync(mm) == 0
    assert len(mm.get_mods(node_id=char.id)) == 1


def test_scan_and_sync_classifies_new_folder(mm):
    char = mm.add_character(name="示例角色丁", node_type="character")
    _make_mod_dir(mm._get_character_library(char.id), "NewSkin")

    added = scan_and_sync(mm)
    assert added == 1
    mods = mm.get_mods(node_id=char.id)
    assert len(mods) == 1
    assert mods[0].node_id == char.id  # 记录应关联实际类别。


def test_scan_and_sync_backfills_from_sidecar(mm):
    from src.core.mod_package import read_sidecar, write_envelope_to_folder
    from src.models.mod import AgeRating

    char = mm.add_character(name="示例角色丁", node_type="character")
    mod_dir = _make_mod_dir(mm._get_character_library(char.id), "Skin")
    assert write_envelope_to_folder(
        mod_dir,
        {
            "name": "示例角色丁·旗袍",
            "info": {"tags": ["旗袍"], "age_rating": "X"},
        },
    )

    added = scan_and_sync(mm)
    assert added == 1
    mod = mm.get_mods(node_id=char.id)[0]
    assert mod.name == "示例角色丁·旗袍"
    assert mod.info.tags == ["旗袍"]
    assert mod.info.age_rating == AgeRating.X
    assert read_sidecar(mod.path) is not None
    assert not (Path(mod.path) / ".d3dxmeta").exists()


def test_scan_and_sync_writes_sidecar_for_bare_mod(mm):
    """外部直接拖入、磁盘无 sidecar 的 Mod：登记后必须补写 .d3dxmeta，
    使其与正常导入路径对称，db 损坏后可从 sidecar 完整重建。"""
    from src.core.mod_package import read_sidecar

    char = mm.add_character(name="示例角色丁", node_type="character")
    mod_dir = _make_mod_dir(mm._get_character_library(char.id), "BareSkin")
    assert read_sidecar(mod_dir) is None  # 外部资源尚未生成分享元数据。

    added = scan_and_sync(mm)
    assert added == 1

    sc = read_sidecar(mod_dir)
    assert sc is not None  # 补写成功
    # 分类面包屑已写入 → db 损坏重建时能从 sidecar 复原归属
    assert "classification" not in sc["local"]


def test_scan_and_sync_does_not_overwrite_existing_sidecar(mm):
    from src.core.mod_package import read_sidecar, write_envelope_to_folder

    char = mm.add_character(name="示例角色丁", node_type="character")
    mod_dir = _make_mod_dir(mm._get_character_library(char.id), "SharedSkin")
    assert write_envelope_to_folder(
        mod_dir,
        {"name": "分享原名", "info": {"tags": ["原标签"]}},
    )

    added = scan_and_sync(mm)
    assert added == 1

    mod = mm.get_mods(node_id=char.id)[0]
    after = read_sidecar(mod.path)
    assert after["name"] == "分享原名"
    assert after["info"]["tags"] == ["原标签"]


def test_scan_and_sync_degrades_invalid_share_envelope_without_moving_source(mm):
    """直接投放的资源结构有效时，忽略无效描述并继续纳管。

    无效描述整体丢弃，不部分采用；纳管后写入完整的当前容器格式。
    """
    import json

    char = mm.add_character(name="示例角色丁", node_type="character")
    mod_dir = _make_mod_dir(mm._get_character_library(char.id), "BrokenShare")
    meta_dir = mod_dir / ".d3dxmeta"
    meta_dir.mkdir()
    info_path = meta_dir / "info.json"
    info_path.write_text(json.dumps({"schema": 1}), encoding="utf-8")

    assert scan_and_sync(mm) == 1
    mods = mm.get_mods(node_id=char.id)
    assert len(mods) == 1
    mod = mods[0]
    assert not mod.info.author and not mod.info.tags  # 描述整份丢弃，不猜不补
    body = Path(mod.path)
    assert body.is_file()  # 库内本体恒为压缩档（2026-08-27 起）
    # 坏信封不得进入 library：容器级 sidecar 是程序重写的当前格式，本体包内无残留信封
    from src.core.import_format import archive_has_reserved_payload
    from src.core.mod_package import read_sidecar
    from src.services.archive import ArchiveService

    assert read_sidecar(body) is not None
    assert not archive_has_reserved_payload(body, ArchiveService()), "坏信封必须在纳管时剥掉，不得随本体一起压进库内"


def test_scan_and_sync_restores_direct_zip_share_envelope(mm, tmp_path):
    from src.core.mod_package import (
        META_DIR,
        append_envelope_to_zip,
        read_sidecar,
    )

    char = mm.add_character(name="示例角色丁", node_type="character")
    archive = mm._get_character_library(char.id) / "SharedZip.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("SharedZip/mod.ini", "[K]\n")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert append_envelope_to_zip(
        archive,
        {"name": "分享压缩包", "info": {"tags": ["分享标签"]}},
        [str(cover)],
    )

    assert scan_and_sync(mm) == 1

    mod = mm.get_mods(node_id=char.id)[0]
    body = Path(mod.path)
    assert mod.name == "分享压缩包"
    assert mod.info.tags == ["分享标签"]
    assert read_sidecar(body)["name"] == "分享压缩包"
    assert (body.parent / META_DIR / "previews" / "cover.png").is_file()
    with zipfile.ZipFile(body, "r") as zf:
        assert not any(META_DIR in name.replace("\\", "/").split("/") for name in zf.namelist())
        assert not any(name.replace("\\", "/").rsplit("/", 1)[-1] == ".d3dxmod" for name in zf.namelist())


def test_scan_and_sync_degrades_invalid_zip_share_envelope(mm):
    """归档直接投放同样降级：坏信封被剥掉，Mod 照常纳管。

    与文件夹路径对称——两条纳管入口必须用同一完整性门槛（第 2 节产品边界）。
    """
    from src.core.mod_package import read_sidecar

    char = mm.add_character(name="示例角色丁", node_type="character")
    archive = mm._get_character_library(char.id) / "BrokenZip.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("BrokenZip/mod.ini", "[K]\n")
        zf.writestr("BrokenZip/.d3dxmeta/info.json", '{"schema": 1}')

    assert scan_and_sync(mm) == 1
    mods = mm.get_mods(node_id=char.id)
    assert len(mods) == 1
    mod = mods[0]
    assert not mod.info.author and not mod.info.tags  # 描述整份丢弃
    body = Path(mod.path)
    # 本地 sidecar 由程序按当前 schema 重写；断言不假设库内格式（默认 import.format
    # = folder，zip 直投会被解压，写死「body 仍是 zip」会误报）。schema==2 本身即证明
    # 源里那份 schema=1 的坏信封没有存活下来。
    sidecar = read_sidecar(body)
    assert sidecar is not None and sidecar["schema"] == 2


def test_scan_and_sync_rolls_back_when_sidecar_write_fails(mm, monkeypatch):
    char = mm.add_character(name="示例角色丁", node_type="character")
    mod_dir = _make_mod_dir(mm._get_character_library(char.id), "SidecarFail")

    monkeypatch.setattr("src.core.mod_intake.write_sidecar", lambda *_args, **_kwargs: False)

    assert scan_and_sync(mm) == 0
    assert mod_dir.is_dir()
    assert (mod_dir / "mod.ini").is_file()
    assert not (mod_dir / ".d3dxmod").exists()
    assert mm.get_mods(node_id=char.id) == []


# 资源发现须按规范化路径判重，并识别嵌套类别的目录标记。


def test_scan_and_sync_leak_substring_name_no_longer_swallowed(mm):
    """名称互为子串的不同路径均应正常登记。"""
    char = mm.add_character(name="示例角色丁", node_type="character")
    lib = mm._get_character_library(char.id)
    _make_mod_dir(lib, "SigmaSwimsuit")
    assert scan_and_sync(mm) == 1  # 先登记长名的

    (_make_mod_dir(lib, "Sigma") / "variant.ini").write_text("distinct", encoding="utf8")  # 短名是长名子串
    added = scan_and_sync(mm)
    assert added == 1, "子串同名的新 Mod 被 LIKE 误判已存在（泄露）"

    names = {m.folder_name for m in mm.get_mods(node_id=char.id)}
    assert names == {"SigmaSwimsuit", "Sigma"}


def test_scan_and_sync_leak_infojson_substring_no_longer_swallowed(mm):
    """Mod 名称与另一项描述中的文字重合，不影响独立路径登记。"""
    char = mm.add_character(name="示例角色丁", node_type="character")
    lib = mm._get_character_library(char.id)
    _make_mod_dir(lib, "FirstMod")
    assert scan_and_sync(mm) == 1
    # 给已存 Mod 的描述写入将要新增的名字
    first = mm.get_mods(node_id=char.id)[0]
    first.info.description = "this blue skin is nice"
    mm.update_mod(first)

    (_make_mod_dir(lib, "blue") / "variant.ini").write_text(
        "distinct", encoding="utf8"
    )  # "blue" 命中 first 的 info_json
    added = scan_and_sync(mm)
    assert added == 1, "名字命中他人 info_json 的新 Mod 被误判已存在（泄露）"
    assert {m.folder_name for m in mm.get_mods(node_id=char.id)} == {"FirstMod", "blue"}


def test_scan_and_sync_leak_same_name_diff_path_both_registered(mm):
    """不同类别下的同名 Mod 分别登记。"""
    a = mm.add_character(name="示例角色丁", node_type="character")
    b = mm.add_character(name="示例角色己", node_type="character")
    _make_mod_dir(mm._get_character_library(a.id), "Default")
    (_make_mod_dir(mm._get_character_library(b.id), "Default") / "variant.ini").write_text("distinct", encoding="utf8")

    added = scan_and_sync(mm)
    assert added == 2, "同名不同路径的第二个 Mod 被 folder_name 判重吞掉（泄露）"

    assert len(mm.get_mods(node_id=a.id)) == 1
    assert len(mm.get_mods(node_id=b.id)) == 1
    # 两条记录指向各自不同的物理路径
    paths = {m.path for m in mm.get_mods()}
    assert len(paths) == 2


def test_scan_and_sync_redundant_subcategory_dir_not_registered(mm):
    """带类别标记的嵌套目录不得被登记为 Mod。"""
    parent = mm.add_character(name="示例游戏甲", node_type="category")
    child = mm.add_character(name="示例角色丁", node_type="character", parent_id=parent.id)
    _make_mod_dir(mm._get_character_library(child.id), "Delta_Skin")

    added = scan_and_sync(mm)
    assert added == 1  # 只有真 Mod 被登记

    mods = mm.get_mods()
    names = {m.folder_name for m in mods}
    assert names == {"Delta_Skin"}
    assert "示例角色丁" not in names and "示例游戏甲" not in names  # 分类目录未被登成假 Mod
    assert mods[0].node_id == child.id


def test_scan_and_sync_deep_nested_mod_discovered(mm):
    """发现流程须覆盖多层类别下的 Mod。"""
    parent = mm.add_character(name="示例游戏甲", node_type="category")
    child = mm.add_character(name="示例角色丁", node_type="character", parent_id=parent.id)
    # child 的物理库位于 示例游戏甲/示例角色丁 —— 相对库根已是第二层，其下的 Mod 在第三层
    _make_mod_dir(mm._get_character_library(child.id), "DeepSkin")

    added = scan_and_sync(mm)
    assert added == 1, "两层以上深度的 Mod 未被发现（泄露）"
    m = mm.get_mods(node_id=child.id)[0]
    assert m.folder_name == "DeepSkin"
