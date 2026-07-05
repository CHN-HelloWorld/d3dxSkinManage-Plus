"""调整 Windows 对话框位置，避免同步重绘。"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from functools import lru_cache

from PySide6.QtCore import QPointF, QRect, QSizeF, Qt


@lru_cache(maxsize=1)
def _user32():
    """加载窗口位置相关 Win32 接口，并声明 ctypes 参数与返回值类型。"""
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.GetWindowRect.restype = wintypes.BOOL
    user.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    user.SetWindowPos.restype = wintypes.BOOL
    return user


class NativeResize:
    """一次拖动在同一屏幕使用固定的逻辑与原生坐标基准。"""

    def __init__(self, window, user, hwnd, frame):
        """捕获窗口逻辑几何、原生边框、显示器和缩放比例，作为一次拖动的坐标基准。"""
        self._user = user
        self._hwnd = hwnd
        self._geometry = QRect(window.geometry())
        self._frame = frame
        self._screen = window.screen()
        self._dpr = window.devicePixelRatioF()
        self._screen_geometry = self._screen.geometry()
        self._base_pixels = self._pixels(self._geometry)

    def _pixels(self, geometry):
        """按基准显示器原点和缩放比例，将逻辑位置与尺寸转换为舍入后的像素值。"""
        position = (QPointF(geometry.topLeft() - self._screen_geometry.topLeft()) * self._dpr).toPoint()
        size = (QSizeF(geometry.size()) * self._dpr).toSize()
        return position.x(), position.y(), size.width(), size.height()

    def apply(self, window, geometry):
        # 跨显示器或缩放变化时重新确定基准，部分越出屏幕不改变坐标系。
        """根据拖动基准调整原生窗口位置与尺寸，同时保留窗口层级和激活状态。

        显示器、缩放或窗口句柄发生变化时返回 False，由调用方重新建立基准或回退到 Qt 调整。"""
        if (
            window.screen() is not self._screen
            or window.devicePixelRatioF() != self._dpr
            or int(window.winId()) != self._hwnd
            or self._screen.geometry() != self._screen_geometry
        ):
            return False
        # 对完整位置和尺寸按 Qt 规则舍入，避免仅舍入增量产生缩放误差。
        target = tuple(
            native + value - origin
            for native, value, origin in zip(self._frame, self._pixels(geometry), self._base_pixels)
        )
        current = wintypes.RECT()
        if not self._user.GetWindowRect(self._hwnd, ctypes.byref(current)):
            return False
        if target == (current.left, current.top, current.right - current.left, current.bottom - current.top):
            return True
        # 不改变层级、不激活窗口、不复制旧像素，由 Qt 返回输入循环后统一绘制。
        return bool(self._user.SetWindowPos(self._hwnd, None, *target, 0x0004 | 0x0010 | 0x0100))


def begin_native_resize(window):
    """为 Windows 无边框窗口捕获原生缩放基准；平台不支持或接口失败时返回 None。"""
    if sys.platform != "win32" or not window.windowFlags() & Qt.WindowType.FramelessWindowHint:
        return None
    try:
        user = _user32()
        hwnd = int(window.winId())
        frame = wintypes.RECT()
        if not user.GetWindowRect(hwnd, ctypes.byref(frame)):
            return None
        return NativeResize(
            window, user, hwnd, (frame.left, frame.top, frame.right - frame.left, frame.bottom - frame.top)
        )
    except (AttributeError, OSError, RuntimeError):
        return None
