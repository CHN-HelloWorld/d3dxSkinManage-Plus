"""合并 Windows 10 标题栏拖动，避免高频位置更新积压到桌面呈现链路。"""

from __future__ import annotations

import ctypes
import math
import sys
from ctypes import wintypes
from functools import lru_cache
from weakref import ref

import shiboken6
from PySide6.QtCore import QAbstractNativeEventFilter, Qt, QTimer
from PySide6.QtWidgets import QApplication, QWidget

from src.utils.logger import logger

_WM_SIZE = 0x0005
_WM_SHOWWINDOW = 0x0018
_WM_CANCELMODE = 0x001F
_WM_NCDESTROY = 0x0082
_WM_NCLBUTTONDOWN = 0x00A1
_WM_KEYDOWN = 0x0100
_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONUP = 0x0202
_WM_CAPTURECHANGED = 0x0215
_HTCAPTION = 2
_VK_LBUTTON = 1
_VK_RBUTTON = 2
_VK_ESCAPE = 0x1B
_SM_SWAPBUTTON = 23
_MOVE_FLAGS = 0x0001 | 0x0004 | 0x0010  # NOSIZE | NOZORDER | NOACTIVATE


class _MONITORINFO(ctypes.Structure):
    """对应 Win32 显示器边界和工作区结构，坐标均为原生像素。"""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


