from __future__ import annotations

import ctypes
from ctypes import wintypes
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QDialog

from src.ui import acrylic, theme, win_effects
from src.ui.dialog_chrome import ResizeHandle


@pytest.fixture
def policies(monkeypatch, qapp):
    """观察真实 Win32 调用边界，原生运行同时执行实际设置。"""
    calls = []
    monkeypatch.setattr(
        win_effects,
        "sys",
        SimpleNamespace(
            platform="win32",
            getwindowsversion=lambda: SimpleNamespace(build=19045),
        ),
    )
    original_set_attr = win_effects._set_attr

    def set_attr(hwnd, attr, value):
        """只强制圆角回退，标题栏保留成功路径，原生运行时调用真实 DWM。"""
        if attr == 33:
            return False
        return original_set_attr(hwnd, attr, value) if qapp.platformName() == "windows" else True

    monkeypatch.setattr(win_effects, "_set_attr", set_attr)
    if not hasattr(ctypes, "windll"):
        monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=SimpleNamespace()), raising=False)
    user = ctypes.windll.user32
    original = getattr(user, "SetWindowCompositionAttribute", None)
    if original is not None:
        original.argtypes = [wintypes.HWND, ctypes.POINTER(win_effects._WINCOMPATTRDATA)]
        original.restype = ctypes.c_int

    def apply(hwnd, pointer):
        data = ctypes.cast(pointer, ctypes.POINTER(win_effects._WINCOMPATTRDATA)).contents
        policy = ctypes.cast(data.Data, ctypes.POINTER(win_effects._ACCENT_POLICY)).contents
        calls.append((policy.AccentState, policy.AccentFlags, policy.GradientColor))
        if original is not None and qapp.platformName() == "windows":
            assert original(hwnd, pointer), "The real Windows compositor must accept the policy"
        return 1

    monkeypatch.setattr(user, "SetWindowCompositionAttribute", apply, raising=False)
    return calls


def shown_dialog(qtbot):
    dialog = QDialog()
    qtbot.addWidget(dialog)
    dialog.setGeometry(100, 100, 520, 360)
    acrylic.apply_dialog_acrylic(dialog, tint_alpha=0.86, animate=False)
    dialog.show()
    qtbot.wait(10)
    return dialog


