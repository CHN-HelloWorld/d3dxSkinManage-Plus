"""禁用 Windows 控制台的快速编辑模式。

控制台选择状态会阻塞标准输出，并使同步日志写入阻塞主线程。
这里只调整输入模式；无控制台或设置失败时保持原状。
"""

from __future__ import annotations

import sys

# Windows Console API 常量（wincon.h）
_STD_INPUT_HANDLE = -10
_INVALID_HANDLE_VALUE = -1
_ENABLE_QUICK_EDIT_MODE = 0x0040
# 修改 QuickEdit 或 InsertMode 时须同时设置 ENABLE_EXTENDED_FLAGS，
# 否则 SetConsoleMode 不会应用这些扩展模式位。
_ENABLE_EXTENDED_FLAGS = 0x0080


def disable_quick_edit_mode() -> bool:
    """禁用快速编辑模式，避免选择控制台文字时阻塞日志写入。

    非 Windows、无有效控制台或设置失败时返回 False，不阻断应用启动。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
        if handle in (0, _INVALID_HANDLE_VALUE, None):
            return False

        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # 无控制台（stdin 被重定向到管道/NUL，或打包成 GUI）时必然失败
            return False

        # 保留其余位，只清 QuickEdit 并确保 EXTENDED_FLAGS 置位
        new_mode = (mode.value & ~_ENABLE_QUICK_EDIT_MODE) | _ENABLE_EXTENDED_FLAGS
        if new_mode == mode.value:
            return True  # 已经是目标状态
        return bool(kernel32.SetConsoleMode(handle, new_mode))
    except Exception:
        return False
