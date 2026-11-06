"""使用 Windows 原生接口修复旧临时目录，独立于宿主内部模块和外部脚本。"""

from __future__ import annotations

import ctypes
import os
import stat
from ctypes import wintypes
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from src.plugin_sdk import TaskContext

_DACL_SECURITY_INFORMATION = 0x00000004
_UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
_SE_FILE_OBJECT = 1
_SE_DACL_PROTECTED = 0x1000
_OWNER_RIGHTS_SID = bytes.fromhex("010100000000000304000000")
_PRIVATE_SIDS = frozenset(
    {
        _OWNER_RIGHTS_SID,
        bytes.fromhex("010100000000000512000000"),
        bytes.fromhex("01020000000000052000000020020000"),
    }
)


@dataclass(frozen=True)
class RepairResult:
    """保存检查数量、修复数量及可供用户重试的失败路径。"""

    scanned: int
    repaired: int
    errors: tuple[str, ...]


class _AclHeader(ctypes.Structure):
    """描述 Windows ACL 头部，用于安全读取访问控制项数量。"""

    _fields_ = [
        ("revision", wintypes.BYTE),
        ("reserved", wintypes.BYTE),
        ("size", wintypes.WORD),
        ("ace_count", wintypes.WORD),
        ("reserved2", wintypes.WORD),
    ]


def _native_path(path: Path) -> str:
    """将绝对路径转换为 Windows 长路径形式，兼容网络共享目录。"""
    value = os.path.abspath(path)
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


@lru_cache(maxsize=1)
def _security_api():
    """明确原生函数的指针签名，避免 64 位进程截断安全描述符。"""
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    output_pointer = ctypes.POINTER(pointer)
    security.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        output_pointer,
        output_pointer,
        output_pointer,
        output_pointer,
        output_pointer,
    ]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        pointer,
        pointer,
        pointer,
        pointer,
    ]
    security.SetNamedSecurityInfoW.restype = wintypes.DWORD
    security.GetAce.argtypes = [pointer, wintypes.DWORD, output_pointer]
    security.GetAce.restype = wintypes.BOOL
    security.GetSecurityDescriptorControl.argtypes = [
        pointer,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    security.GetSecurityDescriptorControl.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    return security, kernel


def _is_private_temp_acl(security, dacl: ctypes.c_void_p) -> bool:
    """只匹配旧临时目录的允许项，拒绝自定义 SID、拒绝项或未知 ACL 格式。"""
    if not dacl.value:
        return False
    header = ctypes.cast(dacl, ctypes.POINTER(_AclHeader)).contents
    has_owner_rights = False
    for index in range(header.ace_count):
        entry = ctypes.c_void_p()
        if not security.GetAce(dacl, index, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        raw_header = ctypes.string_at(entry.value, 4)
        ace_size = int.from_bytes(raw_header[2:4], "little")
        if raw_header[0] != 0 or ace_size < 20:
            return False
        sid_header = ctypes.string_at(entry.value + 8, 8)
        sid_size = 8 + 4 * sid_header[1]
        if 8 + sid_size > ace_size:
            return False
        sid = ctypes.string_at(entry.value + 8, sid_size)
        if sid not in _PRIVATE_SIDS:
            return False
        has_owner_rights |= sid == _OWNER_RIGHTS_SID
    return has_owner_rights


def _restore_private_inheritance(directory: Path) -> bool:
    """仅为匹配旧临时 ACL 的目录启用父目录继承，保留已有访问控制项。"""
    security, kernel = _security_api()
    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    native = _native_path(directory)
    result = security.GetNamedSecurityInfoW(
        native,
        _SE_FILE_OBJECT,
        _DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result:
        raise ctypes.WinError(result)
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not security.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not control.value & _SE_DACL_PROTECTED or not _is_private_temp_acl(security, dacl):
            return False
        result = security.SetNamedSecurityInfoW(
            native,
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _UNPROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        if result:
            raise ctypes.WinError(result)
        return True
    finally:
        kernel.LocalFree(descriptor)


def repair_permissions(task: TaskContext, *, app_dir: Path) -> RepairResult:
    """检查应用数据根中的固定目录，不越界、不跟随重解析点，并在目录边界响应取消。"""
    task.raise_if_cancelled()
    if os.name != "nt":
        raise OSError("Windows ACL")
    app_dir = app_dir.resolve()
    pending = [app_dir / name for name in ("config", "data", "cache", "logs", "plugins", "Updater")]
    scanned = repaired = 0
    errors = []
    while pending:
        task.raise_if_cancelled()
        directory = pending.pop()
        try:
            info = os.stat(_native_path(directory), follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                continue
            scanned += 1
            if _restore_private_inheritance(directory):
                repaired += 1
            with os.scandir(_native_path(directory)) as entries:
                for entry in entries:
                    task.raise_if_cancelled()
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(directory / entry.name)
        except FileNotFoundError:
            continue
        except OSError as error:
            errors.append(f"{directory}: {error}")
        if scanned % 32 == 0:
            task.report_progress(0, str(scanned))
    task.report_progress(1, str(scanned))
    return RepairResult(scanned, repaired, tuple(errors))
