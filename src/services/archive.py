"""压缩解压服务"""

from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

from src.utils import fs
from src.utils.constants import ARCHIVE_EXTENSIONS, META_DIR
from src.utils.logger import logger

# 可选依赖
try:
    import py7zr

    PY7ZR_AVAILABLE = True
except ImportError:
    PY7ZR_AVAILABLE = False

try:
    import rarfile

    RARFILE_AVAILABLE = True
except ImportError:
    RARFILE_AVAILABLE = False

# RAR 优先使用 unrar2-cffi 自带引擎，rarfile 后端则依赖系统外部解压工具。
try:
    from unrar.cffi import rarfile as _unrar_cffi

    UNRAR_CFFI_AVAILABLE = True
except ImportError:
    UNRAR_CFFI_AVAILABLE = False

# 只要任一后端可用即支持 RAR（解压）
RAR_AVAILABLE = UNRAR_CFFI_AVAILABLE or RARFILE_AVAILABLE


# RAR **创建**只能靠专有的 WinRAR 命令行 rar.exe——开源库（rarfile/unrar2-cffi）皆只解压。
# 探测系统是否装了 WinRAR：先查注册表 HKLM\SOFTWARE\WinRAR\exe64，再扫常见安装路径，
# 最后碰运气看 PATH。找不到 → RAR 压缩不可用（设置里下拉禁用 RAR 项）。
_WINRAR_CACHED: str | None | bool = False  # False=未探测；None=探测过但没有；str=路径


def find_winrar_rar_exe() -> str | None:
    """返回可创建 RAR 的命令行 ``rar.exe`` 绝对路径；未安装 WinRAR 返回 None（带缓存）。"""
    global _WINRAR_CACHED
    if _WINRAR_CACHED is not False:
        return _WINRAR_CACHED  # 已探测（命中 None 或路径）

    import os
    import shutil as _sh

    found: str | None = None

    # 1) 注册表（WinRAR 安装时写 HKLM\SOFTWARE\WinRAR，exe64 指向 WinRAR.exe，同目录必有 rar.exe）
    try:
        import winreg  # 仅用于 Windows。

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\WinRAR") as k:
                    exe64, _ = winreg.QueryValueEx(k, "exe64")
                    cand = Path(exe64).parent / "rar.exe"
                    if cand.is_file():
                        found = str(cand)
                        break
            except OSError:
                continue
    except ImportError:
        pass  # 非 Windows

    # 2) 常见安装目录
    if not found:
        for base in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ):
            cand = Path(base) / "WinRAR" / "rar.exe"
            if cand.is_file():
                found = str(cand)
                break

    # 3) PATH 回退处理
    if not found:
        which = _sh.which("rar")
        if which:
            found = which

    _WINRAR_CACHED = found
    return found


def rar_compress_available() -> bool:
    """当前系统能否创建 RAR（即是否装了 WinRAR 命令行）。"""
    return find_winrar_rar_exe() is not None


class _CompressCancelled(Exception):
    """压缩取消的内部控制信号，仅由归档服务使用。"""


class _ExtractCancelled(Exception):
    """内部信号：解压被暂停控制器终止。"""
