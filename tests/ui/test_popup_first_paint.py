from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtWidgets import QApplication, QComboBox, QMenu, QToolTip, QWidget

from src.ui import acrylic, theme, win_effects


class Paints(QObject):
    def __init__(self, app):
        super().__init__()
        self.counts = {}
        self.tooltip_polish_native = []
        self.native_results = []
        app.installEventFilter(self)

    def eventFilter(self, widget, event):
        if event.type() == QEvent.Type.Paint and isinstance(widget, QWidget):
            self.counts[widget] = self.counts.get(widget, 0) + 1
        if (
            event.type() == QEvent.Type.Polish
            and isinstance(widget, QWidget)
            and widget.metaObject().className() == "QTipLabel"
        ):
            self.tooltip_polish_native.append(widget.testAttribute(Qt.WidgetAttribute.WA_WState_Created))
        return False


@pytest.fixture
def popup_ui(qapp, qtbot, monkeypatch, temp_data_dir):
    calls = []
    paints = Paints(qapp)
    monkeypatch.setattr(
        win_effects,
        "sys",
        SimpleNamespace(
            platform="win32",
            getwindowsversion=lambda: SimpleNamespace(build=19045),
        ),
    )
    original_attr = win_effects._set_attr

    def set_attr(hwnd, attr, value):
        """按模拟系统决定圆角支持，其他属性在原生测试中仍调用 Windows。"""
        if attr == win_effects._DWMWA_WINDOW_CORNER_PREFERENCE:
            return win_effects.sys.getwindowsversion().build >= 22000
        return original_attr(hwnd, attr, value) if qapp.platformName() == "windows" else True

    monkeypatch.setattr(win_effects, "_set_attr", set_attr)
    if not hasattr(ctypes, "windll"):
        monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=SimpleNamespace()), raising=False)
    original = getattr(ctypes.windll.user32, "SetWindowCompositionAttribute", None)
    if original is not None:
        from ctypes import wintypes

        original.argtypes = [wintypes.HWND, ctypes.POINTER(win_effects._WINCOMPATTRDATA)]
        original.restype = ctypes.c_int

    def apply(hwnd, pointer):
        popup = QWidget.find(hwnd.value)
        data = ctypes.cast(pointer, ctypes.POINTER(win_effects._WINCOMPATTRDATA)).contents
        policy = ctypes.cast(data.Data, ctypes.POINTER(win_effects._ACCENT_POLICY)).contents
        calls.append((popup, dict(paints.counts), policy.AccentState, policy.GradientColor))
        if original is not None and qapp.platformName() == "windows":
            accepted = bool(original(hwnd, pointer))
            paints.native_results.append(accepted)
            return int(accepted)
        return 1

    monkeypatch.setattr(ctypes.windll.user32, "SetWindowCompositionAttribute", apply, raising=False)
    popup_filter = acrylic.AcrylicPopupFilter()
    paints.popup_filter = popup_filter
    qapp.installEventFilter(popup_filter)
    for effect in (
        Qt.UIEffect.UI_AnimateCombo,
        Qt.UIEffect.UI_AnimateMenu,
        Qt.UIEffect.UI_FadeMenu,
        Qt.UIEffect.UI_AnimateTooltip,
        Qt.UIEffect.UI_FadeTooltip,
    ):
        qapp.setEffectEnabled(effect, False)
    owner = QWidget()
    owner.resize(320, 150)
    qtbot.addWidget(owner)
    owner.show()
    yield owner, calls, paints
    for widget in qapp.topLevelWidgets():
        if widget.metaObject().className() == "QTipLabel":
            shiboken6.delete(widget)
    qapp.removeEventFilter(popup_filter)
    qapp.removeEventFilter(paints)
    assert all(paints.native_results), "The native compositor must accept popup acrylic"


def show_popup(kind, owner):
    if kind == "tooltip":
        QToolTip.showText(QPoint(30, 30), "First tooltip content", owner, msecShowTime=120000)
        popup = next(
            widget for widget in QApplication.topLevelWidgets() if widget.metaObject().className() == "QTipLabel"
        )
        return popup, popup
    if kind == "menu":
        popup = QMenu(owner)
        popup.addAction("First menu action")
        popup.popup(QPoint(30, 30))
        return popup, popup
    combo = QComboBox(owner)
    combo.setEditable(kind == "editable")
    combo.addItems(["First option", "Second option"])
    combo.show()
    combo.showPopup()
    return combo.view().window(), combo.view().viewport()


@pytest.mark.parametrize("kind", ["tooltip", "menu"])
def test_win10_blur_follows_content_paint_and_repaints_same_window(popup_ui, qtbot, kind):
    owner, calls, paints = popup_ui
    popup, content = show_popup(kind, owner)
    hwnd, flags = int(popup.winId()), popup.windowFlags()
    qtbot.waitUntil(lambda: any(row[0] is popup for row in calls))
    applied = [row for row in calls if row[0] is popup]
    assert len(applied) == 1
    _, before, policy, _ = applied[0]
    assert before.get(popup, 0) > 0, "Populate the layered surface before enabling Win10 acrylic"
    assert before.get(content, 0) > 0, "The first upload must include popup text/items"
    assert policy == 3, "窗口区域裁切使用实时模糊，避免亚克力层在圆角之外填充底色"
    qtbot.waitUntil(lambda: paints.counts.get(popup, 0) > before[popup])
    qtbot.waitUntil(lambda: paints.counts.get(content, 0) > before[content])
    assert int(popup.winId()) == hwnd and popup.windowFlags() == flags
    popup.hide()


