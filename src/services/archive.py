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


class ArchiveService:
    """压缩解压服务

    支持 ZIP、7z、RAR 格式的解压和 ZIP 格式的压缩。
    """

    @staticmethod
    def is_supported(path: str | Path) -> bool:
        """判断文件是否为支持的压缩格式"""
        return Path(path).suffix.lower() in ARCHIVE_EXTENSIONS

    def extract(
        self, archive_path: str | Path, dest_dir: str | Path, progress_cb=None, checkpoint=None, should_cancel=None
    ) -> Path | None:
        """解压归档并返回目标目录，失败时返回 None。

        progress_cb 可接收 0～1 的处理进度，具体精度由归档后端提供；
        进度通知失败不影响解压操作。
        """
        archive = Path(archive_path)
        dest = Path(dest_dir)
        fs.ensure_dir(dest)

        suffix = archive.suffix.lower()

        try:
            if suffix == ".zip":
                return self._extract_zip(archive, dest, progress_cb, checkpoint, should_cancel)
            elif suffix == ".7z":
                return self._extract_7z(archive, dest, progress_cb, checkpoint, should_cancel)
            elif suffix == ".rar":
                return self._extract_rar(archive, dest, progress_cb, checkpoint, should_cancel)
            else:
                logger.error("不支持的压缩格式: %s", suffix)
                return None
        except _ExtractCancelled:
            logger.info("解压已终止: %s", archive.name)
            return None
        except Exception as e:
            logger.error("解压失败 (%s): %s", archive.name, e)
            return None

    @staticmethod
    def _emit_progress(progress_cb, frac: float) -> None:
        """上报进度并隔离回调异常，保持归档操作继续执行。"""
        if progress_cb is None:
            return
        try:
            progress_cb(max(0.0, min(1.0, frac)))
        except Exception:
            pass

    @staticmethod
    def _check_extract_continue(checkpoint=None, should_cancel=None) -> None:
        """检查取消标记与调用方检查点，任一拒绝继续时抛出统一的解压取消异常。"""
        if should_cancel is not None and should_cancel():
            raise _ExtractCancelled()
        if checkpoint is not None and not checkpoint():
            raise _ExtractCancelled()

    @staticmethod
    def _single_top_directory(entries) -> str | None:
        """根据成员类型判断解压根，避免将单个根文件当作目录。"""
        tops: set[str] = set()
        for name, is_directory in entries:
            normalized = name.replace("\\", "/").rstrip("/")
            if not normalized:
                continue
            first, separator, _rest = normalized.partition("/")
            if not first or (not separator and not is_directory):
                return None
            tops.add(first)
        return tops.pop() if len(tops) == 1 else None