@lru_cache(maxsize=1)
def _user32():
    """加载拖动所需的窗口接口，并声明句柄和坐标的原生类型。"""
    user = ctypes.WinDLL("user32", use_last_error=True)
    signatures = (
        ("GetWindowRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
        ("GetCursorPos", [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
        ("GetMessagePos", [], wintypes.DWORD),
        ("GetAsyncKeyState", [ctypes.c_int], ctypes.c_short),
        ("GetSystemMetrics", [ctypes.c_int], ctypes.c_int),
        ("GetCapture", [], wintypes.HWND),
        ("SetCapture", [wintypes.HWND], wintypes.HWND),
        ("ReleaseCapture", [], wintypes.BOOL),
        (
            "SetWindowPos",
            [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT],
            wintypes.BOOL,
        ),
        ("MonitorFromWindow", [wintypes.HWND, wintypes.DWORD], wintypes.HANDLE),
        ("GetMonitorInfoW", [wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)], wintypes.BOOL),
        ("DefWindowProcW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t),
    )
    for name, arguments, result in signatures:
        function = getattr(user, name)
        function.argtypes, function.restype = arguments, result
    return user


def _message_position(value: int) -> tuple[int, int]:
    """解码鼠标消息中的有符号屏幕坐标，支持位于主屏左侧或上方的显示器。"""
    return ctypes.c_short(value & 0xFFFF).value, ctypes.c_short((value >> 16) & 0xFFFF).value


class _WindowMoveFilter(QAbstractNativeEventFilter):
    """共用一份捕获状态和帧定时器，只移动当前正在拖动的顶层窗口。"""

    def __init__(self, app: QApplication):
        """由应用持有定时器，窗口仅保留弱引用；应用退出时释放鼠标捕获。"""
        super().__init__()
        self._user = _user32()
        self._hwnd = None
        self._window_ref = None
        self._origin = self._offset = self._last = (0, 0)
        self._work_area = None
        self._dpr = 1.0
        self._button = _VK_LBUTTON
        self._handing_off = False
        self._timer = QTimer(app)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._flush)
        app.aboutToQuit.connect(self.cancel)

    def _cursor_position(self) -> tuple[int, int] | None:
        """读取当前物理指针，不重放消息队列中的旧位置，也不回写光标。"""
        point = wintypes.POINT()
        if self._user.GetCursorPos(ctypes.byref(point)):
            return point.x, point.y
        return None

    def begin(self, window: QWidget, point: tuple[int, int] | None = None) -> bool:
        """按住左键时捕获普通窗口；最大化、全屏和捕获失败保留系统原有行为。"""
        button = _VK_RBUTTON if self._user.GetSystemMetrics(_SM_SWAPBUTTON) else _VK_LBUTTON
        if (
            self._handing_off
            or not window.isVisible()
            or window.isMaximized()
            or window.isFullScreen()
            or not self._user.GetAsyncKeyState(button) & 0x8000
        ):
            return False
        self.cancel()
        hwnd = int(window.winId())
        frame = wintypes.RECT()
        if not self._user.GetWindowRect(hwnd, ctypes.byref(frame)):
            return False
        point = point if point is not None else self._cursor_position()
        if point is None:
            return False
        self._user.SetCapture(hwnd)
        if self._user.GetCapture() != hwnd:
            return False

        self._hwnd, self._window_ref = hwnd, ref(window)
        self._button = button
        # 入场动画也写入 geometry，开始拖动后必须只保留一个位置写入方。
        animation = getattr(window, "_slide_anim", None)
        if animation is not None and shiboken6.isValid(animation):
            animation.stop()
        self._origin = self._last = (frame.left, frame.top)
        self._offset = (point[0] - frame.left, point[1] - frame.top)
        self._dpr = window.devicePixelRatioF()
        screen = window.screen()
        rate = screen.refreshRate() if screen is not None else 60
        if not math.isfinite(rate) or rate <= 0:
            rate = 60
        # 最多每 8 ms 提交一次；较低刷新率屏幕沿用其帧间隔，持续输入不重启定时器。
        self._timer.setInterval(max(8, math.ceil(1000 / rate)))
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        monitor = self._user.MonitorFromWindow(hwnd, 2)
        self._work_area = None
        if monitor and self._user.GetMonitorInfoW(monitor, ctypes.byref(info)):
            area = info.rcWork
            self._work_area = (area.left, area.top, area.right, area.bottom)
        return True

    def _move_to(self, point: tuple[int, int]) -> None:
        """仅更新窗口位置，保留尺寸、已有像素、层级及激活状态。"""
        target = (point[0] - self._offset[0], point[1] - self._offset[1])
        if target != self._last:
            if not self._user.SetWindowPos(self._hwnd, None, *target, 0, 0, _MOVE_FLAGS):
                self.cancel()
                return
            self._last = target

    def _flush(self) -> None:
        """每帧提交最新指针；按钮已释放时等待释放消息中的准确终点。"""
        if self._hwnd is None:
            return
        window = self._window_ref()
        if (
            window is None
            or not shiboken6.isValid(window)
            or not window.isVisible()
            or int(window.effectiveWinId() or 0) != self._hwnd
            or self._user.GetCapture() != self._hwnd
        ):
            self.cancel()
            return
        if not self._user.GetAsyncKeyState(self._button) & 0x8000:
            return
        point = self._cursor_position()
        if point is None:
            self.cancel()
            return
        if window.devicePixelRatioF() != self._dpr:
            # Qt 已处理原生 DPI 消息，跨屏后的锚点与缩放继续由 Windows 确定。
            self._handoff(point)
            return
        self._move_to(point)
        if self._work_area is not None and self._hwnd is not None:
            left, top, right, bottom = self._work_area
            if point[0] <= left or point[0] >= right - 1 or point[1] <= top or point[1] >= bottom - 1:
                self._handoff(point)

    def _handoff(self, point: tuple[int, int]) -> None:
        """触碰工作区边缘或改变 DPI 时交回系统，保留贴靠及原生跨屏调整。"""
        hwnd = self._hwnd
        self.cancel()
        self._handing_off = True
        try:
            packed = (point[0] & 0xFFFF) | ((point[1] & 0xFFFF) << 16)
            self._user.DefWindowProcW(hwnd, _WM_NCLBUTTONDOWN, _HTCAPTION, packed)
        finally:
            self._handing_off = False

    def cancel(self, *, restore: bool = False) -> None:
        """丢弃待处理位置，仅释放自身捕获；Esc 额外恢复拖动前的位置。"""
        if shiboken6.isValid(self._timer):
            self._timer.stop()
        hwnd = self._hwnd
        self._hwnd = self._window_ref = None
        if hwnd is not None:
            if restore and self._last != self._origin:
                self._user.SetWindowPos(hwnd, None, *self._origin, 0, 0, _MOVE_FLAGS)
            if self._user.GetCapture() == hwnd:
                self._user.ReleaseCapture()

    def nativeEventFilter(self, event_type, message):
        """处理标题栏按下、捕获中的移动与释放，其他窗口消息仍交给 Qt 和 Windows。"""
        if event_type != b"windows_generic_MSG" or self._handing_off:
            return False, 0
        try:
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == _WM_NCLBUTTONDOWN and msg.wParam == _HTCAPTION:
                window = QWidget.find(int(msg.hWnd or 0))
                if window is not None and window.isWindow() and self.begin(window, _message_position(msg.lParam)):
                    return True, 0
            if self._hwnd is None or msg.hWnd != self._hwnd:
                return False, 0
            if msg.message == _WM_MOUSEMOVE:
                if not msg.wParam & 1:
                    self.cancel()
                    return False, 0
                if not self._timer.isActive():
                    self._timer.start()
                return True, 0
            if msg.message == _WM_LBUTTONUP:
                self._move_to(_message_position(self._user.GetMessagePos()))
                self.cancel()
                # Qt 仍需收到释放消息，清理自绘标题栏的隐式鼠标抓取和按钮状态。
            elif msg.message == _WM_KEYDOWN and msg.wParam == _VK_ESCAPE:
                self.cancel(restore=True)
                return True, 0
            elif (
                msg.message in (_WM_CAPTURECHANGED, _WM_CANCELMODE, _WM_NCDESTROY)
                or (msg.message == _WM_SHOWWINDOW and not msg.wParam)
                or (msg.message == _WM_SIZE and msg.wParam in (1, 2))
            ):
                self.cancel()
        except (AttributeError, OSError, RuntimeError) as error:
            self.cancel()
            logger.debug("原生窗口拖动回退: %s", error)
        return False, 0


def install(app: QApplication | None) -> _WindowMoveFilter | None:
    """仅在 Windows 10 原生桌面安装一次共用拖动过滤器，其他平台保持现有行为。"""
    if (
        app is None
        or sys.platform != "win32"
        or app.platformName() != "windows"
        or sys.getwindowsversion().build >= 22000
    ):
        return None
    controller = getattr(app, "_native_window_move", None)
    if controller is None:
        try:
            controller = _WindowMoveFilter(app)
            app.installNativeEventFilter(controller)
            app._native_window_move = controller
        except (AttributeError, OSError, RuntimeError) as error:
            if controller is not None:
                controller.cancel()
                app.aboutToQuit.disconnect(controller.cancel)
                controller._timer.deleteLater()
            controller = None
            logger.debug("原生窗口拖动不可用: %s", error)
    return controller


def begin_native_move(window: QWidget) -> bool:
    """让自绘标题栏进入共用拖动路径；非原生输入或接口不可用时由调用方回退。"""
    controller = install(QApplication.instance())
    if controller is None:
        return False
    try:
        return controller.begin(window)
    except (AttributeError, OSError, RuntimeError) as error:
        controller.cancel()
        logger.debug("开始原生窗口拖动失败: %s", error)
        return False
