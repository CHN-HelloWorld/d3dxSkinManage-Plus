"""管理 Windows 子窗口承载，仅由渲染进程修改原生状态。

禁用的子窗口将输入交给父窗口，从而保留原有控件输入路径。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import lru_cache


@lru_cache(maxsize=1)
def _user32():
    """加载并缓存带有明确参数和返回类型的 Windows 窗口管理函数。"""
    user = ctypes.WinDLL("user32", use_last_error=True)
    signatures = (
        ("IsWindow", [wintypes.HWND], wintypes.BOOL),
        ("GetWindow", [wintypes.HWND, ctypes.c_uint], wintypes.HWND),
        ("GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
        ("SetParent", [wintypes.HWND, wintypes.HWND], wintypes.HWND),
        ("GetWindowLongPtrW", [wintypes.HWND, ctypes.c_int], ctypes.c_ssize_t),
        ("SetWindowLongPtrW", [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t),
        (
            "SetWindowPos",
            [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint],
            wintypes.BOOL,
        ),
        ("EnableWindow", [wintypes.HWND, wintypes.BOOL], wintypes.BOOL),
        ("GetWindowDpiAwarenessContext", [wintypes.HWND], ctypes.c_void_p),
        ("AreDpiAwarenessContextsEqual", [ctypes.c_void_p, ctypes.c_void_p], wintypes.BOOL),
    )
    for name, args, result in signatures:
        fn = getattr(user, name)
        fn.argtypes, fn.restype = args, result
    return user


def parent_alive(hwnd: int, process_id: int) -> bool:
    """检查窗口句柄仍然有效且属于指定进程，防止误用被系统回收的句柄。"""
    user = _user32()
    owner = wintypes.DWORD()
    return bool(
        user.IsWindow(hwnd) and user.GetWindowThreadProcessId(hwnd, ctypes.byref(owner)) and owner.value == process_id
    )


def attach_child(hwnd: int, parent: int, process_id: int) -> None:
    """验证父窗口及 DPI 上下文后嵌入无输入子窗口；原生调用失败时抛出异常。"""
    user = _user32()
    if not parent_alive(parent, process_id):
        raise OSError("The background owner window no longer exists")
    if not user.AreDpiAwarenessContextsEqual(
        user.GetWindowDpiAwarenessContext(hwnd), user.GetWindowDpiAwarenessContext(parent)
    ):
        raise OSError("The background window has a different DPI awareness context")
    # 设置父窗口不会自动修改窗口类型，须先配置子窗口和弹窗标志。
    style = user.GetWindowLongPtrW(hwnd, -16)
    user.SetWindowLongPtrW(hwnd, -16, (style & ~0x80000000) | 0x40000000 | 0x08000000)
    extended = user.GetWindowLongPtrW(hwnd, -20)
    user.SetWindowLongPtrW(hwnd, -20, (extended & ~0x00040000) | 0x08000000)
    ctypes.set_last_error(0)
    if not user.SetParent(hwnd, parent) and ctypes.get_last_error():
        raise ctypes.WinError(ctypes.get_last_error())
    user.EnableWindow(hwnd, False)


def position_child(hwnd: int, rect: tuple[int, int, int, int]) -> None:
    """按原生坐标调整子窗口并禁用其输入；设置位置失败时抛出 Windows 异常。"""
    user = _user32()
    if not user.SetWindowPos(hwnd, 0, *rect, 0x0010 | 0x0020):  # 刷新边框但不激活窗口。
        raise ctypes.WinError(ctypes.get_last_error())
    # Qt 首次显示可能更新原生样式，仍须将输入路由给所有者。
    user.EnableWindow(hwnd, False)


def raise_child(hwnd: int) -> None:
    """将新晋原生控件保持在合成前景范围内。"""
    user = _user32()
    if user.GetWindow(hwnd, 3):  # 前一个同级窗口位于当前窗口之上。
        if not user.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0010 | 0x0001 | 0x0002):
            raise ctypes.WinError(ctypes.get_last_error())
