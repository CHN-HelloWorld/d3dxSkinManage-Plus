"""验证归档写盘前统一拒绝越界路径和链接，同时保留普通文件及 RAR 文件复制引用。"""

from __future__ import annotations

import io
import os
import stat
import zipfile
from types import SimpleNamespace

import pytest

import src.services.archive as archive_module
from src.services.archive import ArchiveService

_UNSAFE_NAMES = [
    "../escape.ini",
    "M/../../escape.ini",
    "M/../escape.ini",
    "..\\escape.ini",
    "M\\..\\..\\escape.ini",
    "/escape.ini",
    "\\escape.ini",
    "C:/escape.ini",
    "C:escape.ini",
    "\\\\server\\share\\escape.ini",
    "//server/share/escape.ini",
    "\\\\?\\C:\\escape.ini",
    "M/.. /escape.ini",
    "M/stream:escape.ini",
]
_PAYLOAD = b"[Settings]\nenabled = 1\n"


class _SevenZipReader:
    """仅记录后端提取调用，避免失败回归用例真的写入危险位置。"""

    def __init__(self, members):
        """保存与 py7zr 一致的文件列表及提取调用记录。"""
        self.files = members
        self.calls = []

    def __enter__(self):
        """提供归档读取上下文。"""
        return self

    def __exit__(self, *args):
        """无需关闭实际文件句柄。"""

    def list(self):
        """返回文件类型与名称元数据。"""
        return self.files

    def getnames(self):
        """返回归档成员原始名称。"""
        return [member.filename for member in self.files]

    def extract(self, **kwargs):
        """记录选择性提取，供测试确认预检早于后端写入。"""
        self.calls.append(kwargs)

    def extractall(self, **kwargs):
        """记录整包提取，供测试确认预检早于后端写入。"""
        self.calls.append(kwargs)


class _RarReader:
    """模拟两种 RAR 读取接口，文件引用解析仍使用服务的真实实现。"""

    def __init__(self, members):
        """保存普通文件与引用成员。"""
        self.members = members

    def infolist(self):
        """返回完整成员列表。"""
        return self.members

    def getinfo(self, name):
        """仅允许定位同一归档内存在的成员。"""
        return next(member for member in self.members if member.filename == name)

    def read(self, name):
        """读取固定的安全文件内容。"""
        return _PAYLOAD

    def open(self, member):
        """提供 rarfile 后端使用的流式接口。"""
        return io.BytesIO(_PAYLOAD)


def _member(name, *, directory=False, symlink=False, junction=False, redirection=None, legacy_link=False):
    """构建包含 ZIP 外两种后端链接标记的测试成员。"""
    return SimpleNamespace(
        filename=name,
        file_size=len(_PAYLOAD),
        is_directory=directory,
        is_symlink=symlink,
        is_junction=junction,
        is_dir=lambda: directory,
        isdir=lambda: directory,
        file_redir=redirection,
        create_system=3 if legacy_link else 2,
        _header=SimpleNamespace(
            FileAttr=stat.S_IFLNK if legacy_link else 0,
            headerDataEx=SimpleNamespace(RedirType=redirection[0] if redirection else 0),
        ),
    )


def _archive(tmp_path, monkeypatch, fmt, names, *, link=None):
    """ZIP 使用真实归档，7z 与 RAR 使用无外部写入副作用的元数据后端。"""
    source = tmp_path / f"sample.{fmt}"
    if fmt == "zip":
        with zipfile.ZipFile(source, "w") as writer:
            for name in names:
                member = zipfile.ZipInfo(name)
                if name == link:
                    member.create_system = 3
                    member.external_attr = (stat.S_IFLNK | 0o777) << 16
                writer.writestr(member, _PAYLOAD)
        return source, None
    members = [_member(name, symlink=name == link) for name in names]
    source.write_bytes(b"metadata stub")
    if fmt == "7z":
        reader = _SevenZipReader(members)
        monkeypatch.setattr(archive_module, "PY7ZR_AVAILABLE", True)
        monkeypatch.setattr(archive_module, "open_7z", lambda path: reader)
        monkeypatch.setattr(ArchiveService, "_make_7z_extract_callback", lambda *args: None)
    else:
        reader = _RarReader(members)
        monkeypatch.setattr(archive_module, "RAR_AVAILABLE", True)
        monkeypatch.setattr(ArchiveService, "_rar_open", staticmethod(lambda path: (reader, "rarfile")))
    return source, reader


