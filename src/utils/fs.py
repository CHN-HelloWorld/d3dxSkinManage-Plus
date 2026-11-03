"""跨平台文件系统辅助：Windows 长路径 + 可协作暂停的复制。

应用把 Mod 放在可任意嵌套的类别树下，普通 Win32 路径一旦超过 ``MAX_PATH``，
``pathlib``/``shutil`` 的部分调用会报 ``WinError 206``。本模块只在真正访问文件系统时
给绝对路径加 ``\\\\?\\`` 前缀；数据库和界面仍保存/显示普通路径。
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

from src.utils.language_pack import N_

Checkpoint = Callable[[], bool]
ProgressCallback = Callable[[float], None]


def stat_identity_time_ns(info: os.stat_result) -> int:
    """返回路径查询和句柄查询可共同比较的身份时间戳。

    Windows 的 ctime 在不同查询接口中可能表示创建或变更时间，优先使用创建时间。
    其他平台使用元数据变更时间。
    """
    if os.name == "nt":
        return getattr(info, "st_birthtime_ns", info.st_ctime_ns)
    return info.st_ctime_ns


class CopyCancelled(OSError):
    """协作式暂停/终止检查要求停止复制。"""


class RecycleBinError(OSError):
    """操作系统无法将受管路径移入回收站。"""

    def __init__(
        self,
        path: str | os.PathLike[str],
        message: str = N_("系统无法将该路径放入回收站"),
        *,
        path_too_long: bool = False,
    ) -> None:
        """保存回收失败的规范路径和超长标记，并翻译错误说明。"""
        from src.utils.i18n import t

        super().__init__(t(message))
        self.path = normal_path(path)
        self.path_too_long = path_too_long


def native_path(path: str | os.PathLike[str]) -> str:
    """返回适合底层文件 API 的路径；Windows 使用扩展长度绝对路径。"""
    raw = os.fspath(path)
    if os.name != "nt" or raw.startswith("\\\\?\\"):
        return raw
    absolute = os.path.abspath(raw)
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def normal_path(path: str | os.PathLike[str]) -> str:
    """把扩展长度路径还原为适合存库/展示的普通路径。"""
    raw = os.fspath(path)
    if raw.startswith("\\\\?\\UNC\\"):
        return "\\\\" + raw[8:]
    if raw.startswith("\\\\?\\"):
        return raw[4:]
    return raw


def _windows_path_units(path: str | os.PathLike[str]) -> int:
    """返回普通绝对路径占用的 UTF-16 代码单元数。"""
    absolute = os.path.abspath(normal_path(path))
    return len(absolute.encode("utf-16-le")) // 2


def is_recycle_bin_path_too_long(path: str | os.PathLike[str]) -> bool:
    """判断路径树是否超出 Windows 回收站接口支持的长度。

    普通文件操作使用长路径前缀，回收站接口则可能拒绝超长子路径。
    这里只扫描名称，使界面能在撤回已启用 Mod 前确认是否永久删除。"""
    if os.name != "nt":
        return False
    target = Path(normal_path(path))
    if _windows_path_units(target) >= 260:
        return True
    if not is_dir(target):
        return False
    try:
        for root, dirs, files in walk(target):
            if _windows_path_units(root) >= 260:
                return True
            for name in dirs:
                if _windows_path_units(root / name) >= 260:
                    return True
            for name in files:
                if _windows_path_units(root / name) >= 260:
                    return True
    except OSError:
        # 非路径长度错误由实际回收站操作报告。
        return False
    return False


def ensure_recycle_bin_compatible(path: str | os.PathLike[str]) -> None:
    """在删除前检查路径长度及系统回收站支持，失败时抛出 RecycleBinError。"""
    if is_recycle_bin_path_too_long(path):
        raise RecycleBinError(
            path,
            N_("路径或其内部文件超过 Windows 回收站支持的长度"),
            path_too_long=True,
        )
    try:
        from PySide6.QtCore import QFile
    except Exception as exc:
        raise RecycleBinError(path, N_("当前系统不支持回收站操作")) from exc
    if not QFile.supportsMoveToTrash():
        raise RecycleBinError(path, N_("当前系统或文件位置不支持回收站操作"))


def move_to_trash(path: str | os.PathLike[str]) -> str:
    """将文件或目录移入系统回收站，平台支持时返回回收站路径。

    失败时抛出 RecycleBinError，由界面确认是否改用永久删除。"""
    target = os.path.abspath(normal_path(path))
    if not exists(target):
        return ""
    ensure_recycle_bin_compatible(target)
    try:
        from PySide6.QtCore import QFile

        ok, trashed_path = QFile.moveToTrash(target)
    except Exception as exc:
        raise RecycleBinError(target, str(exc) or N_("系统回收站操作失败")) from exc
    if not ok:
        raise RecycleBinError(target)
    return normal_path(trashed_path or "")


def exists(path: str | os.PathLike[str]) -> bool:
    """通过原生路径查询目标是否存在，支持 Windows 长路径。"""
    return os.path.exists(native_path(path))


def lexists(path: str | os.PathLike[str]) -> bool:
    """通过原生路径检查目录项是否存在，包含目标缺失的链接。"""
    return os.path.lexists(native_path(path))


def is_symlink(path: str | os.PathLike[str]) -> bool:
    """通过原生路径判断目录项是否为符号链接。"""
    return os.path.islink(native_path(path))


def is_file(path: str | os.PathLike[str]) -> bool:
    """通过原生路径判断目标是否为普通文件。"""
    return os.path.isfile(native_path(path))


def is_dir(path: str | os.PathLike[str]) -> bool:
    """通过原生路径判断目标是否为目录。"""
    return os.path.isdir(native_path(path))


def ensure_dir(path: str | os.PathLike[str], *, exist_ok: bool = True) -> Path:
    """递归创建目录并返回无长路径前缀的 Path；exist_ok 控制已存在时是否报错。"""
    os.makedirs(native_path(path), exist_ok=exist_ok)
    return Path(normal_path(path))


def read_bytes(path: str | os.PathLike[str]) -> bytes:
    """通过原生路径读取文件的全部字节，读取失败时传递系统异常。"""
    with open(native_path(path), "rb") as fh:
        return fh.read()


def write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    """确保父目录存在后覆盖写入字节；不提供原子替换保证。"""
    target = Path(normal_path(path))
    ensure_dir(target.parent)
    with open(native_path(target), "wb") as fh:
        fh.write(data)


def read_text(path: str | os.PathLike[str], *, encoding: str = "utf-8") -> str:
    """通过原生路径按指定编码读取全部文本。"""
    with open(native_path(path), encoding=encoding) as fh:
        return fh.read()


def write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    encoding: str = "utf-8",
) -> None:
    """确保父目录存在后按指定编码覆盖文本；不提供原子替换保证。"""
    target = Path(normal_path(path))
    ensure_dir(target.parent)
    with open(native_path(target), "w", encoding=encoding) as fh:
        fh.write(text)


def unlink(path: str | os.PathLike[str], *, missing_ok: bool = False) -> None:
    """删除文件；仅在 missing_ok 为真时忽略目标不存在。"""
    try:
        os.unlink(native_path(path))
    except FileNotFoundError:
        if not missing_ok:
            raise


def rmdir(path: str | os.PathLike[str]) -> None:
    """删除空目录，非空或不可访问时传递系统异常。"""
    os.rmdir(native_path(path))


def rmtree(path: str | os.PathLike[str], *, ignore_errors: bool = False, onerror=None) -> None:
    """递归删除目录树，按调用方指定策略处理错误。"""
    shutil.rmtree(native_path(path), ignore_errors=ignore_errors, onerror=onerror)


def list_children(path: str | os.PathLike[str]) -> list[Path]:
    """列出目录的直接子项，返回不含原生长路径前缀的 Path。"""
    base = Path(normal_path(path))
    with os.scandir(native_path(path)) as entries:
        return [base / entry.name for entry in entries]


def walk(path: str | os.PathLike[str]) -> Iterator[tuple[Path, list[str], list[str]]]:
    """与 ``os.walk`` 相同，但产出的根路径去掉 ``\\?\\`` 前缀。"""
    for root, dirs, files in os.walk(native_path(path)):
        yield Path(normal_path(root)), dirs, files


def iter_files(path: str | os.PathLike[str]) -> Iterator[Path]:
    """递归枚举目录树中的文件路径，沿用 walk 的目录遍历规则。"""
    for root, _dirs, files in walk(path):
        for name in files:
            yield root / name


def is_empty_dir(path: str | os.PathLike[str]) -> bool:
    """判断目标是否为存在的空目录；非目录返回 False。"""
    if not is_dir(path):
        return False
    with os.scandir(native_path(path)) as entries:
        return next(entries, None) is None


def _check_continue(checkpoint: Checkpoint | None, should_cancel) -> None:
    """合并暂停检查点和取消回调，停止请求统一转换为 CopyCancelled。"""
    if should_cancel is not None and should_cancel():
        raise CopyCancelled("copy cancelled")
    if checkpoint is not None and not checkpoint():
        raise CopyCancelled("copy cancelled")


def _path_size(
    path: str | os.PathLike[str],
    checkpoint: Checkpoint | None,
    should_cancel=None,
) -> int:
    """累计文件或目录树的可读文件大小，遍历期间检查取消状态。"""
    _check_continue(checkpoint, should_cancel)
    if is_file(path):
        return max(0, os.path.getsize(native_path(path)))
    total = 0
    for root, _dirs, files in walk(path):
        _check_continue(checkpoint, should_cancel)
        for name in files:
            try:
                total += max(0, os.path.getsize(native_path(root / name)))
            except OSError:
                pass
    return total


def copy_path(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    *,
    checkpoint: Checkpoint | None = None,
    should_cancel=None,
    progress_cb: ProgressCallback | None = None,
    buffer_size: int = 4 * 1024 * 1024,
) -> Path:
    """复制文件/目录，支持长路径，并在每个数据块之间协作暂停/终止。"""
    source = Path(normal_path(src))
    target = Path(normal_path(dst))
    total = max(1, _path_size(source, checkpoint, should_cancel))
    copied = 0

    def emit() -> None:
        """将累计复制字节换算为限制在零到一之间的进度并通知调用方。"""
        if progress_cb is not None:
            progress_cb(max(0.0, min(1.0, copied / total)))

    def copy_file(source_file, target_file):
        """分块复制单个文件并持续检查取消，完成后尽力保留源文件属性。"""
        nonlocal copied
        _check_continue(checkpoint, should_cancel)
        target_parent = Path(normal_path(target_file)).parent
        ensure_dir(target_parent)
        with open(native_path(source_file), "rb") as src_fh, open(native_path(target_file), "wb") as dst_fh:
            while True:
                _check_continue(checkpoint, should_cancel)
                chunk = src_fh.read(buffer_size)
                if not chunk:
                    break
                dst_fh.write(chunk)
                copied += len(chunk)
                emit()
        try:
            shutil.copystat(native_path(source_file), native_path(target_file))
        except OSError:
            pass
        return target_file

    emit()
    if is_dir(source):
        shutil.copytree(
            native_path(source),
            native_path(target),
            copy_function=copy_file,
        )
    else:
        ensure_dir(target.parent)
        copy_file(native_path(source), native_path(target))
    copied = total
    emit()
    return target
