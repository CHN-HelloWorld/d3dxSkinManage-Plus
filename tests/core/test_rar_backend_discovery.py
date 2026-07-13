from __future__ import annotations

import base64
from pathlib import Path

import pytest

from src.services import archive

# 内嵌由 WinRAR 创建的 RAR5 小样本，在无法创建 RAR 的系统上验证随包引擎。
_COMPRESSED_RAR = (
    "UmFyIRoHAQDz4YLrCwEFBwAGAQGAgIAAC8Ab4SMCAwu5AASACiDbh8phgAUAB21vZC5pbmkK"
    "AwLJpH4Fo2LdAcOvNjMAIvoz3OKMHgXgHJ4xjgESMZCAxD/+gdLtequd6nF3aJJpjIbVzJR6"
    "no5EjRGI1TQ2Gof1sB13VlEDBQQA"
)
_EXPECTED_INI = b"[Constants]\nglobal persist $enabled = 1\n" * 32


@pytest.fixture
def compressed_rar(tmp_path):
    source = tmp_path / "sample.rar"
    source.write_bytes(base64.b64decode(_COMPRESSED_RAR))
    return source


def test_rar_extract_with_bundled_engine_without_external_tools(
    compressed_rar,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(archive, "RARFILE_AVAILABLE", False)
    monkeypatch.setattr(archive, "find_winrar_rar_exe", lambda: None)

    result = archive.ArchiveService().extract(compressed_rar, tmp_path / "output")

    assert result is not None, "Source installation is missing the bundled RAR engine"
    assert (result / "mod.ini").read_bytes() == _EXPECTED_INI


def test_rarfile_finds_installed_winrar_outside_path(
    compressed_rar,
    tmp_path,
    monkeypatch,
):
    rarfile = pytest.importorskip("rarfile")
    rar_exe = archive.find_winrar_rar_exe()
    if not rar_exe or not Path(rar_exe).with_name("UnRAR.exe").is_file():
        pytest.skip("This integration case needs an installed WinRAR")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(archive, "UNRAR_CFFI_AVAILABLE", False)
    monkeypatch.setattr(archive, "_WINRAR_CACHED", False)
    monkeypatch.setattr(rarfile, "UNRAR_TOOL", "unrar")
    monkeypatch.setattr(rarfile, "CURRENT_SETUP", None)

    result = archive.ArchiveService().extract(compressed_rar, tmp_path / "output")

    assert result is not None, "Installed WinRAR must be usable without changing PATH"
    assert (result / "mod.ini").read_bytes() == _EXPECTED_INI


def test_rarfile_keeps_an_explicit_working_tool(
    compressed_rar,
    tmp_path,
    monkeypatch,
):
    rarfile = pytest.importorskip("rarfile")
    rar_exe = archive.find_winrar_rar_exe()
    if not rar_exe or not Path(rar_exe).with_name("UnRAR.exe").is_file():
        pytest.skip("This integration case needs an installed WinRAR")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(archive, "UNRAR_CFFI_AVAILABLE", False)
    monkeypatch.setattr(archive, "find_winrar_rar_exe", lambda: None)
    monkeypatch.setattr(rarfile, "UNRAR_TOOL", str(Path(rar_exe).with_name("UnRAR.exe")))
    monkeypatch.setattr(rarfile, "CURRENT_SETUP", None)

    result = archive.ArchiveService().extract(compressed_rar, tmp_path / "output")

    assert result is not None
    assert (result / "mod.ini").read_bytes() == _EXPECTED_INI
