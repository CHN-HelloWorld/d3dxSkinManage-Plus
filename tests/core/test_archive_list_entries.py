"""验证归档成员表读取及内容指纹输入。

无法取得成员摘要时返回 None，调用方需读取实际内容，保持指纹公式一致。
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

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


def test_list_entries_accepts_zero_crc_for_nonempty_member(tmp_path):
    """非空成员的有效 CRC 可以为零，不能据此触发内容解码。"""
    archive = tmp_path / "zero_crc.zip"
    payload = bytes.fromhex("9d0ad96d")
    assert zipfile.crc32(payload) == 0
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("M/tex.dds", payload)

    assert ArchiveService().list_entries(archive) == [("M/tex.dds", len(payload), 0)]


@pytest.mark.parametrize("hash_type, expected", [(0, None), (1, [("M/tex.dds", 4, 0)]), (2, None)])
def test_rar_crc_availability_uses_header_hash_type(tmp_path, monkeypatch, hash_type, expected):
    """随包 RAR 引擎须按校验类型区分有效零 CRC 与未记录 CRC。"""
    import src.services.archive as archive_module

    class Member:
        """提供随包 RAR 引擎的成员元数据协议。"""

        filename = "M\\tex.dds"
        file_size = 4
        CRC = 0
        _header = SimpleNamespace(headerDataEx=SimpleNamespace(HashType=hash_type))

        def is_dir(self):
            """当前样本为普通文件。"""
            return False

    reader = SimpleNamespace(infolist=lambda: [Member()])
    monkeypatch.setattr(archive_module, "RAR_AVAILABLE", True)
    monkeypatch.setattr(ArchiveService, "_rar_open", staticmethod(lambda path: (reader, "cffi")))
    assert ArchiveService().list_entries(tmp_path / "sample.rar") == expected


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