@pytest.mark.parametrize("fmt", ["zip", "7z", "rar"])
@pytest.mark.parametrize("operation", ["extract", "extract_files"])
@pytest.mark.parametrize("name", _UNSAFE_NAMES)
def test_unsafe_member_rejects_archive_before_any_writes(tmp_path, monkeypatch, fmt, operation, name):
    source, reader = _archive(tmp_path, monkeypatch, fmt, ["M/safe.ini", name])
    destination = tmp_path / "destination"
    service = ArchiveService()
    if operation == "extract":
        assert service.extract(source, destination) is None
    else:
        assert service.extract_files(source, ["M/safe.ini", name], destination) == []
    assert not destination.exists()
    assert not (tmp_path / "escape.ini").exists()
    if fmt == "7z":
        assert reader.calls == []


@pytest.mark.parametrize("fmt", ["zip", "7z", "rar"])
def test_unsafe_requested_name_is_rejected_even_when_not_in_archive(tmp_path, monkeypatch, fmt):
    source, reader = _archive(tmp_path, monkeypatch, fmt, ["M/safe.ini"])
    destination = tmp_path / "destination"
    assert ArchiveService().extract_files(source, ["M/safe.ini", "../missing.ini"], destination) == []
    assert not destination.exists()
    if fmt == "7z":
        assert reader.calls == []


@pytest.mark.parametrize("fmt", ["zip", "7z", "rar"])
@pytest.mark.parametrize("operation", ["extract", "extract_files"])
def test_archive_symbolic_link_rejects_all_writes(tmp_path, monkeypatch, fmt, operation):
    source, reader = _archive(tmp_path, monkeypatch, fmt, ["M/safe.ini", "M/link"], link="M/link")
    destination = tmp_path / "destination"
    service = ArchiveService()
    if operation == "extract":
        assert service.extract(source, destination) is None
    else:
        assert service.extract_files(source, ["M/safe.ini"], destination) == []
    assert not destination.exists()
    if fmt == "7z":
        assert reader.calls == []


def _directory_link(link, target, kind):
    """在 Windows 原生创建连接点，符号链接不可用时明确跳过对应场景。"""
    if kind == "junction":
        if os.name != "nt":
            pytest.skip("目录连接点仅适用于 Windows")
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"无法创建符号链接: {error}")


