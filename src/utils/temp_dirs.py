"""创建可供用户访问的临时目录，保留标准临时目录的生命周期和清理语义。"""

from __future__ import annotations

import os
import tempfile

from src.utils import fs
from src.utils.directory_permissions import restore_directory_inheritance


def mkdtemp(*, suffix: str | None = None, prefix: str | None = None, dir=None) -> str:
    """创建独立临时目录，Windows 立即恢复父目录权限继承。"""
    directory = tempfile.mkdtemp(suffix=suffix, prefix=prefix, dir=dir)
    if os.name == "nt":
        try:
            restore_directory_inheritance(directory)
        except OSError:
            fs.rmtree(directory, ignore_errors=True)
            raise
    return directory


def temporary_directory(
    *, suffix: str | None = None, prefix: str | None = None, dir=None, ignore_cleanup_errors: bool = False
) -> tempfile.TemporaryDirectory[str]:
    """返回标准临时目录对象，使用前恢复继承并保留自动清理和显式 cleanup。"""
    directory = tempfile.TemporaryDirectory(
        suffix=suffix, prefix=prefix, dir=dir, ignore_cleanup_errors=ignore_cleanup_errors
    )
    if os.name == "nt":
        try:
            restore_directory_inheritance(directory.name)
        except OSError:
            directory.cleanup()
            raise
    return directory