@pytest.mark.parametrize("kind", ["combo", "editable"])
def test_win10_dropdown_first_paint_contains_its_backdrop_and_keeps_selection(popup_ui, qapp, qtbot, kind):
    """首次同步绘制就应包含完整背景，不能先显示预乘叠色或等原生回调再补齐。"""
    owner, _, _ = popup_ui
    previous_theme = theme.current_theme()
    theme.set_theme("light")
    owner.setObjectName("firstPaintOwner")
    owner.setStyleSheet("QWidget#firstPaintOwner { background: #2070c0; }")
    owner.resize(440, 360)
    try:
        combo = QComboBox(owner)
        combo.setEditable(kind == "editable")
        combo.addItems([f"Option {index}" for index in range(8)])
        combo.setGeometry(50, 50, 240, 32)
        combo.show()
        qtbot.mouseMove(owner, QPoint(owner.width() - 5, owner.height() - 5))
        combo.showPopup()
        popup = combo.view().window()
        hwnd = int(popup.winId())
        image = popup.grab().toImage()
        ratio = image.devicePixelRatio()
        pixel = image.pixelColor(round((popup.width() - 16) * ratio), round((popup.height() - 12) * ratio))
        assert pixel.alpha() == 255, "首帧磨砂必须包含下层背景，不能只绘制半透明叠色"
        # 采样点远离收起态控件与父窗边缘，白色 84% 加背景 #2070c0 的 16%。
        assert all(abs(actual - expected) <= 2 for actual, expected in zip(pixel.getRgb()[:3], (242, 246, 251))), pixel
        qapp.processEvents()
        assert int(popup.winId()) == hwnd
        combo = popup.parentWidget()
        combo.view().setCurrentIndex(combo.model().index(1, 0))
        qtbot.keyClick(combo.view(), Qt.Key.Key_Return)
        assert combo.currentIndex() == 1 and not popup.isVisible()
    finally:
        theme.set_theme(previous_theme)


def test_hidden_popup_does_not_enable_queued_acrylic(popup_ui, qapp):
    owner, calls, _ = popup_ui
    popup, _ = show_popup("menu", owner)
    # 模拟绘制完成但排队原生效果尚未应用时关闭窗口。
    popup.grab()
    popup.hide()
    qapp.processEvents()
    assert not [row for row in calls if row[0] is popup]


def test_tooltip_polish_does_not_allocate_native_window(popup_ui):
    owner, _, paints = popup_ui
    show_popup("tooltip", owner)
    assert paints.tooltip_polish_native and not any(paints.tooltip_polish_native)


def test_reopening_before_callback_uses_only_current_show(popup_ui, qapp, qtbot):
    owner, calls, _ = popup_ui
    popup, _ = show_popup("menu", owner)
    popup.grab()
    popup.hide()
    popup.show()
    popup.grab()
    qtbot.waitUntil(lambda: any(row[0] is popup for row in calls))
    qapp.processEvents()
    assert len([row for row in calls if row[0] is popup]) == 1


def test_deleted_popup_cancels_pending_native_effect(popup_ui, qapp):
    owner, calls, _ = popup_ui
    popup, _ = show_popup("menu", owner)
    popup.grab()
    shiboken6.delete(popup)
    qapp.processEvents()
    assert not calls


@pytest.mark.parametrize("build", [22000, 26100])
@pytest.mark.parametrize("kind", ["combo", "tooltip", "menu"])
def test_win11_keeps_acrylic_at_show(popup_ui, qapp, monkeypatch, build, kind):
    owner, calls, _ = popup_ui
    monkeypatch.setattr(win_effects.sys, "getwindowsversion", lambda: SimpleNamespace(build=build))
    popup, _ = show_popup(kind, owner)
    applied = [row for row in calls if row[0] is popup]
    assert applied and all(row[1].get(popup, 0) == 0 and row[2] == 4 for row in applied)
    count = len(applied)
    qapp.processEvents()
    assert len([row for row in calls if row[0] is popup]) == count


def test_theme_change_before_first_callback_uses_latest_tooltip_tint(popup_ui, qtbot):
    owner, calls, _ = popup_ui
    previous = theme.current_theme()
    try:
        theme.set_theme("light")
        popup, _ = show_popup("tooltip", owner)
        hwnd = int(popup.winId())
        popup.grab()
        theme.set_theme("dark")
        acrylic.reset_for_theme_change()
        popup.grab()
        qtbot.waitUntil(lambda: any(row[0] is popup for row in calls))
        applied = [row for row in calls if row[0] is popup]
        assert len(applied) == 1 and applied[0][3] == 0x30202020
        assert int(popup.winId()) == hwnd
    finally:
        theme.set_theme(previous)


def test_startup_group_routes_first_paint_to_acrylic(popup_ui, qapp, qtbot, monkeypatch):
    from src.ui.application_event_filters import group_application_event_filters, remove_application_event_filter

    owner, calls, paints = popup_ui
    qapp.removeEventFilter(paints.popup_filter)
    monkeypatch.setattr(acrylic, "_filter_singleton", None)
    with group_application_event_filters(qapp):
        acrylic.install(qapp)
    try:
        popup, _ = show_popup("menu", owner)
        qtbot.waitUntil(lambda: any(row[0] is popup for row in calls))
        assert all(row[1].get(popup, 0) > 0 for row in calls if row[0] is popup)
    finally:
        remove_application_event_filter(qapp, acrylic._filter_singleton)
