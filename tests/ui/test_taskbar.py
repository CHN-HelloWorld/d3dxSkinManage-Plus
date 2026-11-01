"""直接读取 Windows 窗口属性，验证任务栏身份及持久图标来源。"""

import ctypes
import subprocess
import sys
from ctypes import wintypes
from uuid import UUID

import pytest
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMainWindow

from src.ui import taskbar
from src.utils.constants import APP_DISPLAY_NAME, MAIN_EXE_NAME
from src.utils.paths import get_resources_dir


def _read_shell_properties(hwnd):
    """通过 IPropertyStore 读取 Shell 的实际值，并释放查询分配的内存。"""
    iid = ctypes.create_string_buffer(UUID("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99").bytes_le)
    store = ctypes.c_void_p()
    assert ctypes.windll.shell32.SHGetPropertyStoreForWindow(hwnd, iid, ctypes.byref(store)) == 0
    vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    read = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(taskbar._PropertyKey), ctypes.POINTER(taskbar._PropVariant)
    )(vtable[5])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
    clear = ctypes.windll.ole32.PropVariantClear
    clear.argtypes = [ctypes.POINTER(taskbar._PropVariant)]
    clear.restype = ctypes.c_long
    values = {}
    try:
        for pid in (2, 3, 4, 5):
            key = taskbar._PropertyKey(
                (ctypes.c_ubyte * 16).from_buffer_copy(UUID("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3").bytes_le), pid
            )
            value = taskbar._PropVariant()
            try:
                assert read(store, ctypes.byref(key), ctypes.byref(value)) == 0
                assert value.header[0] == 31
                values[pid] = ctypes.wstring_at(value.value[0])
            finally:
                clear(ctypes.byref(value))
    finally:
        release(store)
    return values


@pytest.mark.parametrize("frozen", [False, True])
def test_window_publishes_taskbar_icon_and_relaunch_identity(qapp, qtbot, monkeypatch, tmp_path, frozen):
    if sys.platform != "win32" or qapp.platformName() != "windows":
        pytest.skip("需要 Windows 原生窗口属性存储")
    install_dir = tmp_path / "应用目录 with spaces"
    monkeypatch.setattr(taskbar, "is_frozen", lambda: frozen)
    monkeypatch.setattr(taskbar, "get_install_dir", lambda: install_dir)
    previous_icon = qapp.windowIcon()
    qapp.setWindowIcon(QIcon(str(get_resources_dir() / "app.ico")))
    window = QMainWindow()
    qtbot.addWidget(window)
    try:
        taskbar.configure_window(window)
        values = _read_shell_properties(int(window.winId()))
        command = [str(install_dir / MAIN_EXE_NAME)] if frozen else [sys.executable, str(install_dir / "run_app.py")]
        assert values == {
            2: subprocess.list2cmdline(command),
            3: f"{install_dir / MAIN_EXE_NAME},0" if frozen else str(get_resources_dir() / "app.ico"),
            4: APP_DISPLAY_NAME,
            5: "d3dxSkinManage-Plus.App",
        }
        send = ctypes.windll.user32.SendMessageW
        send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        send.restype = ctypes.c_ssize_t
        assert all(send(int(window.winId()), 0x7F, kind, 0) for kind in (0, 1))
    finally:
        window.close()
        qapp.setWindowIcon(previous_icon)
