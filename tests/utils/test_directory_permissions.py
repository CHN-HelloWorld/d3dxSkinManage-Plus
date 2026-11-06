"""验证 Windows 临时目录权限及旧缓存迁移，不修改测试目录之外的 ACL。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.utils.directory_permissions import restore_directory_inheritance
from src.utils.temp_dirs import mkdtemp, temporary_directory

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ACL 专用回归")


def test_temporary_directory_keeps_parent_access_and_cleanup(tmp_path):
    restore_directory_inheritance(tmp_path.parent)
    restore_directory_inheritance(tmp_path)
    with temporary_directory(dir=tmp_path) as value:
        directory = Path(value)
        assert directory.is_dir()
        assert not restore_directory_inheritance(directory, only_private=True)
    assert not directory.exists()


def test_mkdtemp_keeps_inheritance_after_rename(tmp_path):
    restore_directory_inheritance(tmp_path.parent)
    restore_directory_inheritance(tmp_path)
    directory = Path(mkdtemp(dir=tmp_path))
    target = tmp_path / "published"
    directory.rename(target)
    assert not restore_directory_inheritance(target, only_private=True)
