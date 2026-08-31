from __future__ import annotations

import pytest
import shiboken6
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QToolTip, QWidget

from src.ui import acrylic, icons, theme
from src.ui.background_style import install_background_style


@pytest.fixture
def tooltip_ui(qapp, qtbot, monkeypatch):
    old_sheet, old_theme = qapp.styleSheet(), theme.current_theme()
    old_background = theme.is_background_active()
    old_surfaces = getattr(qapp, "_background_style", None)
    surfaces = install_background_style(qapp)
    old_enabled = surfaces.enabled
    monkeypatch.setattr(icons, "write_themed_svg", lambda *args, **kwargs: None)
    popup_filter = acrylic.AcrylicPopupFilter()
    qapp.installEventFilter(popup_filter)
    owner = QWidget()
    owner.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    # 真实设置页和行容器使用该无作用域内联样式。
    owner.setStyleSheet("background: transparent; color: #ff0000;")
    owner.resize(240, 100)
    qtbot.addWidget(owner)
    owner.show()

    def apply(mode, background):
        theme.set_theme(mode)
        theme.set_background_active(background)
        qapp.setStyleSheet(theme.appearance_qss(mode))
        surfaces.apply(background)
        acrylic.reset_for_theme_change()

    yield owner, apply
    for widget in qapp.topLevelWidgets():
        if widget.metaObject().className() == "QTipLabel":
            shiboken6.delete(widget)
    qapp.removeEventFilter(popup_filter)
    surfaces.apply(old_enabled)
    if old_surfaces is None:
        surfaces.dispose()
    theme.set_theme(old_theme)
    theme.set_background_active(old_background)
    qapp.setStyleSheet(old_sheet)


def _assert_tooltip(qapp, mode):
    qapp.processEvents()
    tooltip = next(widget for widget in qapp.topLevelWidgets() if widget.metaObject().className() == "QTipLabel")
    image = tooltip.grab().toImage()
    ratio = tooltip.devicePixelRatioF()
    sample = image.pixelColor(round(5 * ratio), round(5 * ratio))
    assert 230 <= sample.alpha() <= 240
    expected = QColor(theme.color("bg.card", mode))
    assert all(abs(actual - target) <= 1 for actual, target in zip(sample.getRgb()[:3], expected.getRgb()[:3]))
    assert tooltip.palette().color(QPalette.ColorGroup.Inactive, QPalette.ColorRole.ToolTipText) == QColor(
        theme.color("text.primary", mode)
    )
    assert tooltip.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    return tooltip


@pytest.mark.parametrize("background", [False, True])
def test_native_tooltip_is_readable_on_transparent_page_and_theme_switch(qapp, tooltip_ui, background):
    owner, apply = tooltip_ui
    apply("dark", background)
    QToolTip.showText(QPoint(20, 20), "Tooltip with readable text", owner, msecShowTime=120000)
    tooltip = _assert_tooltip(qapp, "dark")
    for mode in ("light", "dark", "light"):
        apply(mode, background)
        assert _assert_tooltip(qapp, mode) is tooltip


def test_tooltip_remains_readable_when_shown_for_another_control(qapp, qtbot, tooltip_ui):
    owner, apply = tooltip_ui
    apply("light", True)
    QToolTip.showText(QPoint(20, 20), "First control", owner, msecShowTime=120000)
    _assert_tooltip(qapp, "light")
    other = QWidget(owner)
    other.setStyleSheet("background: transparent; color: #0000ff;")
    other.show()
    QToolTip.showText(QPoint(80, 20), "Another control", other, msecShowTime=120000)
    # Qt 可能在提示来源变化时替换提示窗口，两种生命周期路径都须显示当前文本和可读颜色。
    assert _assert_tooltip(qapp, "light").text() == "Another control"
