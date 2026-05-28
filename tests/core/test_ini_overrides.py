"""验证容器覆盖文件路径及有效 INI 的读取优先级。"""

from __future__ import annotations

import zipfile
from pathlib import Path

from src.core.mod_package import (
    body_ini_rels,
    iter_effective_inis,
    list_overrides,
    override_path_for,
    read_body_ini_text,
    read_override,
    resolve_effective_ini_text,
    resolve_ini_overrides_dir,
    write_container_marker,
    write_override,
)

_INI = "[KeySwap]\nkey = VK_A\nback = VK_B\n"
_INI_EDITED = "[KeySwap]\nkey = VK_X\nback = VK_B\n"


def _folder_container(root: Path, name: str) -> Path:
    """造一个 文件夹本体容器：容器/<name>/ 本体 + .d3dxmod marker。返回本体路径。"""
    container = root / name
    body = container / name
    body.mkdir(parents=True)
    # 二进制写：保证文件内容严格为 LF（write_text 在 Windows 会把 \n 翻成 \r\n，
    # 而 read_body_ini_text 刻意逐字节 round-trip，翻译后会与 LF 常量比对失配）。
    (body / "merged.ini").write_bytes(_INI.encode("utf-8"))
    (body / "sub").mkdir()
    (body / "sub" / "extra.ini").write_bytes(_INI.encode("utf-8"))
    write_container_marker(container, name, "0" * 40, imported_at="2026-10-05T10:00:00")
    return body


def _archive_container(root: Path, name: str) -> Path:
    """造一个 归档本体容器：容器/<name>.zip（单顶层 <name>/）+ .d3dxmod。返回本体路径。"""
    container = root / name
    container.mkdir(parents=True)
    body = container / f"{name}.zip"
    with zipfile.ZipFile(body, "w") as zf:
        zf.writestr(f"{name}/merged.ini", _INI)
        zf.writestr(f"{name}/sub/extra.ini", _INI)
    write_container_marker(container, body.name, "0" * 40, imported_at="2026-10-05T10:00:00")
    return body


# override 目录解析


def test_overrides_dir_in_container(tmp_path):
    body = _folder_container(tmp_path, "FolderMod")
    d = resolve_ini_overrides_dir(str(body))
    assert d == body.parent / ".d3dxmeta" / "ini_overrides"


def test_overrides_dir_none_for_standalone(tmp_path):
    # 独立归档（不在容器内）→ 无 override 能力
    z = tmp_path / "Standalone.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Standalone/merged.ini", _INI)
    assert resolve_ini_overrides_dir(str(z)) is None
    assert override_path_for(str(z), "merged.ini") is None
    assert write_override(str(z), "merged.ini", _INI_EDITED) is False


# override 读写往复


def test_write_read_override_roundtrip(tmp_path):
    body = _folder_container(tmp_path, "M")
    assert write_override(str(body), "merged.ini", _INI_EDITED)
    op = override_path_for(str(body), "merged.ini")
    assert op.is_file()
    assert op == body.parent / ".d3dxmeta" / "ini_overrides" / "merged.ini"
    assert read_override(str(body), "merged.ini") == _INI_EDITED
    assert list_overrides(str(body)) == ["merged.ini"]


def test_override_subpath_mirrors_body_layout(tmp_path):
    body = _folder_container(tmp_path, "M")
    assert write_override(str(body), "sub/extra.ini", _INI_EDITED)
    op = override_path_for(str(body), "sub/extra.ini")
    assert op == body.parent / ".d3dxmeta" / "ini_overrides" / "sub" / "extra.ini"
    assert "sub/extra.ini" in list_overrides(str(body))


def test_temporary_configuration_writes_are_not_effective_inis(tmp_path):
    body = _folder_container(tmp_path, "M")
    assert write_override(body, "merged.ini", _INI_EDITED)
    directory = resolve_ini_overrides_dir(body)
    (directory / "merged.ini.synctmp").write_text(_INI_EDITED, encoding="utf-8")
    (directory / "merged.ini.partial.tmp").write_text(_INI_EDITED, encoding="utf-8")
    assert list_overrides(body) == ["merged.ini"]


def test_norm_rel_blocks_traversal(tmp_path):
    body = _folder_container(tmp_path, "M")
    # 目录穿越应被规范化剥除，绝不写到容器外
    op = override_path_for(str(body), "../../evil.ini")
    assert op is not None
    assert "evil.ini" in op.name
    ov_dir = resolve_ini_overrides_dir(str(body))
    assert ov_dir in op.parents  # 仍落在 overrides 目录内


# 有效 ini 优先级


def test_effective_ini_prefers_override_folder(tmp_path):
    body = _folder_container(tmp_path, "M")
    assert resolve_effective_ini_text(str(body), "merged.ini") == _INI  # 无 override → 本体
    write_override(str(body), "merged.ini", _INI_EDITED)
    assert resolve_effective_ini_text(str(body), "merged.ini") == _INI_EDITED  # override 优先


def test_effective_ini_prefers_override_archive(tmp_path):
    body = _archive_container(tmp_path, "M")
    assert resolve_effective_ini_text(str(body), "merged.ini") == _INI
    write_override(str(body), "merged.ini", _INI_EDITED)
    assert resolve_effective_ini_text(str(body), "merged.ini") == _INI_EDITED


# 本体 ini 枚举（folder 与 archive 剥壳一致）


def test_body_ini_rels_folder(tmp_path):
    body = _folder_container(tmp_path, "M")
    assert set(body_ini_rels(str(body))) == {"merged.ini", "sub/extra.ini"}


def test_body_ini_rels_archive_strips_single_top(tmp_path):
    body = _archive_container(tmp_path, "M")
    # 归档单顶层 M/ 被剥掉 → 与 folder 本体一致的 rel
    assert set(body_ini_rels(str(body))) == {"merged.ini", "sub/extra.ini"}


def test_read_body_ini_archive_member(tmp_path):
    body = _archive_container(tmp_path, "M")
    assert read_body_ini_text(str(body), "merged.ini") == _INI
    assert read_body_ini_text(str(body), "sub/extra.ini") == _INI
    assert read_body_ini_text(str(body), "nonexist.ini") is None


# iter_effective_inis：本体全集 ∪ 仅 override 存在的


def test_iter_effective_inis_union(tmp_path):
    body = _folder_container(tmp_path, "M")
    write_override(str(body), "merged.ini", _INI_EDITED)  # 覆盖已有本体 ini
    out = dict(iter_effective_inis(str(body)))
    assert out["merged.ini"] == _INI_EDITED  # override 优先
    assert out["sub/extra.ini"] == _INI  # 未改的取本体
    assert set(out) == {"merged.ini", "sub/extra.ini"}
