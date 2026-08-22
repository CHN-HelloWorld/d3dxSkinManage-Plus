import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QListWidget, QPushButton, QStyle, QStyleOptionButton, QStyleOptionViewItem

from src.ui import theme
from src.ui.main_window import _NavItemDelegate


def _paint_surface(widget, state):
    image = QImage(180, 48, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    if isinstance(widget, QListWidget):
        option = QStyleOptionViewItem()
        option.initFrom(widget)
        option.widget = widget
        option.rect = QRect(0, 0, 180, 48)
        option.state = QStyle.StateFlag.State_Enabled | state
        widget.itemDelegate().paint(painter, option, widget.model().index(0, 0))
    else:
        option = QStyleOptionButton()
        option.initFrom(widget)
        option.rect = QRect(0, 0, 180, 48)
        option.state = QStyle.StateFlag.State_Enabled | state
        widget.style().drawControl(QStyle.ControlElement.CE_PushButton, option, painter, widget)
    painter.end()
    return image.pixelColor(90, 24)


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("collapsed", [False, True])
@pytest.mark.parametrize("name", ["navList", "navLaunchBtn", "navCollapseBtn"])
def test_navigation_hover_transmits_background_and_preserves_selection(qtbot, mode, collapsed, name):
    widget = QListWidget() if name == "navList" else QPushButton()
    qtbot.addWidget(widget)
    widget.setObjectName(name)
    widget.setProperty("collapsed", collapsed)
    if isinstance(widget, QListWidget):
        widget.addItem("")
        delegate = _NavItemDelegate(widget)
        delegate.collapsed = collapsed
        widget.setItemDelegate(delegate)
    hover = QStyle.StateFlag.State_MouseOver
    for background in (False, True, False):
        widget.setProperty("_backgroundSurface", background)
        widget.setStyleSheet(theme.appearance_qss(mode))
        widget.ensurePolished()
        painted = _paint_surface(widget, hover)
        if background:
            assert 100 < painted.alpha() < 220, painted.getRgb()
        else:
            assert painted == QColor(theme.color("bg.hover", mode))
        if name == "navCollapseBtn":
            continue
        if isinstance(widget, QListWidget):
            selected = _paint_surface(widget, hover | QStyle.StateFlag.State_Selected)
        else:
            widget.setProperty("current", True)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            selected = _paint_surface(widget, hover)
            widget.setProperty("current", False)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        if background:
            assert 0 < selected.alpha() < 240, selected.getRgb()
            assert selected != painted
        else:
            assert selected == QColor(theme.color("accent.subtle", mode))


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("name", ["navLaunchBtn", "navCollapseBtn"])
def test_navigation_pressed_surface_stays_translucent(qtbot, mode, name):
    widget = QPushButton()
    qtbot.addWidget(widget)
    widget.setObjectName(name)
    widget.setProperty("_backgroundSurface", True)
    widget.setStyleSheet(theme.appearance_qss(mode))
    widget.ensurePolished()
    pressed = _paint_surface(widget, QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_Sunken)
    assert 0 < pressed.alpha() < 220, pressed.getRgb()


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_settings_navigation_and_description_follow_material(qtbot, temp_data_dir, mode):
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QLabel

    from src.ui.pages.settings import SettingsPage

    old_theme, old_background = theme.current_theme(), theme.is_background_active()
    theme.set_theme(mode)
    try:
        page = SettingsPage()
        qtbot.addWidget(page)
        button = page._settings_nav_btns[1]
        footer = next(label for label in page._settings_footer.findChildren(QLabel) if "自动保存" in label.text())
        for background in (False, True, False):
            theme.set_background_active(background)
            page.retheme()
            button.ensurePolished()
            for state in (QStyle.StateFlag.State_MouseOver, QStyle.StateFlag.State_On):
                painted = _paint_surface(button, state)
                # 交互着色叠加在共享设置材质上。
                assert 0 < painted.alpha() < 240
            footer.ensurePolished()
            ink = footer.palette().color(QPalette.ColorRole.WindowText)
            # 次级文本跟随材质的可读性调整。
            assert ink == QColor(theme.surface_text_color("settings.muted", mode))
    finally:
        theme.set_theme(old_theme)
        theme.set_background_active(old_background)