def drag_event(handle, kind, position, button, buttons):
    event = QMouseEvent(
        kind,
        QPointF(handle.mapFromGlobal(position)),
        QPointF(position),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(handle, event)


def start_drag(dialog):
    handle = next(h for h in dialog.findChildren(ResizeHandle) if h.edges == Qt.Edge.RightEdge | Qt.Edge.BottomEdge)
    start = handle.mapToGlobal(handle.rect().center())
    drag_event(handle, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    return handle, start


def finish_drag(handle, start):
    drag_event(
        handle,
        QEvent.Type.MouseButtonRelease,
        start + QPoint(35, 20),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
    )


def test_corner_drag_keeps_blur_without_rebuilding_or_recoloring_window(qtbot, policies):
    dialog = shown_dialog(qtbot)
    assert policies[-1][:2] == (3, 0)
    hwnd, flags, size = int(dialog.winId()), dialog.windowFlags(), dialog.size()
    image = dialog.grab().toImage()
    dpr = dialog.devicePixelRatioF()
    edge_pixel = image.pixelColor(round(20 * dpr), round(2 * dpr))
    policies.clear()
    handle, start = start_drag(dialog)
    assert policies == [], "拖动沿用已配置的实时模糊，不重复设置系统材质"
    assert int(dialog.winId()) == hwnd and dialog.windowFlags() == flags
    assert dialog.grab().toImage().pixelColor(round(20 * dpr), round(2 * dpr)) == edge_pixel
    for delta in (QPoint(12, 8), QPoint(35, 20)):
        drag_event(handle, QEvent.Type.MouseMove, start + delta, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    qtbot.wait(20)
    assert policies == []
    finish_drag(handle, start)
    assert policies == []
    assert dialog.size().width() == size.width() + 35
    assert dialog.size().height() == size.height() + 20
    assert int(dialog.winId()) == hwnd and dialog.windowFlags() == flags


@pytest.mark.parametrize("action", ["hide", "theme"])
def test_rounded_material_survives_theme_change_and_reopen(qtbot, policies, action):
    dialog = shown_dialog(qtbot)
    assert policies[-1][:2] == (3, 0)
    policies.clear()
    handle, start = start_drag(dialog)
    assert policies == []
    if action == "hide":
        dialog.hide()
        dialog.show()
        assert dialog.isVisible()
        assert not dialog.mask().isEmpty()
        assert policies == []
    else:
        previous = theme.current_theme()
        try:
            theme.set_theme("light" if previous == "dark" else "dark")
            QApplication.sendEvent(dialog, QEvent(QEvent.Type.StyleChange))
            assert policies[-1][:2] == (3, 0)
            applied = len(policies)
            finish_drag(handle, start)
            assert len(policies) == applied
            expected = 0x30202020 if theme.current_theme() == "dark" else 0x30F3F3F3
            assert policies[-1][2] == expected
        finally:
            theme.set_theme(previous)


def test_native_move_preserves_blur_without_a_qt_mouse_release(qtbot, policies):
    if QApplication.platformName() != "windows":
        pytest.skip("Requires native messages; run on a private desktop")
    dialog = shown_dialog(qtbot)
    assert policies[-1][:2] == (3, 0)
    policies.clear()
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.SendMessageW.restype = ctypes.c_ssize_t
    user.SendMessageW(int(dialog.winId()), 0x0231, 0, 0)  # WM_ENTERSIZEMOVE
    assert policies == []
    user.SendMessageW(int(dialog.winId()), 0x0232, 0, 0)  # WM_EXITSIZEMOVE
    assert policies == []


def test_opaque_update_dialog_keeps_its_existing_surface(qtbot, policies):
    from src.ui.dialogs.mod_updates_dialog import ModUpdatesDialog

    dialog = ModUpdatesDialog([])
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.wait(10)
    policies.clear()
    flags = dialog.windowFlags()
    handle, start = start_drag(dialog)
    finish_drag(handle, start)
    assert policies == []
    assert dialog.property("opaqueDialogSurface")
    assert not dialog.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert dialog.windowFlags() == flags


@pytest.mark.parametrize(
    "build,corner_radius,expected_state",
    [(19045, None, 3), (19045, 8, 3), (22631, None, 4), (22631, 8, 3)],
)
def test_win10_blur_does_not_depend_on_software_corners(
    qtbot, policies, monkeypatch, build, corner_radius, expected_state
):
    """Win10 即使没有软件圆角也须避开完整 Acrylic，Win11 保持既有材质选择。"""
    monkeypatch.setattr(win_effects.sys, "getwindowsversion", lambda: SimpleNamespace(build=build))
    dialog = QDialog(None, Qt.WindowType.FramelessWindowHint)
    qtbot.addWidget(dialog)
    dialog.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    if corner_radius is not None:
        dialog.setProperty("_window_corner_radius", corner_radius)

    assert win_effects.apply_acrylic_blurbehind(dialog)
    assert policies[-1][0] == expected_state


@pytest.mark.parametrize("build", [22000, 22631, 26100])
def test_unaffected_windows_keep_full_acrylic_during_drag(qtbot, policies, monkeypatch, build):
    monkeypatch.setattr(win_effects.sys, "getwindowsversion", lambda: SimpleNamespace(build=build))
    monkeypatch.setattr(win_effects, "_set_attr", lambda *_: True)
    dialog = shown_dialog(qtbot)
    assert policies[-1][0] == 4
    policies.clear()
    handle, start = start_drag(dialog)
    finish_drag(handle, start)
    assert policies == []
