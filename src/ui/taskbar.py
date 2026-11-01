"""为 Windows 任务栏提供明确的应用身份、图标资源和重新启动信息。"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes
from uuid import UUID

from PySide6.QtWidgets import QApplication, QWidget

from src.utils.constants import APP_DISPLAY_NAME, APP_NAME, MAIN_EXE_NAME
from src.utils.logger import logger
from src.utils.paths import get_install_dir, get_resources_dir, is_frozen

_APP_ID = f"{APP_NAME}.App"
_APP_MODEL_GUID = UUID("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3").bytes_le
_PROPERTY_STORE_IID = UUID("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99").bytes_le
_RELAUNCH_COMMAND = 2
_RELAUNCH_ICON_RESOURCE = 3
_RELAUNCH_DISPLAY_NAME_RESOURCE = 4
_APP_USER_MODEL_ID = 5
_VT_LPWSTR = 31


class _PropertyKey(ctypes.Structure):
    """按原生 PROPERTYKEY 布局保存属性集 GUID 和属性编号。"""

    _fields_ = [("fmtid", ctypes.c_ubyte * 16), ("pid", ctypes.c_uint32)]


class _PropVariant(ctypes.Structure):
    """为 PROPVARIANT 的类型字段及联合体预留与指针宽度匹配的空间。"""

    _fields_ = [("header", ctypes.c_uint16 * 4), ("value", ctypes.c_void_p * 2)]


def configure_process() -> None:
    """在创建窗口前登记进程应用标识，使任务栏按应用分组。"""
    if sys.platform != "win32":
        return
    try:
        setter = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        setter.argtypes = [wintypes.LPCWSTR]
        setter.restype = ctypes.c_long
        result = setter(_APP_ID)
        if result < 0:
            raise OSError(f"SetCurrentProcessExplicitAppUserModelID: {result:#x}")
    except Exception:
        logger.exception("登记任务栏进程标识失败")


def _set_properties(hwnd: int, values: dict[int, str]) -> None:
    """写入窗口属性存储；字符串在 SetValue 复制完成前由当前调用持有。"""
    shell32 = ctypes.windll.shell32
    shell32.SHGetPropertyStoreForWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
    store = ctypes.c_void_p()
    iid = ctypes.create_string_buffer(_PROPERTY_STORE_IID)
    result = shell32.SHGetPropertyStoreForWindow(hwnd, iid, ctypes.byref(store))
    if result < 0:
        raise OSError(f"SHGetPropertyStoreForWindow: {result:#x}")

    vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
    try:
        set_value = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant)
        )(vtable[6])
        for property_id, text in values.items():
            key = _PropertyKey((ctypes.c_ubyte * 16).from_buffer_copy(_APP_MODEL_GUID), property_id)
            buffer = ctypes.create_unicode_buffer(text)
            value = _PropVariant()
            value.header[0] = _VT_LPWSTR
            value.value[0] = ctypes.cast(buffer, ctypes.c_void_p).value
            result = set_value(store, ctypes.byref(key), ctypes.byref(value))
            if result < 0:
                raise OSError(f"IPropertyStore.SetValue({property_id}): {result:#x}")
    finally:
        release(store)


def configure_window(window: QWidget) -> None:
    """显示主窗口前显式绑定图标，并为任务栏和固定入口登记持久资源路径。"""
    window.setWindowIcon(QApplication.windowIcon())
    if sys.platform != "win32" or QApplication.platformName() != "windows":
        return
    install_dir = get_install_dir()
    if is_frozen():
        executable = install_dir / MAIN_EXE_NAME
        command = [str(executable)]
        # 单文件解包路径会在退出后消失，固定入口必须从安装目录的 EXE 提取图标。
        icon_resource = f"{executable},0"
    else:
        command = [sys.executable, str(install_dir / "run_app.py")]
        icon_resource = str(get_resources_dir() / "app.ico")
    try:
        # Shell 仅在重新启动命令与显示名同时存在时使用指定的图标资源；最后发布应用标识。
        _set_properties(
            int(window.winId()),
            {
                _RELAUNCH_COMMAND: subprocess.list2cmdline(command),
                _RELAUNCH_ICON_RESOURCE: icon_resource,
                _RELAUNCH_DISPLAY_NAME_RESOURCE: APP_DISPLAY_NAME,
                _APP_USER_MODEL_ID: _APP_ID,
            },
        )
    except Exception:
        logger.exception("登记任务栏窗口图标失败")
