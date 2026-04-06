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

    def _extract_zip(
        self,
        archive: Path,
        dest: Path,
        progress_cb=None,
        checkpoint=None,
        should_cancel=None,
    ) -> Path | None:
        """解压 ZIP（逐数据块上报/暂停，避免大文件在暂停后继续写盘）。"""
        extract_folder = dest / archive.stem
        if extract_folder.exists():
            extract_folder = self._get_unique_path(extract_folder)

        with zipfile.ZipFile(fs.native_path(archive), "r") as zf:
            infos = zf.infolist()
            top = self._single_top_directory((info.filename, info.is_dir()) for info in infos)
            target = dest if top else extract_folder
            if not top:
                fs.ensure_dir(extract_folder)

            total_bytes = max(1, sum(max(0, info.file_size) for info in infos))
            done_bytes = 0
            target_root = os.path.abspath(str(target))
            self._emit_progress(progress_cb, 0.02)
            for info in infos:
                self._check_extract_continue(checkpoint, should_cancel)
                rel = info.filename.replace("\\", "/")
                outp = target / rel
                try:
                    if os.path.commonpath([target_root, os.path.abspath(str(outp))]) != target_root:
                        logger.warning("跳过越界 ZIP 条目: %s", info.filename)
                        continue
                except ValueError:
                    continue
                if info.is_dir():
                    fs.ensure_dir(outp)
                    continue
                fs.ensure_dir(outp.parent)
                with zf.open(info, "r") as src_fh, open(fs.native_path(outp), "wb") as dst_fh:
                    while True:
                        self._check_extract_continue(checkpoint, should_cancel)
                        chunk = src_fh.read(4 * 1024 * 1024)
                        if not chunk:
                            break
                        dst_fh.write(chunk)
                        done_bytes += len(chunk)
                        self._emit_progress(progress_cb, done_bytes / total_bytes)
            return (dest / top) if top else extract_folder

    def _extract_7z(
        self,
        archive: Path,
        dest: Path,
        progress_cb=None,
        checkpoint=None,
        should_cancel=None,
    ) -> Path | None:
        """解压 7z。

        通过 ExtractCallback 累计文件写出字节数，并映射为 0.05 至 0.95 的进度。
        """
        if not PY7ZR_AVAILABLE:
            logger.error("py7zr 未安装，无法解压 .7z 文件")
            return None

        extract_folder = dest / archive.stem
        if extract_folder.exists():
            extract_folder = self._get_unique_path(extract_folder)

        with py7zr.SevenZipFile(fs.native_path(archive), mode="r") as sz:
            top = self._single_top_directory((info.filename, info.is_directory) for info in sz.list())
            target = dest if top else extract_folder
            if not top:
                fs.ensure_dir(extract_folder)

            self._check_extract_continue(checkpoint, should_cancel)
            self._emit_progress(progress_cb, 0.05)
            cb = self._make_7z_extract_callback(sz, progress_cb, checkpoint, should_cancel)
            if cb is not None:
                sz.extractall(path=fs.native_path(target), callback=cb)
            else:
                sz.extractall(path=fs.native_path(target))
            self._check_extract_continue(checkpoint, should_cancel)
            self._emit_progress(progress_cb, 0.95)
            return (dest / top) if top else extract_folder

    def _make_7z_extract_callback(
        self,
        sz,
        progress_cb,
        checkpoint=None,
        should_cancel=None,
    ):
        """构造按写入字节数报告解压进度的 py7zr 回调。

        不需要进度或取消检查时返回 None；无法确定总大小时只检查取消。
        逐文件累加写入长度，并隔离进度通知异常。
        """
        if progress_cb is None and checkpoint is None and should_cancel is None:
            return None
        from py7zr.callbacks import ExtractCallback

        try:
            total = 0
            for info in sz.list():
                if not getattr(info, "is_directory", False):
                    total += int(getattr(info, "uncompressed", 0) or 0)
        except Exception:
            total = 0
        emit = self._emit_progress
        check = self._check_extract_continue

        class _Cb(ExtractCallback):
            """适配 7z 解压回调，将成员边界上的取消检查和累计进度转发给调用方。"""

            def __init__(self):
                """将已完成成员的累计写入字节数初始化为零。"""
                self._done = 0

            def report_start_preparation(self):
                """在归档准备阶段检查调用方是否允许继续解压。"""
                check(checkpoint, should_cancel)

            def report_start(self, processing_file_path, processing_bytes):
                """开始处理归档成员前检查取消和调用方检查点。"""
                check(checkpoint, should_cancel)

            def report_update(self, decompressed_bytes):
                """处理成员内部进度回调时检查取消，不把未完成成员计入累计进度。"""
                check(checkpoint, should_cancel)

            def report_end(self, processing_file_path, wrote_bytes):
                """累计已完成成员的写入字节数，将解压进度映射至 5%～95%；取消异常继续向上传播。"""
                try:
                    check(checkpoint, should_cancel)
                    self._done += int(wrote_bytes)
                    if total > 0:
                        frac = 0.05 + 0.90 * min(1.0, self._done / total)
                        emit(progress_cb, frac)
                except _ExtractCancelled:
                    raise
                except Exception:
                    pass

            def report_postprocess(self):
                """满足解压回调协议，后处理阶段无需额外操作。"""
                pass

            def report_warning(self, message):
                """接收归档库警告回调，不单独转发警告消息。"""
                pass

        return _Cb()

    def _extract_rar(
        self,
        archive: Path,
        dest: Path,
        progress_cb=None,
        checkpoint=None,
        should_cancel=None,
    ) -> Path | None:
        """解压 RAR

        首选 unrar2-cffi（自带解压引擎，无需外部工具），缺失时回退 rarfile。
        """
        if not RAR_AVAILABLE:
            logger.error(
                "无可用 RAR 解压后端（请安装 unrar2-cffi，或在系统中安装 WinRAR/7-Zip/unrar），无法解压 .rar 文件"
            )
            return None

        extract_folder = dest / archive.stem
        if extract_folder.exists():
            extract_folder = self._get_unique_path(extract_folder)

        rf, kind = self._rar_open(archive)
        if rf is None:
            return None

        # cffi 目录项不带尾斜杠，必须按 infolist 的目录标记区分根文件。
        top = self._single_top_directory((info.filename, self._rar_is_dir(info, kind)) for info in rf.infolist())

        # 粗粒度进度：rar 两后端 extractall 均不逐条回调 → 只报起止（0.05→0.95）。
        self._emit_progress(progress_cb, 0.05)
        if top:
            self._rar_extract_all(rf, kind, dest, progress_cb, checkpoint, should_cancel)
            self._emit_progress(progress_cb, 0.95)
            return dest / top
        fs.ensure_dir(extract_folder)
        self._rar_extract_all(rf, kind, extract_folder, progress_cb, checkpoint, should_cancel)
        self._emit_progress(progress_cb, 0.95)
        return extract_folder

    # RAR 后端适配（统一 unrar.cffi / rarfile 的 API 差异）

    @staticmethod
    def _rar_open(archive: Path):
        """打开 RAR，返回 ``(rf, kind)``。

        kind: ``'cffi'``（unrar2-cffi，首选）/ ``'rarfile'``（系统工具）。两者皆缺
        返回 ``(None, '')``。
        """
        if UNRAR_CFFI_AVAILABLE:
            return _unrar_cffi.RarFile(fs.native_path(archive)), "cffi"
        if RARFILE_AVAILABLE:
            # WinRAR 的安装器通常不修改 PATH；rarfile 默认只找 PATH 中的 unrar。
            # 复用创建 RAR 时的安装探测，同时保留调用方配置的可用工具路径。
            if not shutil.which(rarfile.UNRAR_TOOL):
                rar_exe = find_winrar_rar_exe()
                if rar_exe:
                    unrar_exe = Path(rar_exe).with_name("UnRAR.exe")
                    if unrar_exe.is_file():
                        rarfile.UNRAR_TOOL = str(unrar_exe)
            return rarfile.RarFile(fs.native_path(archive), "r"), "rarfile"
        return None, ""
