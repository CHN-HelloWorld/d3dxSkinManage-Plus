"""验证容器 INI 编辑写入覆盖层、本体指纹不变，以及有效文本优先读取覆盖内容。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from src.core.mod_identity import new_mod_id
from src.core.mod_manager import ModManager
from src.core.mod_package import (
    INI_OVERRIDES_SEEDED_MARKER,
    read_override,
    resolve_effective_ini_text,
    write_container_marker,
)
from src.models.mod import Mod, ModStatus
from src.utils import fs
from tests.helpers import registered_games as registered_games

_INI = b"[KeySwap]\nkey = VK_A\nback = VK_B\n"


@pytest.fixture
def mm(temp_data_dir):
    m = ModManager()
    m.set_game("game_a")
    return m


def _folder_mod(mm: ModManager, char_id: str, name: str) -> Mod:
    """容器 + 文件夹本体：容器/<name>/<name>/merged.ini。返回已登记的 Mod。"""
    lib = mm._get_character_library(char_id)
    container = lib / name
    body = container / name
    body.mkdir(parents=True)
    (body / "merged.ini").write_bytes(_INI)
    mod_id = new_mod_id()
    write_container_marker(
        container, name, Mod.compute_sha1(str(body)), mod_id=mod_id, imported_at="2026-10-05T10:00:00"
    )
    mod = Mod(
        game_id="game_a",
        imported_at="2026-10-05T10:00:00",
        mod_id=mod_id,
        name=name,
        folder_name=name,
        path=str(body),
        content_hash=Mod.compute_sha1(str(body)),
        status=ModStatus.DISABLED,
        node_id=char_id,
    )
    mm._index.add_mod(mod)
    assert mm._write_mod_sidecar(mod)
    return mod


def _archive_mod(mm: ModManager, char_id: str, name: str) -> Mod:
    """容器 + 归档本体：容器/<name>/<name>.zip（单顶层 <name>/merged.ini）。"""
    lib = mm._get_character_library(char_id)
    container = lib / name
    container.mkdir(parents=True)
    body = container / f"{name}.zip"
    with zipfile.ZipFile(body, "w") as zf:
        zf.writestr(f"{name}/merged.ini", _INI.decode())
    mod_id = new_mod_id()
    write_container_marker(
        container, body.name, Mod.compute_sha1(str(body)), mod_id=mod_id, imported_at="2026-10-05T10:00:00"
    )
    mod = Mod(
        game_id="game_a",
        imported_at="2026-10-05T10:00:00",
        mod_id=mod_id,
        name=name,
        folder_name=name,
        path=str(body),
        content_hash=Mod.compute_sha1(str(body)),
        status=ModStatus.DISABLED,
        node_id=char_id,
    )
    mm._index.add_mod(mod)
    assert mm._write_mod_sidecar(mod)
    return mod


def _find_key_binding(bindings):
    for b in bindings:
        if b.section == "KeySwap":
            return b
    return None


# 归档本体中的按键也应可被枚举。


def test_archive_body_key_bindings_not_empty(mm):
    char = mm.add_character(name="示例角色戊", node_type="character")
    mod = _archive_mod(mm, char.id, "ArcMod")
    bindings = mm.parse_mod_key_bindings(mod)
    assert bindings, "归档本体应能列出按键（不再静默消失）"
    b = _find_key_binding(bindings)
    assert b is not None
    assert b.key_value == "VK_A"
    assert b.rel == "merged.ini"


# 改键写 override、归档零改动


def test_edit_archive_key_writes_override_not_body(mm):
    char = mm.add_character(name="示例角色戊", node_type="character")
    mod = _archive_mod(mm, char.id, "ArcEdit")
    body = Path(mod.path)
    hash_before = Mod.compute_sha1(str(body))
    bytes_before = body.read_bytes()

    b = _find_key_binding(mm.parse_mod_key_bindings(mod))
    assert mm.edit_mod_key(mod.mod_id, b.rel, b.key_line_no, "key", "VK_X")

    # 归档本体一个字节不动（hash 稳定，INV-2）
    assert body.read_bytes() == bytes_before
    assert Mod.compute_sha1(str(body)) == hash_before
    # override 已写、有效 ini 反映改动
    ov = read_override(mod.path, "merged.ini")
    assert ov is not None and "VK_X" in ov
    eff = resolve_effective_ini_text(mod.path, "merged.ini")
    assert "VK_X" in eff and "VK_B" in eff  # back 保留
    # 再次解析看到新值
    b2 = _find_key_binding(mm.parse_mod_key_bindings(mod))
    assert b2.key_value == "VK_X"


def test_edit_folder_key_writes_override_body_untouched(mm):
    char = mm.add_character(name="示例角色戊", node_type="character")
    mod = _folder_mod(mm, char.id, "FolderEdit")
    body_ini = Path(mod.path) / "merged.ini"
    bytes_before = body_ini.read_bytes()

    b = _find_key_binding(mm.parse_mod_key_bindings(mod))
    assert mm.edit_mod_key(mod.mod_id, b.rel, b.key_line_no, "key", "VK_Y")

    # 文件夹本体内的原始 ini 不被改（改动落 override，本体是 content_hash 锚点）
    assert body_ini.read_bytes() == bytes_before
    # 有效 ini 用 override 值
    eff = resolve_effective_ini_text(mod.path, "merged.ini")
    assert "VK_Y" in eff


def test_edit_back_field_preserves_key(mm):
    char = mm.add_character(name="示例角色戊", node_type="character")
    mod = _archive_mod(mm, char.id, "BackEdit")
    b = _find_key_binding(mm.parse_mod_key_bindings(mod))
    assert b.back_line_no is not None
    assert mm.edit_mod_key(mod.mod_id, b.rel, b.back_line_no, "back", "VK_Z")
    eff = resolve_effective_ini_text(mod.path, "merged.ini")
    assert "VK_A" in eff  # key 保留
    assert "VK_Z" in eff  # back 改了


# 非容器本体无 override 能力


def test_non_container_body_cannot_edit(mm):
    char = mm.add_character(name="示例角色戊", node_type="character")
    lib = mm._get_character_library(char.id)
    standalone = lib / "Standalone.zip"
    with zipfile.ZipFile(standalone, "w") as zf:
        zf.writestr("Standalone/merged.ini", _INI.decode())
    mod = Mod(
        game_id="game_a",
        imported_at="2026-10-05T10:00:00",
        mod_id="id_standalone",
        name="Standalone",
        folder_name="Standalone",
        path=str(standalone),
        content_hash=Mod.compute_sha1(str(standalone)),
        status=ModStatus.DISABLED,
        node_id=char.id,
    )
    mm._index.add_mod(mod)
    # 独立归档无容器 → 无 override 能力
    assert mm.edit_mod_key(mod.mod_id, "merged.ini", 1, "key", "VK_X") is False


def test_long_native_prefixed_override_path_still_lists_keys(mm, tmp_path):
    container = tmp_path / "library"
    for i in range(8):
        container /= f"segment_{i}_" + "x" * 25
    container /= "示例角色壬"
    body = container / "示例角色壬.7z"
    fs.write_bytes(body, b"placeholder")
    write_container_marker(container, body.name, "0" * 40, imported_at="2026-10-05T10:00:00")
    override_root = container / ".d3dxmeta" / "ini_overrides"
    rel = "regional_iota_outfits/MasterIota.ini"
    fs.write_text(override_root / rel, "[KeySwap]\nkey = p\n")
    fs.write_text(override_root / INI_OVERRIDES_SEEDED_MARKER, "1")
    assert len(str(override_root / rel)) > 260

    mod = Mod(
        game_id="game_a",
        imported_at="2026-10-05T10:00:00",
        mod_id="long_native",
        name="示例角色壬",
        folder_name="示例角色壬",
        path=fs.native_path(body),
        status=ModStatus.DISABLED,
    )
    bindings = mm.parse_mod_key_bindings(mod)

    assert bindings and bindings[0].key_value == "p"
    assert bindings[0].rel == rel
    assert not bindings[0].rel.startswith("\\\\?\\")
