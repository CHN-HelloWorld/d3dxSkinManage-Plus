"""恢复或清理原生更新器生成的待删除更新器文件。"""

from __future__ import annotations

import os
import re
import stat
import struct
from pathlib import Path

from src.utils.constants import UPDATER_EXE_NAME, UPDATER_SUBDIR
from src.utils.logger import logger

_RETIRED = re.compile(r"updater\.exe\.old_update\.([0-9]+)\.([0-9]+)\.([0-9]+)", re.IGNORECASE)


def _reparse(info: os.stat_result) -> bool:
    """识别符号链接或 Windows 重解析点，供更新器清理拒绝跳转路径。"""
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _directory(install_dir: Path) -> Path | None:
    """仅接受祖先及更新器目录均为普通目录的安装路径，缺失或访问失败时返回 None。"""
    directory = Path(os.path.abspath(install_dir)) / UPDATER_SUBDIR
    try:
        for parent in (*reversed(directory.parents), directory):
            info = parent.lstat()
            if _reparse(info) or not stat.S_ISDIR(info.st_mode):
                return None
        return directory
    except OSError:
        return None


def _regular_file(path: Path) -> bool:
    """通过不跟随链接的状态查询确认普通文件，排除重解析点，查询错误向上传递。"""
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and not _reparse(info)


def _native_image(path: Path) -> bool:
    """校验普通文件是否具有完整的 x64 PE 可执行结构及有文件数据支撑的入口，不验证发布者身份。"""
    if not _regular_file(path):
        return False
    with path.open("rb") as stream:
        file_size = path.stat().st_size
        dos = stream.read(64)
        if len(dos) != 64 or dos[:2] != b"MZ":
            return False
        offset = struct.unpack_from("<I", dos, 60)[0]
        if offset < 64 or offset > file_size - 24:
            return False
        stream.seek(offset)
        header = stream.read(24)
        if len(header) != 24 or header[:4] != b"PE\0\0":
            return False
        machine, sections = struct.unpack_from("<HH", header, 4)
        optional_size, flags = struct.unpack_from("<HH", header, 20)
        table_end = offset + 24 + optional_size + sections * 40
        if (
            machine != 0x8664
            or not 1 <= sections <= 96
            or not flags & 2
            or flags & 0x2000
            or optional_size < 240
            or table_end > file_size
        ):
            return False
        optional = stream.read(optional_size)
        if len(optional) != optional_size:
            return False
        if struct.unpack_from("<H", optional)[0] != 0x20B:
            return False
        entry = struct.unpack_from("<I", optional, 16)[0]
        image_size, headers_size = struct.unpack_from("<II", optional, 56)
        subsystem = struct.unpack_from("<H", optional, 68)[0]
        if not 0 < entry < image_size or not table_end <= headers_size <= file_size or subsystem not in (2, 3):
            return False
        if struct.unpack_from("<I", optional, 108)[0] > 4:
            certificate, certificate_size = struct.unpack_from("<II", optional, 144)
            if certificate_size and (certificate < headers_size or certificate + certificate_size > file_size):
                return False
        entry_is_backed = False
        for _ in range(sections):
            section = stream.read(40)
            if len(section) != 40:
                return False
            virtual_size, address, raw_size, raw_offset = struct.unpack_from("<IIII", section, 8)
            characteristics = struct.unpack_from("<I", section, 36)[0]
            if raw_size and (raw_offset < headers_size or raw_offset + raw_size > file_size):
                return False
            if address + max(virtual_size, raw_size) > image_size:
                return False
            if characteristics & 0x20000000 and address <= entry < address + raw_size:
                entry_is_backed = True
        return entry_is_backed


def _retired(directory: Path) -> list[tuple[tuple[int, int, int], Path]]:
    """按退役文件名提取时间、序号和进程 ID，返回从新到旧的候选路径，不在此处删除文件。"""
    result = []
    for path in directory.iterdir():
        match = _RETIRED.fullmatch(path.name)
        if match:
            pid, timestamp, serial = match.groups()
            result.append(((int(timestamp), int(serial), int(pid)), path))
    return sorted(result, reverse=True)


def cleanup_retired_updaters(install_dir: Path) -> bool:
    """存在由更新器管理且需稍后重试清理的文件时返回 True。"""
    directory = _directory(install_dir)
    if directory is None:
        return False
    try:
        retired = _retired(directory)
        if not retired:
            return False
        if not _native_image(directory / UPDATER_EXE_NAME):
            return True
    except OSError:
        return True
    pending = False
    for _order, path in retired:
        try:
            if not _native_image(path):
                continue
            if not path.stat().st_mode & stat.S_IWRITE:
                path.chmod(stat.S_IWRITE)
            path.unlink()
            logger.info("已清理旧更新器: %s", path.name)
        except FileNotFoundError:
            pass
        except OSError:
            pending = True
            logger.debug("旧更新器暂时无法清理，保留待重试: %s", path.name)
    return pending


def find_or_restore_updater(install_dir: Path) -> Path | None:
    """保持固定启动路径，仅在该路径缺失时恢复待删除的更新器。"""
    directory = _directory(install_dir)
    if directory is None:
        return None
    current = directory / UPDATER_EXE_NAME
    try:
        if os.path.lexists(current):
            return current if _regular_file(current) else None
        retired = _retired(directory)
    except OSError:
        return None
    for _order, path in retired:
        try:
            if not _native_image(path):
                continue
            # Windows 重命名不会覆盖其他进程同时恢复的文件。
            path.rename(current)
            logger.warning("固定更新器路径缺失，已恢复旧副本: %s", path.name)
            return current
        except FileExistsError:
            return current if _regular_file(current) else None
        except OSError:
            logger.debug("旧更新器暂时无法恢复: %s", path.name)
    return None
