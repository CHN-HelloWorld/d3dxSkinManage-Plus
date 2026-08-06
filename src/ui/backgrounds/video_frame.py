"""映射视频缓冲时释放 GIL，避免解码或 GPU 等待阻塞界面。

Windows x64 使用公开的 QVideoFrame 导出方法和统一调用约定，不读取对象内部布局。
"""

from __future__ import annotations

import ctypes
import platform
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import shiboken6
from PySide6.QtMultimedia import QVideoFrame


@lru_cache(maxsize=1)
def _native_functions():
    """缓存 Windows x64 的视频帧映射导出函数；平台或 Qt 导出不支持时返回 None。"""
    if (
        sys.platform != "win32"
        or ctypes.sizeof(ctypes.c_void_p) != 8
        or platform.machine().lower() not in ("amd64", "x86_64")
    ):
        return None
    try:
        # 复用 Qt 已加载的多媒体模块，不搜索其他安装目录。
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        get_module = kernel.GetModuleHandleW
        get_module.argtypes = [ctypes.c_wchar_p]
        get_module.restype = ctypes.c_void_p
        handle = get_module("Qt6Multimedia.dll")
        if not handle:
            return None
        library = ctypes.CDLL("Qt6Multimedia.dll", handle=handle)
        map_frame = getattr(library, "?map@QVideoFrame@@QEAA_NW4MapMode@1@@Z")
        unmap_frame = getattr(library, "?unmap@QVideoFrame@@QEAAXXZ")
        map_frame.argtypes = [ctypes.c_void_p, ctypes.c_int]
        map_frame.restype = ctypes.c_bool
        unmap_frame.argtypes = [ctypes.c_void_p]
        unmap_frame.restype = None
        # 在所有调用期间保留动态库和类型化函数。
        return library, map_frame, unmap_frame
    except (OSError, AttributeError):
        return None


def map_video_frame(frame: QVideoFrame, mode: QVideoFrame.MapMode) -> bool:
    """映射视频帧，取消映射前调用方必须独占访问。"""
    native = _native_functions()
    if native is None:
        return frame.map(mode)
    # 原生调用期间保留视频帧的强引用。
    return bool(native[1](shiboken6.getCppPointer(frame)[0], mode.value))


def unmap_video_frame(frame: QVideoFrame) -> None:
    """取消映射时仍保持帧对象有效。"""
    native = _native_functions()
    if native is None:
        frame.unmap()
        return
    native[2](shiboken6.getCppPointer(frame)[0])


@lru_cache(maxsize=1)
def _native_clear():
    """获取并缓存 QVideoFrame 赋值导出，以便释放缓冲时暂时释放 GIL。"""
    native = _native_functions()
    if native is None:
        return None
    try:
        assign = getattr(native[0], "??4QVideoFrame@@QEAAAEAV0@AEBV0@@Z")
        assign.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        assign.restype = ctypes.c_void_p
        return assign
    except AttributeError:
        return None


def clear_video_frame(frame: QVideoFrame) -> None:
    """在所属工作线程释放帧的 GPU 缓冲，期间不持有 GIL。"""
    empty = QVideoFrame()
    assign = _native_clear()
    if assign is not None:
        assign(shiboken6.getCppPointer(frame)[0], shiboken6.getCppPointer(empty)[0])
    # 原生导出不可用时，最后的包装引用也由工作线程释放。


@lru_cache(maxsize=1)
def _retirement_pool():
    # GPU 缓冲等待不占 Qt 共享工作池，独立队列保留帧直到释放。
    """惰性创建单线程释放队列，避免等待 GPU 缓冲占用 Qt 的共享线程池。"""
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix="video-frame-release")


def retire_video_frame(frame: QVideoFrame):
    """把视频帧交给独立工作线程释放，并返回用于观察完成状态的 Future。"""
    future = _retirement_pool().submit(clear_video_frame, frame)

    def finished(result):
        """读取释放任务的异常并写入日志，避免异步错误被静默丢弃。"""
        error = result.exception()
        if error is not None:
            from src.utils.logger import logger

            logger.warning("视频帧释放失败: %s", error)

    future.add_done_callback(finished)
    return future
