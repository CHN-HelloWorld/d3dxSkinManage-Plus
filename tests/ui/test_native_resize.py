from __future__ import annotations

from ctypes import POINTER, cast, wintypes
from types import SimpleNamespace

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QDialog

from src.ui.dialog_chrome import ResizeHandle
from src.ui.native_resize import NativeResize


def test_partially_offscreen_resize_keeps_native_positioning():
    screen = SimpleNamespace(geometry=lambda: QRect(0, 0, 1920, 1080))
    window = SimpleNamespace(
        geometry=lambda: QRect(80, 80, 600, 400),
        screen=lambda: screen,
        devicePixelRatioF=lambda: 1.25,
        winId=lambda: 123,
    )
    positions = []

    def get_rect(hwnd, pointer):
        rect = cast(pointer, POINTER(wintypes.RECT)).contents
        rect.left, rect.top, rect.right, rect.bottom = 100, 100, 850, 600
        return True

    user = SimpleNamespace(GetWindowRect=get_rect, SetWindowPos=lambda *args: positions.append(args) or True)
    resize = NativeResize(window, user, 123, (100, 100, 750, 500))
    assert resize.apply(window, QRect(-40, 80, 720, 400))
    assert positions == [(123, None, -50, 100, 900, 500, 0x0114)]


def test_resize_reanchors_after_monitor_change(qtbot, monkeypatch):
    from src.ui import native_resize

    dialog = QDialog()
    qtbot.addWidget(dialog)
    dialog.setGeometry(100, 100, 600, 400)
    handle = ResizeHandle(dialog, Qt.Edge.RightEdge, Qt.CursorShape.SizeHorCursor)
    handle._drag_origin = QPoint(100, 100)
    handle._initial_geometry = QRect(dialog.geometry())
    handle._native_resize = SimpleNamespace(apply=lambda *_: False)
    next_resize = SimpleNamespace(apply=lambda window, rect: window.setGeometry(rect) or True)
    monkeypatch.setattr(native_resize, "begin_native_resize", lambda window: next_resize)
    handle._pending_position = QPoint(120, 100)
    handle._apply_pending_resize()
    assert dialog.width() == 620
    assert handle._native_resize is next_resize, "A monitor change must not disable the rest of this drag"
    handle._pending_position = QPoint(135, 100)
    handle._apply_pending_resize()
    assert dialog.width() == 635
