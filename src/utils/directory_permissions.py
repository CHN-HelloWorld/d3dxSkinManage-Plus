"""恢复 Python 私有临时目录的 Windows 权限继承，不扩大父目录的访问范围。"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from functools import lru_cache

from src.utils import fs

_DACL_SECURITY_INFORMATION = 0x00000004
_UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
_SE_FILE_OBJECT = 1
_SE_DACL_PROTECTED = 0x1000
_PRIVATE_SIDS = frozenset(
    {
        bytes.fromhex("010100000000000304000000"),
        bytes.fromhex("010100000000000512000000"),
        bytes.fromhex("01020000000000052000000020020000"),
    }
)
_OWNER_RIGHTS_SID = bytes.fromhex("010100000000000304000000")


class _AclHeader(ctypes.Structure):
    """描述 Windows ACL 头部，用于读取访问控制项数量。"""

    _fields_ = [
        ("revision", wintypes.BYTE),
        ("reserved", wintypes.BYTE),
        ("size", wintypes.WORD),
        ("ace_count", wintypes.WORD),
        ("reserved2", wintypes.WORD),
    ]


@lru_cache(maxsize=1)
def _security_api():
    """配置安全描述符接口的指针类型，避免在 64 位进程中截断地址。"""
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
    """仅识别临时目录的所有者、系统和管理员允许项，保留自定义或拒绝权限。"""
    if not dacl.value:
        return False
    header = ctypes.cast(dacl, ctypes.POINTER(_AclHeader)).contents
    has_owner_rights = False
    for index in range(header.ace_count):
        entry = ctypes.c_void_p()
        if not security.GetAce(dacl, index, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        raw_header = ctypes.string_at(entry.value, 4)
        if raw_header[0] != 0 or int.from_bytes(raw_header[2:4], "little") < 20:
            return False
        sid_header = ctypes.string_at(entry.value + 8, 8)
        sid_size = 8 + 4 * sid_header[1]
        if 8 + sid_size > int.from_bytes(raw_header[2:4], "little"):
            return False
        sid = ctypes.string_at(entry.value + 8, sid_size)
        if sid not in _PRIVATE_SIDS:
            return False
        has_owner_rights |= sid == _OWNER_RIGHTS_SID
    return has_owner_rights


def restore_directory_inheritance(path: str | os.PathLike[str], *, only_private: bool = False) -> bool:
    """启用父目录 ACL 继承并保留现有允许项；非 Windows 或无匹配项时不修改。"""
    if os.name != "nt":
        return False
    security, kernel = _security_api()
    descriptor = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    native = fs.native_path(path)
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
        if only_private:
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            if not security.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not control.value & _SE_DACL_PROTECTED or not _is_private_temp_acl(security, dacl):
                return False
        if not dacl.value:
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
