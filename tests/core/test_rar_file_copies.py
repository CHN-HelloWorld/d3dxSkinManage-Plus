"""验证 RAR 文件复制引用使用真实目标内容，而不是空字节或占位校验值。"""

from __future__ import annotations

import base64
import zlib

import pytest

from src.models.mod import Mod
from src.services.archive import ArchiveService

_COPY_RAR = (
    "UmFyIRoHAQAJ78hvCwEFBwQGAQGAgIAAg9sNGyQCAwu5AASACiDbh8phgB0ACGNvcHkuaW5pCgMC"
    "q/SjNdVt3QHDrzYzACL6M9zijB4F4ByeMY4BEjGQgMQ//oHS7Xqrnepxd2iSaYyG1cyUep6ORI0R"
    "iNU0NhqH9bAzF7SXMAIDGIAABIAKIAAAAADAGAAHbW9kLmluaQoDAqv0ozXVbd0BDAUFAAhjb3B5"
    "LmluaR13VlEDBQQA"
)
_PAYLOAD = b"[Constants]\nglobal persist $enabled = 1\n" * 32


@pytest.fixture
def copy_rar(tmp_path, monkeypatch):
    """生成带文件复制引用的 RAR5 样本，禁用外部工具以验证随包引擎。"""
    monkeypatch.setenv("PATH", "")
    source = tmp_path / "copies.rar"
    source.write_bytes(base64.b64decode(_COPY_RAR))
    return source


def test_copy_member_metadata_uses_target_size_and_crc(copy_rar, tmp_path):
    service = ArchiveService()
    crc = zlib.crc32(_PAYLOAD)
    entries = service.list_entries(copy_rar)
    assert entries == [("copy.ini", len(_PAYLOAD), crc), ("mod.ini", len(_PAYLOAD), crc)]

    folder = tmp_path / "body"
    folder.mkdir()
    for name in ("copy.ini", "mod.ini"):
        (folder / name).write_bytes(_PAYLOAD)
    assert Mod.compute_sha1(copy_rar) == Mod.compute_sha1(folder)


@pytest.mark.parametrize("operation", ["read_file", "extract_files", "read_all_inis", "extract"])
def test_copy_member_reads_actual_target_bytes(copy_rar, tmp_path, operation):
    service = ArchiveService()
    if operation == "read_file":
        assert service.read_file(copy_rar, "mod.ini") == _PAYLOAD
    elif operation == "read_all_inis":
        assert service.read_all_inis(copy_rar) == {"copy.ini": _PAYLOAD, "mod.ini": _PAYLOAD}
    elif operation == "extract_files":
        extracted = service.extract_files(copy_rar, ["mod.ini"], tmp_path / "selected")
        assert len(extracted) == 1
        assert extracted[0].read_bytes() == _PAYLOAD
    else:
        progress = []
        root = service.extract(copy_rar, tmp_path / "all", progress_cb=progress.append)
        assert root is not None
        assert (root / "mod.ini").read_bytes() == _PAYLOAD
        assert (root / "copy.ini").read_bytes() == _PAYLOAD
        assert progress and progress[-1] == pytest.approx(0.95)
