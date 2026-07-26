"""通过 Windows 图形捕获读取本应用创建的场景窗口。

进程操作、窗口发现和捕获清理均在界面线程之外执行，仅传递最新独立图像。
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import threading
import time
import uuid
import weakref
from ctypes import wintypes
from pathlib import Path

import shiboken6
from PySide6.QtCore import QCoreApplication, QObject, Signal
from PySide6.QtGui import QImage

from src.utils.i18n import t as tr


def validate_engine_path(path):
    # 延迟导入原生后端，避免普通启动加载可选捕获模块。
    """延迟导入原生后端并返回引擎可执行文件的验证结果及错误说明。"""
    from .engine import validate_engine_path as validate

    return validate(path)


def validate_scene_project(path: str) -> Path:
    """验证场景项目类型、素材路径边界及文件存在性，返回规范化后的项目路径。

    允许场景配置存放在 scene.pkg 中；无效项目或读取失败均转换为可展示的 ValueError。
    """
    project = Path(path).expanduser().resolve()
    if project.name.lower() != "project.json" or not project.is_file():
        raise ValueError(tr("请选择场景素材的 project.json 文件"))
    try:
        data = json.loads(project.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("type"), str) or data["type"].casefold() != "scene":
            raise ValueError(tr("素材不是 Wallpaper Engine 场景"))
        filename = data.get("file")
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError(tr("场景素材未指定文件"))
        relative = Path(filename)
        target = (project.parent / relative).resolve()
        if relative.is_absolute() or ".." in relative.parts or not target.is_relative_to(project.parent):
            raise ValueError(tr("场景文件不能超出素材目录"))
        # 场景配置可位于场景包内，由 Wallpaper Engine 打开项目时解析。
        packed_scene = project.parent / "scene.pkg"
        packed = (
            relative.as_posix().lower() == "scene.json"
            and packed_scene.is_file()
            and packed_scene.resolve().is_relative_to(project.parent)
        )
        if not target.is_file() and not packed:
            raise ValueError(tr("场景素材文件不存在"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(tr("无法读取场景素材：{exc}", exc=exc)) from exc
    return project


class _Windows:
    """封装场景窗口查找、屏幕外布局及隐藏所有者窗口的 Win32 资源操作。"""

    def __init__(self):
        """加载 user32 接口并声明指针宽度正确的参数类型，初始化隐藏所有者句柄。"""
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.api.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        self.api.FindWindowW.restype = wintypes.HWND
        self.api.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        self.api.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.api.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.api.GetWindowLongW.restype = wintypes.LONG
        self.api.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
        self.api.SetWindowLongW.restype = wintypes.LONG
        self.api.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.api.IsWindowVisible.argtypes = [wintypes.HWND]
        self.api.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        self.api.CreateWindowExW.restype = wintypes.HWND
        self.api.DestroyWindow.argtypes = [wintypes.HWND]
        self.api.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        self.api.GetWindow.restype = wintypes.HWND
        self._set_owner = self.api.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else self.api.SetWindowLongW
        self._set_owner.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        self._set_owner.restype = ctypes.c_ssize_t
        self._owner = None

    def find(self, name):
        """按唯一窗口标题查找场景窗口，未发现时返回空句柄。"""
        return self.api.FindWindowW(None, name)

    def offscreen_x(self):
        """返回虚拟桌面右侧之外的横坐标，供场景窗口离屏显示。"""
        return self.api.GetSystemMetrics(76) + self.api.GetSystemMetrics(78) + 100

    def resize(self, hwnd, size):
        # 隐藏所有者窗口可移除任务栏入口，无需使用会影响图形捕获资格的工具窗口标志。
        """创建隐藏所有者并调整场景窗口样式、位置和尺寸，使其可捕获且不显示任务栏入口。

        原生接口失败时抛出系统错误，由捕获线程统一处理和回收。
        """
        if self._owner is None:
            self._owner = self.api.CreateWindowExW(0, "STATIC", "", 0x80000000, 0, 0, 0, 0, None, None, None, None)
            if not self._owner:
                raise ctypes.WinError(ctypes.get_last_error())
        if self.api.GetWindow(hwnd, 4) != self._owner:  # GW_OWNER
            self.api.ShowWindow(hwnd, 0)
            ctypes.set_last_error(0)
            previous = self._set_owner(hwnd, -8, self._owner)  # GWLP_HWNDPARENT
            if not previous and ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
        style = self.api.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
        hidden_style = style & ~0x40000
        if style != hidden_style:
            ctypes.set_last_error(0)
            previous = self.api.SetWindowLongW(hwnd, -20, hidden_style)
            if not previous and ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
        if not self.api.SetWindowPos(hwnd, 1, self.offscreen_x(), 0, *size, 0x0010 | 0x0040 | 0x0020):
            raise OSError(tr("无法调整场景窗口大小"))

    def dispose(self):
        """销毁本对象创建的隐藏所有者窗口并清空句柄。"""
        if self._owner:
            self.api.DestroyWindow(self._owner)
            self._owner = None

    def close(self, name):
        # 每次按会话身份重新查找，避免使用已被复用的窗口句柄。
        """按唯一标题重新查找场景窗口并投递关闭消息，避免误用已复用的旧句柄。"""
        hwnd = self.find(name)
        if hwnd:
            self.api.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
