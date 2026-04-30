"""验证归档成员表读取及内容指纹输入。

无法取得成员摘要时返回 None，调用方需读取实际内容，保持指纹公式一致。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from src.services.archive import ArchiveService

# 三格式共用的样本内容：含子目录、含空文件（CRC32 天然为 0，不得误判成"缺 CRC"）
_ENTRIES = {
    "M/mod.ini": b"[Constants]\nx=1\n",
    "M/tex.dds": b"\x00\x01\x02\x03" * 10,
    "M/empty.txt": b"",
    "M/sub/more.buf": b"\x04\x05",
}


def _write_body(root: Path) -> Path:
    for name, data in _ENTRIES.items():
        member = root / name
        member.parent.mkdir(parents=True, exist_ok=True)
        member.write_bytes(data)
    return root / "M"


def _make_archive(tmp_path: Path, fmt: str) -> Path:
    """用项目自己的压缩链路产包，保证与真实库内归档同形态（单顶层目录）。"""
    body = _write_body(tmp_path / "source")
    produced = ArchiveService().compress(str(body), str(tmp_path / "out"), fmt)
    if produced is None:
        pytest.skip(f"{fmt} 压缩后端不可用")
    return Path(produced)


@pytest.mark.parametrize("fmt", ["zip", "7z", "rar"])
def test_list_entries_returns_rel_size_crc(tmp_path, fmt):
    archive = _make_archive(tmp_path, fmt)

    entries = ArchiveService().list_entries(archive)

    assert entries is not None, f"{fmt}: 后端可用时不得返回 None"
    by_rel = {rel: (size, crc) for rel, size, crc in entries}
    assert set(by_rel) == set(_ENTRIES), f"{fmt}: 成员集合不符 -> {sorted(by_rel)}"
    for name, data in _ENTRIES.items():
        size, _crc = by_rel[name]
        assert size == len(data), f"{fmt}/{name}: size 必须是解压后字节数"


@pytest.mark.parametrize("fmt", ["zip", "7z", "rar"])
def test_list_entries_crc_matches_uncompressed_bytes(tmp_path, fmt):
    import zlib

    archive = _make_archive(tmp_path, fmt)
    entries = ArchiveService().list_entries(archive)
    assert entries is not None
    for rel, _size, crc in entries:
        assert crc == zlib.crc32(_ENTRIES[rel]) & 0xFFFFFFFF, f"{fmt}/{rel}: CRC32 与解压后字节不符"


@pytest.mark.parametrize("fmt", ["zip", "7z", "rar"])
def test_list_entries_excludes_directory_entries(tmp_path, fmt):
    archive = _make_archive(tmp_path, fmt)
    entries = ArchiveService().list_entries(archive)
    assert entries is not None
    assert all(not rel.endswith("/") for rel, _s, _c in entries)
    # "M/sub" 是目录，必须已被剔除；只有 "M/sub/more.buf" 留下
    assert "M/sub" not in {rel for rel, _s, _c in entries}


def test_list_entries_uses_forward_slashes(tmp_path):
    archive = _make_archive(tmp_path, "rar")
    entries = ArchiveService().list_entries(archive)
    assert entries is not None
    assert all("\\" not in rel for rel, _s, _c in entries)


def test_list_entries_returns_none_when_crc_unavailable(tmp_path):
    """CRC 不可用（如 RAR5 用 BLAKE2sp）→ 整包返回 None，交给昂贵途径。

    构造法：把真 zip 里的 CRC 字段字节抹零（本地头 + 中心目录各一份），
    ``zipfile`` 读 infolist 时便报告 ``CRC == 0`` 而 ``file_size > 0``。
    """
    import struct

    archive = tmp_path / "nocrc.zip"
    payload = b"\x01\x02\x03\x04\x05"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("M/tex.dds", payload)

    raw = archive.read_bytes()
    crc_bytes = struct.pack("<I", zipfile.crc32(payload) & 0xFFFFFFFF)
    assert raw.count(crc_bytes) == 2, "预期本地头与中心目录各含一份 CRC"
    archive.write_bytes(raw.replace(crc_bytes, b"\x00\x00\x00\x00"))

    infos = zipfile.ZipFile(archive).infolist()
    assert infos[0].CRC == 0 and infos[0].file_size == len(payload), "构造未生效"

    assert ArchiveService().list_entries(archive) is None


def test_list_entries_returns_none_when_7z_reports_no_crc(tmp_path, monkeypatch):
    py7zr = pytest.importorskip("py7zr")
    archive = _make_archive(tmp_path, "7z")

    class _FakeInfo:
        def __init__(self, filename):
            self.filename = filename
            self.is_directory = False
            self.uncompressed = 128
            self.crc32 = None

    monkeypatch.setattr(
        py7zr.SevenZipFile,
        "list",
        lambda self: [_FakeInfo("M/tex.dds")],
    )
    assert ArchiveService().list_entries(archive) is None


def test_list_entries_returns_none_without_backend(tmp_path, monkeypatch):
    import src.services.archive as archive_mod

    seven = _make_archive(tmp_path, "7z")
    monkeypatch.setattr(archive_mod, "PY7ZR_AVAILABLE", False)
    assert ArchiveService().list_entries(seven) is None


def test_list_entries_returns_none_for_missing_or_corrupt(tmp_path):
    svc = ArchiveService()
    assert svc.list_entries(tmp_path / "nope.zip") is None
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip at all")
    assert svc.list_entries(broken) is None
    assert svc.list_entries(tmp_path / "body.folder") is None


def test_list_entries_empty_archive_returns_empty_list(tmp_path):
    archive = tmp_path / "empty.zip"
    with zipfile.ZipFile(archive, "w"):
        pass
    assert ArchiveService().list_entries(archive) == []