@pytest.mark.parametrize("fmt", ["zip", "7z", "rar"])
@pytest.mark.parametrize("operation", ["extract", "extract_files"])
@pytest.mark.parametrize("kind", ["symlink", "junction"])
@pytest.mark.parametrize("at_root", [False, True])
def test_existing_destination_link_cannot_escape(tmp_path, monkeypatch, fmt, operation, kind, at_root):
    source, reader = _archive(tmp_path, monkeypatch, fmt, ["M/safe.ini", "M/sub/escape.ini"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escape.ini").write_bytes(b"unchanged")
    destination = tmp_path / "destination"
    if at_root:
        _directory_link(destination, outside, kind)
    else:
        (destination / "M").mkdir(parents=True)
        _directory_link(destination / "M/sub", outside, kind)
    service = ArchiveService()
    if operation == "extract":
        assert service.extract(source, destination) is None
    else:
        assert service.extract_files(source, ["M/safe.ini", "M/sub/escape.ini"], destination) == []
    assert (outside / "escape.ini").read_bytes() == b"unchanged"
    assert not (destination / "M/safe.ini").exists()
    if fmt == "7z":
        assert reader.calls == []


@pytest.mark.parametrize("operation", ["read_file", "read_all_inis", "rewrite", "remove"])
@pytest.mark.parametrize("link_kind", ["traversal", "symlink", "junction"])
def test_7z_temporary_extraction_is_preflighted(tmp_path, monkeypatch, operation, link_kind):
    name = "../escape.ini" if link_kind == "traversal" else "M/link.ini"
    source, reader = _archive(tmp_path, monkeypatch, "7z", ["M/safe.ini", name])
    reader.files[-1].is_symlink = link_kind == "symlink"
    reader.files[-1].is_junction = link_kind == "junction"
    temporary_dirs = []

    def make_temp(**kwargs):
        """强制临时目录位于用例内，避免触及系统临时目录。"""
        directory = tmp_path / kwargs["prefix"]
        directory.mkdir()
        temporary_dirs.append(directory)
        return str(directory)

    monkeypatch.setattr("src.utils.temp_dirs.mkdtemp", make_temp)
    service = ArchiveService()
    before = source.read_bytes() if source.exists() else None
    if operation == "read_file":
        assert service.read_file(source, name) is None
    elif operation == "read_all_inis":
        assert service.read_all_inis(source) == {}
    elif operation == "rewrite":
        assert service._rewrite_7z_with_member(source, "M/safe.ini", b"replacement") is False
    else:
        assert service.remove_members_7z(source, lambda member: member == "M/safe.ini") is False
    assert reader.calls == []
    assert not (tmp_path / "escape.ini").exists()
    assert all(not directory.exists() for directory in temporary_dirs)
    if before is not None:
        assert source.read_bytes() == before


@pytest.mark.parametrize("kind", ["cffi", "rarfile"])
@pytest.mark.parametrize("redirection_type", [1, 2, 3])
def test_rar_link_redirections_are_rejected_before_files_are_created(tmp_path, monkeypatch, kind, redirection_type):
    reader = _RarReader([_member("M/safe.ini"), _member("M/link", redirection=(redirection_type, 0, "outside"))])
    monkeypatch.setattr(archive_module, "RAR_AVAILABLE", True)
    monkeypatch.setattr(ArchiveService, "_rar_open", staticmethod(lambda path: (reader, kind)))
    destination = tmp_path / "destination"
    assert ArchiveService().extract(tmp_path / "sample.rar", destination) is None
    assert not destination.exists()


def test_legacy_cffi_rar_symbolic_link_is_rejected(tmp_path, monkeypatch):
    reader = _RarReader([_member("M/safe.ini"), _member("M/link", legacy_link=True)])
    monkeypatch.setattr(archive_module, "RAR_AVAILABLE", True)
    monkeypatch.setattr(ArchiveService, "_rar_open", staticmethod(lambda path: (reader, "cffi")))
    destination = tmp_path / "destination"
    assert ArchiveService().extract(tmp_path / "sample.rar", destination) is None
    assert not destination.exists()


@pytest.mark.parametrize("redirection_type", [4, 5])
def test_safe_same_archive_rar_file_references_still_extract(tmp_path, monkeypatch, redirection_type):
    reader = _RarReader(
        [_member("M/source.ini"), _member("M/copy.ini", redirection=(redirection_type, 0, "M/source.ini"))]
    )
    monkeypatch.setattr(archive_module, "RAR_AVAILABLE", True)
    monkeypatch.setattr(ArchiveService, "_rar_open", staticmethod(lambda path: (reader, "rarfile")))
    destination = tmp_path / "destination"
    service = ArchiveService()
    root = service.extract(tmp_path / "sample.rar", destination)
    assert root == destination / "M"
    assert (root / "copy.ini").read_bytes() == _PAYLOAD
    selected = service.extract_files(tmp_path / "sample.rar", ["M/copy.ini"], tmp_path / "selected")
    assert len(selected) == 1
    assert selected[0].read_bytes() == _PAYLOAD


@pytest.mark.parametrize("fmt", ["zip", "7z"])
@pytest.mark.parametrize("operation", ["extract", "extract_files", "read_file", "read_all_inis"])
def test_safe_nested_archives_keep_existing_behavior(tmp_path, monkeypatch, fmt, operation):
    source = tmp_path / f"safe.{fmt}"
    if fmt == "zip":
        with zipfile.ZipFile(source, "w") as writer:
            writer.writestr("M/sub/safe.ini", _PAYLOAD)
    else:
        py7zr = pytest.importorskip("py7zr")
        with py7zr.SevenZipFile(source, "w") as writer:
            writer.writestr(_PAYLOAD, "M/sub/safe.ini")

    def make_temp(**kwargs):
        """让真实 7z 后端的临时提取保持在测试目录内。"""
        directory = tmp_path / kwargs["prefix"]
        directory.mkdir()
        return str(directory)

    monkeypatch.setattr("src.utils.temp_dirs.mkdtemp", make_temp)
    service = ArchiveService()
    if operation == "extract":
        root = service.extract(source, tmp_path / "destination")
        assert root == tmp_path / "destination/M"
        assert (root / "sub/safe.ini").read_bytes() == _PAYLOAD
    elif operation == "extract_files":
        selected = service.extract_files(source, ["M/sub/safe.ini"], tmp_path / "selected")
        assert len(selected) == 1
        assert selected[0].read_bytes() == _PAYLOAD
    elif operation == "read_file":
        assert service.read_file(source, "M/sub/safe.ini") == _PAYLOAD
    else:
        assert service.read_all_inis(source) == {"M/sub/safe.ini": _PAYLOAD}


@pytest.mark.parametrize("name", ["..", "C:", "\\\\server\\share", *_UNSAFE_NAMES])
def test_top_directory_inference_rejects_unsafe_names(name):
    with pytest.raises(ValueError):
        ArchiveService._single_top_directory([(name, True)])


@pytest.mark.parametrize("operation", ["extract", "extract_files"])
def test_7z_duplicate_generated_name_cannot_follow_existing_junction(tmp_path, monkeypatch, operation):
    source, reader = _archive(tmp_path, monkeypatch, "7z", ["M/sub", "M/sub", "M/sub/safe.ini"])
    for member in reader.files[:2]:
        member.is_directory = True
    destination = tmp_path / "destination"
    (destination / "M").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _directory_link(destination / "M/sub_0", outside, "junction")
    service = ArchiveService()
    if operation == "extract":
        assert service.extract(source, destination) is None
    else:
        assert service.extract_files(source, ["M/sub", "M/sub/safe.ini"], destination) == []
    assert reader.calls == []


@pytest.mark.parametrize("kind", ["cffi", "rarfile"])
def test_rar_directory_redirection_is_rejected(tmp_path, monkeypatch, kind):
    reader = _RarReader([_member("M/safe.ini"), _member("M/link", directory=True, redirection=(3, 0, "outside"))])
    monkeypatch.setattr(archive_module, "RAR_AVAILABLE", True)
    monkeypatch.setattr(ArchiveService, "_rar_open", staticmethod(lambda path: (reader, kind)))
    destination = tmp_path / "destination"
    assert ArchiveService().extract(tmp_path / "sample.rar", destination) is None
    assert not destination.exists()


@pytest.mark.parametrize("target", ["../source.ini", "M/../source.ini", "C:source.ini", "missing.ini"])
def test_rar_copy_reference_target_must_be_safe_and_in_same_archive(tmp_path, monkeypatch, target):
    reader = _RarReader([_member("M/safe.ini"), _member("M/copy.ini", redirection=(5, 0, target))])
    monkeypatch.setattr(archive_module, "RAR_AVAILABLE", True)
    monkeypatch.setattr(ArchiveService, "_rar_open", staticmethod(lambda path: (reader, "rarfile")))
    destination = tmp_path / "destination"
    assert ArchiveService().extract(tmp_path / "sample.rar", destination) is None
    assert not destination.exists()


@pytest.mark.parametrize("fmt", ["zip", "7z", "rar"])
def test_selected_member_with_null_byte_is_rejected_before_writes(tmp_path, monkeypatch, fmt):
    source, reader = _archive(tmp_path, monkeypatch, fmt, ["M/safe.ini"])
    destination = tmp_path / "destination"
    assert ArchiveService().extract_files(source, ["M/safe.ini", "M/bad\x00.ini"], destination) == []
    assert not destination.exists()
    if fmt == "7z":
        assert reader.calls == []


@pytest.mark.parametrize("operation", ["inject_7z", "rewrite_7z", "inject_rar"])
def test_injected_member_path_is_validated_before_writes(tmp_path, monkeypatch, operation):
    source, reader = _archive(tmp_path, monkeypatch, "7z", ["M/safe.ini"])
    existing_scratch = source.with_name(source.stem + "_inject_tmp.7z")
    existing_scratch.write_bytes(b"unchanged")
    monkeypatch.setattr(archive_module, "find_winrar_rar_exe", lambda: "rar.exe")

    def refuse_temp(**kwargs):
        """危险注入名称必须在创建临时目录或启动外部工具前被拒绝。"""
        pytest.fail("不安全路径触发了临时目录创建")

    monkeypatch.setattr("src.utils.temp_dirs.mkdtemp", refuse_temp)
    service = ArchiveService()
    if operation == "inject_7z":
        assert service.inject_member_7z(source, "../escape.ini", _PAYLOAD, allow_rewrite=True) is False
    elif operation == "rewrite_7z":
        assert service._rewrite_7z_with_member(source, "../escape.ini", _PAYLOAD) is False
    else:
        assert service.inject_member_rar(source.with_suffix(".rar"), "../escape.ini", _PAYLOAD) is False
    assert reader.calls == []
    assert existing_scratch.read_bytes() == b"unchanged"
