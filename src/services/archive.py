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

    @staticmethod
    def _rar_is_dir(info, kind: str) -> bool:
        """目录项判定：两后端都用方法，但名字不同（cffi: ``is_dir`` / rarfile: ``isdir``）。"""
        return info.is_dir() if kind == "cffi" else info.isdir()

    @staticmethod
    def _rar_file_names(rf, kind: str) -> list[str]:
        """RAR 内文件清单（排除目录项）。cffi 的目录项不带尾斜杠，故统一走 infolist 过滤。"""
        return [it.filename for it in rf.infolist() if not ArchiveService._rar_is_dir(it, kind)]

    def _rar_extract_all(
        self,
        rf,
        kind: str,
        target: Path,
        progress_cb=None,
        checkpoint=None,
        should_cancel=None,
    ) -> None:
        """将 RAR 内容解压到目标目录。

        rarfile 使用整体解压接口，unrar.cffi 逐成员读取并写入；成员路径须通过目录范围校验。
        """
        target_root = target.resolve()
        infos = list(rf.infolist())
        files = [it for it in infos if not self._rar_is_dir(it, kind)]
        total = max(1, len(files))
        done = 0
        for it in infos:
            self._check_extract_continue(checkpoint, should_cancel)
            rel = it.filename.replace("\\", "/")
            outp = target / rel
            # 防目录穿越：解析后必须仍落在 target 内
            try:
                outp.resolve().relative_to(target_root)
            except ValueError:
                logger.warning("跳过越界 RAR 条目: %s", it.filename)
                continue
            if self._rar_is_dir(it, kind):
                fs.ensure_dir(outp)
                continue
            fs.ensure_dir(outp.parent)
            with open(fs.native_path(outp), "wb") as out_fh:
                if kind == "rarfile":
                    with rf.open(it) as src_fh:
                        while True:
                            self._check_extract_continue(checkpoint, should_cancel)
                            chunk = src_fh.read(4 * 1024 * 1024)
                            if not chunk:
                                break
                            out_fh.write(chunk)
                else:
                    # unrar.cffi 当前只提供整成员 read；至少在成员边界严格暂停。
                    out_fh.write(rf.read(it.filename))
            done += 1
            self._emit_progress(progress_cb, 0.05 + 0.90 * done / total)

    @staticmethod
    def _iter_files_with_sizes(source: Path, checkpoint=None, should_cancel=None):
        """收集 source 下所有文件 + 各自大小 + 总字节。返回 (files, sizes, total)。"""
        files: list[Path] = []
        sizes: list[int] = []
        total = 0
        for fp in sorted(fs.iter_files(source)):
            if should_cancel is not None and should_cancel():
                raise _CompressCancelled()
            if checkpoint is not None and not checkpoint():
                raise _CompressCancelled()
            if fs.is_file(fp):
                try:
                    sz = os.path.getsize(fs.native_path(fp))
                except OSError:
                    sz = 0
                files.append(fp)
                sizes.append(sz)
                total += sz
        return files, sizes, total

    def compress_to_zip(
        self, source_dir: str | Path, output_path: str | Path, progress_cb=None, should_cancel=None, checkpoint=None
    ) -> bool:
        """压缩为包含同名顶层目录的 ZIP。

        顶层目录避免手动解压时将成员散落到目标目录；内容指纹会剥离单一顶层，
        不影响不同存储形式的内容比较。progress_cb 按已写入字节比例报告进度；
        每个文件写入前检查 should_cancel，取消时删除未完成归档。
        """
        source = Path(source_dir)
        output = Path(output_path)

        if not fs.is_dir(source):
            logger.error("源文件夹不存在: %s", source)
            return False

        try:
            fs.ensure_dir(output.parent)
            files, sizes, total = self._iter_files_with_sizes(source, checkpoint, should_cancel)
            done = 0
            with zipfile.ZipFile(fs.native_path(output), "w", zipfile.ZIP_DEFLATED) as zf:
                for file_path, fsize in zip(files, sizes):
                    if should_cancel and should_cancel():
                        logger.info("压缩被取消(zip): %s", source.name)
                        raise _CompressCancelled()
                    if checkpoint is not None and not checkpoint():
                        raise _CompressCancelled()
                    arcname = file_path.relative_to(source.parent).as_posix()
                    info = zipfile.ZipInfo.from_file(fs.native_path(file_path), arcname)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with (
                        open(fs.native_path(file_path), "rb") as src_fh,
                        zf.open(info, "w", force_zip64=True) as dst_fh,
                    ):
                        while True:
                            if should_cancel and should_cancel():
                                raise _CompressCancelled()
                            if checkpoint is not None and not checkpoint():
                                raise _CompressCancelled()
                            chunk = src_fh.read(4 * 1024 * 1024)
                            if not chunk:
                                break
                            dst_fh.write(chunk)
                            done += len(chunk)
                            if progress_cb and total > 0:
                                try:
                                    progress_cb(done / total)
                                except Exception:
                                    pass

            logger.info("压缩完成: %s -> %s", source.name, output.name)
            return True
        except _CompressCancelled:
            try:
                fs.unlink(output, missing_ok=True)
            except OSError:
                pass
            return False
        except Exception as e:
            logger.error("压缩失败: %s", e)
            return False

    def compress_to_7z(
        self, source_dir: str | Path, output_path: str | Path, progress_cb=None, should_cancel=None, checkpoint=None
    ) -> bool:
        """使用 py7zr 压缩为含同名顶层目录的 7z。

        每个文件写入后按累计字节比例报告进度；单个大文件写入期间无法继续细分。
        """
        if not PY7ZR_AVAILABLE:
            logger.error("py7zr 未安装，无法创建 .7z")
            return False
        source = Path(source_dir)
        output = Path(output_path)
        if not fs.is_dir(source):
            logger.error("源文件夹不存在: %s", source)
            return False
        try:
            fs.ensure_dir(output.parent)
            # 使用较低的 LZMA2 压缩等级，减少已压缩贴图和缓冲数据的重复压缩开销。
            filters = None
            try:
                filters = [{"id": py7zr.FILTER_LZMA2, "preset": 1}]
            except AttributeError:
                filters = None
            files, sizes, total = self._iter_files_with_sizes(source, checkpoint, should_cancel)
            done = 0
            with py7zr.SevenZipFile(fs.native_path(output), "w", filters=filters) as sz:
                for file_path, fsize in zip(files, sizes):
                    if should_cancel and should_cancel():
                        logger.info("压缩被取消(7z): %s", source.name)
                        raise _CompressCancelled()
                    if checkpoint is not None and not checkpoint():
                        raise _CompressCancelled()
                    arcname = file_path.relative_to(source.parent)
                    sz.write(fs.native_path(file_path), str(arcname).replace("\\", "/"))
                    done += fsize
                    if progress_cb and total > 0:
                        try:
                            progress_cb(done / total)
                        except Exception:
                            pass
            logger.info("压缩完成(7z): %s -> %s", source.name, output.name)
            return True
        except _CompressCancelled:
            try:
                fs.unlink(output, missing_ok=True)
            except OSError:
                pass
            return False
        except Exception as e:
            logger.error("7z 压缩失败: %s", e)
            # 半成品清理，避免残留损坏包被后续误认成 mod
            try:
                fs.unlink(output, missing_ok=True)
            except OSError:
                pass
            return False

    def compress_to_rar(
        self, source_dir: str | Path, output_path: str | Path, progress_cb=None, should_cancel=None, checkpoint=None
    ) -> bool:
        """调用 WinRAR 的 rar.exe 压缩为含顶层目录的 RAR，不可用时返回 False。

        通过 -idn 隐藏文件名并保留百分比，从标准输出解析实时进度；
        进度解析失败不影响压缩结果。
        """
        rar_exe = find_winrar_rar_exe()
        if not rar_exe:
            logger.error("未检测到 WinRAR（rar.exe），无法创建 .rar")
            return False
        source = Path(source_dir)
        output = Path(output_path)
        if not source.exists() or not source.is_dir():
            logger.error("源文件夹不存在: %s", source)
            return False
        try:
            import re
            import subprocess

            output.parent.mkdir(parents=True, exist_ok=True)
            output.unlink(missing_ok=True)  # 压缩前移除同名归档，以满足 RAR 工具的写入要求。
            creationflags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationflags = subprocess.CREATE_NO_WINDOW  # 不弹黑框
            # 使用 -idn 隐藏文件名并保留进度。按二进制读取输出，
            # 以回车或换行分块解析百分比，避免系统编码横幅造成解码失败。
            proc = subprocess.Popen(
                [rar_exe, "a", "-ep1", "-r", "-o+", "-idn", str(output), source.name],
                cwd=str(source.parent),
                creationflags=creationflags,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            pct_re = re.compile(rb"(\d{1,3})%")
            last = -1
            try:
                # 后台读取标准输出，主循环以队列超时轮询取消状态，
                # 避免压缩器长时间无输出时阻塞取消请求。
                import os as _os
                import queue as _queue
                import threading as _threading

                _CANCEL_POLL_S = 0.05
                chunks: _queue.Queue = _queue.Queue(maxsize=64)
                fd = proc.stdout.fileno() if proc.stdout else None

                def _pump() -> None:
                    """从 RAR 子进程输出管道读取进度并投递到有界队列，结束时发送空块哨兵。"""
                    try:
                        while True:
                            try:
                                block = _os.read(fd, 4096)
                            except OSError:
                                block = b""
                            if not block:
                                break
                            try:
                                chunks.put(block, timeout=1.0)
                            except _queue.Full:
                                pass  # 队列已满时跳过本次进度数据，继续排空子进程输出。
                    finally:
                        chunks.put(b"")  # 结束哨兵

                pump = None
                if fd is not None:
                    pump = _threading.Thread(target=_pump, name="rar-progress-pump", daemon=True)
                    pump.start()

                while fd is not None:
                    # 取消时终止 RAR 子进程。
                    if should_cancel and should_cancel():
                        logger.info("压缩被取消(rar)，终止 rar.exe: %s", source.name)
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        try:
                            output.unlink(missing_ok=True)
                        except OSError:
                            pass
                        return False
                    if checkpoint is not None and not checkpoint():
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        return False
                    try:
                        chunk = chunks.get(timeout=_CANCEL_POLL_S)
                    except _queue.Empty:
                        # 无输出时继续轮询仍在执行的进程，进程结束后处理结果。
                        if proc.poll() is not None and chunks.empty():
                            break
                        continue
                    if not chunk:
                        break
                    for m in pct_re.finditer(chunk):
                        pct = min(100, int(m.group(1)))
                        if pct > last and progress_cb:  # 单调推进，忽略回跳/重复
                            last = pct
                            try:
                                progress_cb(pct / 100.0)
                            except Exception:
                                pass
            except Exception:
                pass
            proc.wait(timeout=600)
            if proc.returncode != 0 or not output.exists():
                logger.error("RAR 压缩失败 (rc=%s)", proc.returncode)
                try:
                    output.unlink(missing_ok=True)
                except OSError:
                    pass
                return False
            logger.info("压缩完成(rar): %s -> %s", source.name, output.name)
            return True
        except Exception as e:
            logger.error("RAR 压缩异常: %s", e)
            try:
                output.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def compress(
        self,
        source_dir: str | Path,
        output_base: str | Path,
        fmt: str = "zip",
        progress_cb=None,
        should_cancel=None,
        checkpoint=None,
    ) -> Path | None:
        """按指定格式压缩文件夹，返回实际产物路径，失败返回 None。

        output_base 不含扩展名，方法按 zip、7z 或 rar 添加后缀。RAR 工具不可用时改用 ZIP。
        所有格式均包含同名顶层目录，progress_cb 和 should_cancel 透传至压缩器。
        取消时中止压缩并清理未完成文件。
        """
        fmt = (fmt or "zip").lower()
        base = Path(output_base)
        if fmt == "7z" and PY7ZR_AVAILABLE:
            out = base.with_suffix(".7z")
            return out if self.compress_to_7z(source_dir, out, progress_cb, should_cancel, checkpoint) else None
        if fmt == "rar" and rar_compress_available():
            out = base.with_suffix(".rar")
            if self.compress_to_rar(source_dir, out, progress_cb, should_cancel, checkpoint):
                return out
            # 取消时不回退 zip（否则又跑一遍压缩，白等）
            if should_cancel and should_cancel():
                return None
            # rar 失败 → 回退 zip
            logger.warning("RAR 压缩失败，回退 ZIP: %s", base.name)
        elif fmt == "rar":
            logger.warning("未安装 WinRAR，RAR 存储回退 ZIP: %s", base.name)
        elif fmt == "7z":
            logger.warning("py7zr 不可用，7z 存储回退 ZIP: %s", base.name)
        # 默认 / 回退：zip
        out = base.with_suffix(".zip")
        return out if self.compress_to_zip(source_dir, out, progress_cb, should_cancel, checkpoint) else None

    # 向归档追加 .d3dxmeta 元数据成员。

    def inject_member_7z(
        self, archive_path: str | Path, arcname: str, data: bytes, allow_rewrite: bool = False
    ) -> bool:
        """向 7z 追加成员，已有同名成员时按 allow_rewrite 决定是否重写。

        新增成员作为独立块追加，无需重新压缩原有数据；同名覆盖会重建归档。
        默认禁止重写并返回 False，调用方须明确处理该结果。
        """
        if not PY7ZR_AVAILABLE:
            return False
        archive = Path(archive_path)
        arcname = arcname.replace("\\", "/")
        try:
            # 探测是否已存在同名成员，决定走「追加快路径」还是回退外置 sidecar
            with py7zr.SevenZipFile(str(archive), mode="r") as sz:
                existing = set(sz.getnames())
            if arcname in existing:
                if not allow_rewrite:
                    # 不在主线程做多秒级整包重压；返回 False → 上层写外置 sidecar 回退处理。
                    return False
                return self._rewrite_7z_with_member(archive, arcname, data)
            # 快路径：追加模式，仅写新块，不重压既有 99MB 内容
            with py7zr.SevenZipFile(str(archive), mode="a") as sz:
                sz.writestr(data, arcname)
            return True
        except Exception as e:
            # 追加失败也不做慢速重压（除非显式允许）——回退外置 sidecar。
            logger.warning("7z 追加成员失败: %s", e)
            if allow_rewrite:
                return self._rewrite_7z_with_member(archive, arcname, data)
            return False

    def _rewrite_7z_with_member(self, archive: Path, arcname: str, data: bytes) -> bool:
        """慢路径：解全包→写/覆盖成员→整包重压。仅用于覆盖已存在成员（追加会重复）。"""
        if not PY7ZR_AVAILABLE:
            return False
        import tempfile

        tmpdir = Path(tempfile.mkdtemp(prefix="7z_inject_"))
        try:
            with py7zr.SevenZipFile(str(archive), mode="r") as sz:
                sz.extractall(path=str(tmpdir))
            target = tmpdir / arcname
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            tmp_out = archive.with_name(archive.stem + "_inject_tmp.7z")
            tmp_out.unlink(missing_ok=True)
            with py7zr.SevenZipFile(str(tmp_out), mode="w") as sz:
                for fp in sorted(tmpdir.rglob("*")):
                    if fp.is_file():
                        sz.write(str(fp), str(fp.relative_to(tmpdir)).replace("\\", "/"))
            archive.unlink(missing_ok=True)
            tmp_out.replace(archive)
            return True
        except Exception as e:
            logger.error("7z 注入成员失败 (%s): %s", arcname, e)
            try:
                (archive.with_name(archive.stem + "_inject_tmp.7z")).unlink(missing_ok=True)
            except OSError:
                pass
            return False
        finally:
            shutil.rmtree(str(tmpdir), ignore_errors=True)

    def remove_members_7z(self, archive_path: str | Path, pred) -> bool:
        """从 .7z 移除满足 ``pred(name)`` 的成员（整包重压实现）。无匹配则免重压直接成功。"""
        if not PY7ZR_AVAILABLE:
            return False
        archive = Path(archive_path)
        import tempfile

        tmpdir = Path(tempfile.mkdtemp(prefix="7z_strip_"))
        try:
            with py7zr.SevenZipFile(str(archive), mode="r") as sz:
                names = [n for n in sz.getnames()]
            if not any(pred(n) for n in names):
                return True
            with py7zr.SevenZipFile(str(archive), mode="r") as sz:
                sz.extractall(path=str(tmpdir))
            tmp_out = archive.with_name(archive.stem + "_strip_tmp.7z")
            tmp_out.unlink(missing_ok=True)
            with py7zr.SevenZipFile(str(tmp_out), mode="w") as sz:
                for fp in sorted(tmpdir.rglob("*")):
                    if not fp.is_file():
                        continue
                    rel = str(fp.relative_to(tmpdir)).replace("\\", "/")
                    if pred(rel):
                        continue
                    sz.write(str(fp), rel)
            archive.unlink(missing_ok=True)
            tmp_out.replace(archive)
            return True
        except Exception as e:
            logger.error("7z 移除成员失败: %s", e)
            return False
        finally:
            shutil.rmtree(str(tmpdir), ignore_errors=True)

    def inject_member_rar(self, archive_path: str | Path, arcname: str, data: bytes) -> bool:
        """向已存在的 .rar 追加/覆盖成员（WinRAR ``rar a`` + ``-ap<内部路径前缀>``）。

        WinRAR 支持向已有归档就地追加成员，无需整包重建。把待写内容落到临时暂存目录，
        用 ``-ap{top}`` 指定归档内路径前缀，使成员进到 ``{top}/.d3dxmeta/info.json``。
        """
        rar_exe = find_winrar_rar_exe()
        if not rar_exe:
            return False
        archive = Path(archive_path)
        arcname = arcname.replace("\\", "/")
        # 拆出「归档内目录前缀」与「文件名」：rar 用 -ap 指定前缀、参数只给要加的本地文件
        if "/" in arcname:
            prefix, fname = arcname.rsplit("/", 1)
        else:
            prefix, fname = "", arcname
        import subprocess
        import tempfile

        tmpdir = Path(tempfile.mkdtemp(prefix="rar_inject_"))
        try:
            # 先剔除旧的同路径成员（覆盖语义），再加新的
            self.remove_members_rar(archive, lambda n: n.replace("\\", "/") == arcname)
            local = tmpdir / fname
            local.write_bytes(data)
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            cmd = [rar_exe, "a", "-o+", "-idq", "-ep"]
            if prefix:
                cmd.append(f"-ap{prefix}")
            cmd += [str(archive), fname]
            proc = subprocess.run(cmd, cwd=str(tmpdir), creationflags=creationflags, capture_output=True, timeout=300)
            if proc.returncode != 0:
                logger.error(
                    "RAR 注入成员失败 (rc=%s): %s",
                    proc.returncode,
                    (proc.stderr or b"").decode("utf-8", "ignore")[:200],
                )
                return False
            return True
        except Exception as e:
            logger.error("RAR 注入成员异常: %s", e)
            return False
        finally:
            shutil.rmtree(str(tmpdir), ignore_errors=True)

    def remove_members_rar(self, archive_path: str | Path, pred) -> bool:
        """从 .rar 删除满足 ``pred(name)`` 的成员（WinRAR ``rar d``）。无匹配则直接成功。"""
        rar_exe = find_winrar_rar_exe()
        if not rar_exe:
            return False
        archive = Path(archive_path)
        if not archive.exists():
            return True
        try:
            targets = [n for n in self.list_contents(archive) if pred(n.replace("\\", "/"))]
            if not targets:
                return True
            import subprocess

            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            # rar d 匹配成员名须用归档内的原生路径分隔符（WinRAR 在 Windows 下用反斜杠）：
            # 传正斜杠会匹配不到、返回 rc=10（no files deleted）。故用 list_contents 返回的原样。
            cmd = [rar_exe, "d", "-idq", str(archive)] + list(targets)
            proc = subprocess.run(cmd, creationflags=creationflags, capture_output=True, timeout=300)
            return proc.returncode == 0
        except Exception as e:
            logger.error("RAR 删除成员异常: %s", e)
            return False

    # 内容读取

    def list_contents(self, archive_path: str | Path) -> list[str]:
        """列出压缩包中的文件（不含目录条目）

        参数：
            archive_path: 压缩包路径

        返回：
            文件路径列表（内部相对路径）
        """
        archive = Path(archive_path)
        suffix = archive.suffix.lower()

        try:
            if suffix == ".zip":
                with zipfile.ZipFile(fs.native_path(archive), "r") as zf:
                    return [n for n in zf.namelist() if not n.endswith("/")]
            elif suffix == ".7z":
                if not PY7ZR_AVAILABLE:
                    logger.error("py7zr 未安装，无法读取 .7z 文件")
                    return []
                with py7zr.SevenZipFile(fs.native_path(archive), mode="r") as sz:
                    return [info.filename for info in sz.list() if not info.is_directory]
            elif suffix == ".rar":
                if not RAR_AVAILABLE:
                    logger.error("无可用 RAR 解压后端，无法读取 .rar 文件")
                    return []
                rf, kind = self._rar_open(archive)
                if rf is None:
                    return []
                return self._rar_file_names(rf, kind)
            else:
                logger.error("不支持的压缩格式: %s", suffix)
                return []
        except Exception as e:
            logger.error("列出压缩包内容失败 (%s): %s", archive.name, e)
            return []

    def list_entries(self, archive_path: str | Path) -> list[tuple[str, int, int]] | None:
        """只读归档成员表，返回路径、解压后大小及 CRC-32，不解压文件。

        目录成员被排除，路径统一为正斜杠并保留归档顶层。空归档返回空列表；
        无法读取或任一有效成员缺少可用 CRC 时返回 None。
        调用方须通过实际内容计算相同字段，保持内容指纹公式一致。
        """
        archive = Path(archive_path)
        suffix = archive.suffix.lower()

        def _accept(rel: str, size, crc) -> tuple[str, int, int] | None:
            """统一归一 + CRC 可用性判定。返回 None 代表该成员的 CRC 不可用。"""
            if crc is None:
                return None
            size = int(size or 0)
            crc = int(crc) & 0xFFFFFFFF
            # CRC32 为 0 且有内容 → 后端没记 CRC（空文件的 CRC32 天然是 0，是合法值）
            if crc == 0 and size > 0:
                return None
            return rel.replace("\\", "/"), size, crc

        try:
            if suffix == ".zip":
                with zipfile.ZipFile(fs.native_path(archive), "r") as zf:
                    infos = [i for i in zf.infolist() if not i.is_dir()]
                    raw = [(i.filename, i.file_size, i.CRC) for i in infos]
            elif suffix == ".7z":
                if not PY7ZR_AVAILABLE:
                    return None
                with py7zr.SevenZipFile(fs.native_path(archive), mode="r") as sz:
                    infos = [i for i in sz.list() if not i.is_directory]
                    raw = [(i.filename, i.uncompressed, i.crc32) for i in infos]
            elif suffix == ".rar":
                if not RAR_AVAILABLE:
                    return None
                rf, kind = self._rar_open(archive)
                if rf is None:
                    return None
                raw = [
                    (i.filename, i.file_size, getattr(i, "CRC", None))
                    for i in rf.infolist()
                    if not self._rar_is_dir(i, kind)
                ]
            else:
                return None
        except Exception as e:
            logger.debug("读取归档成员表失败，将回退逐字节 (%s): %s", archive.name, e)
            return None

        entries: list[tuple[str, int, int]] = []
        for name, size, crc in raw:
            accepted = _accept(name, size, crc)
            if accepted is None:
                # 任一成员缺少可用 CRC 时读取完整归档内容，不能忽略该成员后生成身份。
                logger.debug("归档成员缺可用 CRC，整包回退逐字节: %s -> %s", archive.name, name)
                return None
            entries.append(accepted)
        return entries
