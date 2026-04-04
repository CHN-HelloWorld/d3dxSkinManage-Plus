"""Windows 文件监控适配层，保留原生通知溢出信息。

直接读取系统缓冲并发布父目录移动，后代处理交给索引中的子树对账，避免递归枚举移动目录。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from functools import lru_cache
from pathlib import Path

from src.utils import fs


class _NativeAPI:
    """封装目录监听和文件身份查询所需的 Windows 原生接口及 ctypes 数据结构。"""

    def __init__(self):
        """声明内核函数签名并构造文件身份结构，确保原生调用使用正确参数与返回类型。"""
        import ctypes as c
        from ctypes import wintypes as w

        self.c, self.w = c, w
        self.k = c.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateFileW": (w.HANDLE, [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]),
            "CloseHandle": (w.BOOL, [w.HANDLE]),
            "OpenFileById": (w.HANDLE, [w.HANDLE, c.c_void_p, w.DWORD, w.DWORD, c.c_void_p, w.DWORD]),
            "ReadFile": (w.BOOL, [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]),
            "GetFileInformationByHandleEx": (w.BOOL, [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]),
            "GetFileInformationByHandle": (w.BOOL, [w.HANDLE, c.c_void_p]),
            "GetFinalPathNameByHandleW": (w.DWORD, [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]),
            "CreateEventW": (w.HANDLE, [c.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]),
            "ReadDirectoryChangesW": (
                w.BOOL,
                [w.HANDLE, c.c_void_p, w.DWORD, w.BOOL, w.DWORD, c.c_void_p, c.c_void_p, c.c_void_p],
            ),
            "GetOverlappedResult": (w.BOOL, [w.HANDLE, c.c_void_p, c.POINTER(w.DWORD), w.BOOL]),
            "CancelIoEx": (w.BOOL, [w.HANDLE, c.c_void_p]),
        }
        for name, (result, arguments) in signatures.items():
            method = getattr(self.k, name)
            method.restype, method.argtypes = result, arguments

        # 复用 ctypes 结构定义，减少重复描述符和垃圾回收环；每次读取使用独立缓冲。
        class Identifier(c.Union):
            """映射文件编号联合体，容纳传统及扩展文件身份。"""

            _fields_ = [("file_id", c.c_longlong), ("extended", c.c_byte * 16)]

        class Descriptor(c.Structure):
            """映射按身份打开文件时使用的编号描述结构。"""

            _fields_ = [("size", w.DWORD), ("kind", c.c_int), ("identifier", Identifier)]

        class Information(c.Structure):
            """映射句柄文件信息，读取卷序号及高低位文件编号。"""

            _fields_ = [
                ("attributes", w.DWORD),
                ("times", w.DWORD * 6),
                ("volume", w.DWORD),
                ("size_high", w.DWORD),
                ("size_low", w.DWORD),
                ("links", w.DWORD),
                ("index_high", w.DWORD),
                ("index_low", w.DWORD),
            ]

        self.Identifier, self.Descriptor, self.Information = Identifier, Descriptor, Information

        class FileIdInformation(c.Structure):
            """映射包含卷序号和 128 位文件编号的扩展身份信息。"""

            _fields_ = [("volume", c.c_ulonglong), ("identifier", c.c_ubyte * 16)]

        self.FileIdInformation = FileIdInformation

    def open(self, path, *, observe=False):
        """以共享访问模式打开路径，监听模式启用重叠读写；失败时抛出原生错误。"""
        handle = self.k.CreateFileW(
            fs.native_path(path), 1 if observe else 0x80, 7, None, 3, 0x02000000 | (0x40000000 if observe else 0), None
        )
        if handle == self.c.c_void_p(-1).value:
            raise self.c.WinError(self.c.get_last_error())
        return handle

    def open_by_id(self, path, file_id, volume_hint=None, *, access=0x80, share=7):
        """按文件标识持有句柄，不锁定祖先目录名称。

        Windows 上按路径打开后代可能阻止祖先改名，因此基线不保留这种句柄。
        """
        c = self.c
        descriptor = self.Descriptor(c.sizeof(self.Descriptor), 0, self.Identifier(file_id=file_id))
        temporary = volume_hint is None
        if temporary:
            volume_hint = self.open(Path(path).anchor)
        try:
            handle = self.k.OpenFileById(volume_hint, c.byref(descriptor), access, share, None, 0x02000000)
            if handle in (None, 0, c.c_void_p(-1).value):
                raise c.WinError(c.get_last_error())
            return handle
        finally:
            if temporary:
                self.k.CloseHandle(volume_hint)

    def read_marker_by_id(self, path, info, volume_hint=None):
        """按标识读取，避免子路径句柄阻止父目录改名。"""
        handle = self.open_by_id(path, info.st_ino, volume_hint, access=0x80000000)
        try:
            if self.identity(handle) != (info.st_dev, info.st_ino):
                raise OSError("marker identity changed before read")
            buffer = self.c.create_string_buffer(info.st_size + 1)
            size = self.w.DWORD()
            if not self.k.ReadFile(handle, buffer, len(buffer), self.c.byref(size), None):
                raise self.c.WinError(self.c.get_last_error())
            if size.value != info.st_size:
                raise OSError("marker size changed during read")
            return buffer.raw[: size.value].decode("utf-8")
        finally:
            self.k.CloseHandle(handle)

    def identity(self, handle):
        """读取句柄的卷和文件编号，优先使用扩展身份并在不可用时回退到传统接口。"""
        c = self.c
        # CPython 3.12 起使用 64 位卷序列号和 128 位文件标识，须采用相同结构比较身份。
        if sys.version_info >= (3, 12):
            extended = self.FileIdInformation()
            if self.k.GetFileInformationByHandleEx(handle, 18, c.byref(extended), c.sizeof(extended)):
                return extended.volume, int.from_bytes(bytes(extended.identifier), "little")
        info = self.Information()
        if not self.k.GetFileInformationByHandle(handle, c.byref(info)):
            raise c.WinError(c.get_last_error())
        return info.volume, (info.index_high << 32) | info.index_low
