"""验证原生拖动只提交最新位置，释放或取消后不再追赶指针。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog


class MouseWindowApi:
    """记录窗口系统边界，不模拟控制器的合并、取消或贴靠决策。"""

    def __init__(self, hwnd):
        """设置独立的物理坐标、鼠标状态和窗口区域。"""
        self.hwnd = hwnd
        self.cursor = (280, 135)
        self.release = (280, 135)
        self.frame = (100, 120, 500, 400)
        self.capture = None
        self.pressed = True
        self.allow_capture = True
        self.swapped = False
        self.moves = []
        self.handoffs = []

    def GetWindowRect(self, hwnd, pointer):
        """返回窗口当前的原生边框。"""
        assert hwnd == self.hwnd
        rect = ctypes.cast(pointer, ctypes.POINTER(wintypes.RECT)).contents
        rect.left, rect.top, rect.right, rect.bottom = self.frame
        return True

    def GetCursorPos(self, pointer):
        """返回当前物理指针，独立于消息中已经过期的坐标。"""
        point = ctypes.cast(pointer, ctypes.POINTER(wintypes.POINT)).contents
        point.x, point.y = self.cursor
        return True

    def GetMessagePos(self):
        """返回释放消息发生时的位置，保留负坐标的有符号编码。"""
        x, y = self.release
        return (x & 0xFFFF) | ((y & 0xFFFF) << 16)

    def GetAsyncKeyState(self, key):
        """提供左键的物理状态。"""
        assert key == (2 if self.swapped else 1)
        return 0x8000 if self.pressed else 0

    def GetSystemMetrics(self, index):
        """提供主鼠标按钮的左右键映射。"""
        assert index == 23
        return self.swapped

    def GetCapture(self):
        """返回当前鼠标捕获所有者。"""
        return self.capture

    def SetCapture(self, hwnd):
        """按接口结果转移捕获，允许验证捕获失败后的回退。"""
        previous = self.capture
        if self.allow_capture:
            self.capture = hwnd
        return previous

    def ReleaseCapture(self):
        """释放仍属于测试窗口的捕获。"""
        assert self.capture == self.hwnd
        self.capture = None
        return True

    def SetWindowPos(self, hwnd, after, x, y, width, height, flags):
        """只接受不改变尺寸、层级或激活状态的移动请求。"""
        assert (hwnd, after, width, height, flags) == (self.hwnd, None, 0, 0, 0x0015)
        self.moves.append((x, y))
        self.frame = (x, y, x + 400, y + 280)
        return True

    def MonitorFromWindow(self, hwnd, flags):
        """返回用于确定贴靠边界的显示器。"""
        return 1

    def GetMonitorInfoW(self, monitor, pointer):
        """提供包含负坐标的桌面工作区。"""
        from src.ui.native_move import _MONITORINFO

        info = ctypes.cast(pointer, ctypes.POINTER(_MONITORINFO)).contents
        info.rcWork = wintypes.RECT(-1280, 0, 1920, 1080)
        return True

    def DefWindowProcW(self, hwnd, message, hit, point):
        """记录交给系统的贴靠或跨屏拖动，不替控制器启动新的拖动。"""
        self.handoffs.append((hwnd, message, hit, point))
        return 0


@pytest.fixture
def moving_window(qapp, qtbot, monkeypatch):
    """使用真实 Qt 定时器与窗口，仅隔离外部 Win32 接口。"""
    from src.ui import native_move

    window = QDialog(None, Qt.WindowType.FramelessWindowHint)
    qtbot.addWidget(window)
    window.setGeometry(100, 120, 400, 280)
    window.show()
    qapp.processEvents()
    screen = SimpleNamespace(refreshRate=lambda: 144)
    monkeypatch.setattr(window, "screen", lambda: screen)
    monkeypatch.setattr(window, "devicePixelRatioF", lambda: 1.0)
    api = MouseWindowApi(int(window.winId()))
    monkeypatch.setattr(native_move, "_user32", lambda: api)
    controller = native_move._WindowMoveFilter(qapp)
    yield window, api, controller
    controller.cancel()
    qapp.aboutToQuit.disconnect(controller.cancel)
    controller._timer.deleteLater()


def send(controller, hwnd, message, wparam=0, lparam=0):
    """通过原生过滤器入口投递一条窗口消息。"""
    msg = wintypes.MSG()
    msg.hWnd, msg.message, msg.wParam, msg.lParam = hwnd, message, wparam, lparam
    return controller.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg))


def test_coalesces_moves_and_commits_release_position(moving_window, qtbot):
    window, api, controller = moving_window
    assert controller.begin(window)
    send(controller, api.hwnd, 0x0200, 1)
    timer_id = controller._timer.timerId()
    for _ in range(40):
        send(controller, api.hwnd, 0x0200, 1, 10)
    assert controller._timer.timerId() == timer_id
    assert api.moves == []
    api.cursor = (555, 315)
    qtbot.waitUntil(lambda: api.moves == [(375, 300)])

    send(controller, api.hwnd, 0x0200, 1)
    api.release, api.cursor, api.pressed = (580, 345), (1000, 700), False
    assert send(controller, api.hwnd, 0x0202) == (False, 0)
    qtbot.wait(20)
    assert api.moves == [(375, 300), (400, 330)]
    assert api.capture is None
    assert not controller._timer.isActive()


@pytest.mark.parametrize("message,wparam", [(0x0215, 0), (0x001F, 0), (0x0018, 0), (0x0005, 1), (0x0082, 0)])
def test_capture_loss_hide_or_destroy_discards_pending_move(moving_window, qtbot, message, wparam):
    window, api, controller = moving_window
    assert controller.begin(window)
    send(controller, api.hwnd, 0x0200, 1)
    api.cursor = (500, 500)
    if message == 0x0215:
        api.capture = 999
    assert send(controller, api.hwnd, message, wparam) == (False, 0)
    qtbot.wait(20)
    assert api.moves == []
    assert api.capture == (999 if message == 0x0215 else None)
    assert not controller._timer.isActive()


def test_escape_restores_drag_origin(moving_window):
    window, api, controller = moving_window
    assert controller.begin(window)
    api.cursor = (555, 315)
    controller._flush()
    assert send(controller, api.hwnd, 0x0100, 0x1B) == (True, 0)
    assert api.moves == [(375, 300), (100, 120)]
    assert api.capture is None


def test_work_area_edge_hands_back_to_system_move(moving_window):
    window, api, controller = moving_window
    assert controller.begin(window)
    api.cursor = (-1280, 180)
    controller._flush()
    assert api.moves == [(-1460, 165)]
    assert api.capture is None
    assert api.handoffs == [(api.hwnd, 0x00A1, 2, (-1280 & 0xFFFF) | (180 << 16))]


def test_dpi_change_hands_back_to_system_move(moving_window, monkeypatch):
    window, api, controller = moving_window
    assert controller.begin(window)
    monkeypatch.setattr(window, "devicePixelRatioF", lambda: 1.25)
    controller._flush()
    assert api.capture is None
    assert api.handoffs == [(api.hwnd, 0x00A1, 2, 280 | (135 << 16))]


def test_failed_capture_preserves_the_existing_owner(moving_window):
    window, api, controller = moving_window
    api.capture, api.allow_capture = 999, False
    assert not controller.begin(window)
    assert api.capture == 999
    assert not controller._timer.isActive()


def test_native_caption_uses_the_shared_controller(moving_window):
    _window, api, controller = moving_window
    assert send(controller, api.hwnd, 0x00A1, 2, 280 | (135 << 16)) == (True, 0)
    assert api.capture == api.hwnd
    assert send(controller, api.hwnd, 0x00A3, 2) == (False, 0)


def test_swapped_primary_button_still_starts_drag(moving_window):
    window, api, controller = moving_window
    api.swapped = True
    assert controller.begin(window)


def test_drag_stops_an_unfinished_entrance_animation(moving_window, qtbot):
    from PySide6.QtCore import QAbstractAnimation, QPropertyAnimation, QRect

    window, _api, controller = moving_window
    animation = QPropertyAnimation(window, b"geometry", window)
    animation.setStartValue(QRect(100, 80, 400, 280))
    animation.setEndValue(QRect(100, 120, 400, 280))
    animation.setDuration(200)
    window._slide_anim = animation
    animation.start()
    assert controller.begin(window)
    geometry = window.geometry()
    qtbot.wait(25)
    assert animation.state() == QAbstractAnimation.State.Stopped
    assert window.geometry() == geometry


@pytest.mark.parametrize(
    "platform,build,backend",
    [("linux", 19045, "windows"), ("win32", 22631, "windows"), ("win32", 19045, "offscreen")],
)
def test_other_platforms_do_not_install_the_win10_move_filter(qapp, monkeypatch, platform, build, backend):
    from src.ui import native_move

    monkeypatch.setattr(
        native_move, "sys", SimpleNamespace(platform=platform, getwindowsversion=lambda: SimpleNamespace(build=build))
    )
    monkeypatch.setattr(qapp, "platformName", lambda: backend)
    monkeypatch.setattr(native_move, "_WindowMoveFilter", lambda _app: pytest.fail("不应安装 Win10 拖动过滤器"))
    assert native_move.install(qapp) is None
