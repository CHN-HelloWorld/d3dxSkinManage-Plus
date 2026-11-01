"""验证已有卡片、通知和激活遮罩在换肤后保持正确外观。"""

import pytest
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QWidget

from src.ui import theme
from src.ui.pages.overview import OverviewPage
from src.ui.widgets.toast import Toast


@pytest.fixture
def appearance(qapp):
    previous = theme.current_theme(), theme.is_background_active(), qapp.styleSheet()

    def apply(mode, background=False):
        theme.set_theme(mode)
        theme.set_background_active(background)
        qapp.setStyleSheet(theme.render_qss(mode) + (theme.background_overlay_qss(mode) if background else ""))

    yield apply
    mode, background, sheet = previous
    theme.set_theme(mode)
    theme.set_background_active(background)
    qapp.setStyleSheet(sheet)


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("refresh", ["visible", "recycled"])
def test_category_summary_follows_theme_without_recreating_card(
    qtbot,
    qapp,
    temp_data_dir,
    appearance,
    background,
    refresh,
):
    appearance("dark", background)
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(900, 600)
    page.show()
    record = {"id": "category", "name": "Category", "mod_count": 7, "enabled_count": 3}
    page.set_characters([record])
    qtbot.waitUntil(lambda: "category" in page._vs_card_cache)
    card = page._vs_card_cache["category"]
    card.set_selected(True)
    original_text = card._info_label.text()
    old_ink = card._info_label.palette().color(QPalette.ColorRole.WindowText)

    appearance("light", background)
    if refresh == "visible":
        page.retheme()
        assert page._vs_card_cache["category"] is card
        assert card._selected
    else:
        page._vs_create_card({"type": "category", "id": "category", "data": record}, pooled_card=card)
    qapp.processEvents()

    ink = card._info_label.palette().color(QPalette.ColorRole.WindowText)
    assert ink != old_ink, "Reusing a card must not retain the previous theme's summary color"
    assert ink == QColor(theme.surface_text_color("text.secondary"))
    assert card._info_label.text() == original_text
    page.close()


@pytest.mark.parametrize("start,end", [("light", "dark"), ("dark", "light")])
def test_visible_theme_notification_keeps_clear_corners(qtbot, qapp, appearance, start, end):
    appearance(start)
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.resize(700, 300)
    parent.show()
    toast = Toast(parent)
    toast.busy("Applying theme")
    qapp.processEvents()

    appearance(end)
    qapp.processEvents()
    for completed in (False, True):
        if completed:
            toast.done("Theme applied")
        image = toast.grab().toImage()
        for x, y in ((0, 0), (image.width() - 1, 0), (0, image.height() - 1), (image.width() - 1, image.height() - 1)):
            assert image.pixelColor(x, y).alpha() == 0, "Rounded corners must not retain a square palette fill"
        assert image.pixelColor(image.width() // 2, 4) == QColor(theme.color("bg.card"))
    toast.hide()


@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize(
    "start,end,old_background,new_background",
    [
        ("dark", "light", False, False),
        ("light", "dark", False, False),
        ("dark", "light", True, True),
        ("light", "dark", True, True),
        ("dark", "dark", False, True),
        ("light", "light", True, False),
    ],
)
def test_activation_overlay_matches_current_appearance_without_recreation(
    qtbot, qapp, appearance, hidden, start, end, old_background, new_background
):
    """切换明暗或材质后，已有遮罩应与新建遮罩一致，并保留导航信号。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QFrame, QLabel, QMainWindow, QPushButton, QStackedWidget

    from src.ui.main_window import MainWindow
    from src.ui.widgets.lock_overlay import LockOverlay

    class Window(MainWindow):
        """仅保留页面栈和激活遮罩，复用主窗口的外观刷新入口。"""

        def __init__(self):
            """创建用于换肤验证的空页面和常驻遮罩。"""
            QMainWindow.__init__(self)
            self._stack = QStackedWidget(self)
            self.setCentralWidget(self._stack)
            for _ in range(self.PAGE_SETTINGS + 1):
                self._stack.addWidget(QWidget())
            self._stack.setCurrentIndex(self.PAGE_SETTINGS)
            self._lock_overlay = LockOverlay(self._stack)

        def _refresh_themed_icons(self):
            """省略夹具未创建的导航图标，保留共享图标刷新流程。"""
            pass

        def closeEvent(self, event):
            """标记外观回调失效，并执行 Qt 关闭流程。"""
            self._appearance_disposed = True
            QMainWindow.closeEvent(self, event)

    appearance(start, old_background)
    window = Window()
    qtbot.addWidget(window)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window.resize(900, 650)
    window.show()
    overlay = window._lock_overlay
    overlay.setGeometry(window._stack.rect())
    overlay.setVisible(not hidden)
    original_card = overlay.findChild(QFrame, "lockOverlayCard")
    qapp.processEvents()

    appearance(end, new_background)
    completed = []
    window._post_appearance_refresh(
        "Theme applied",
        on_finished=lambda: completed.append(True),
        colors_changed=start != end,
        refresh_background=False,
    )
    qtbot.waitUntil(lambda: bool(completed))
    overlay.show()
    reference = LockOverlay(window._stack)
    reference.setGeometry(overlay.geometry())
    reference.show()
    qapp.processEvents()

    # 新建控件作为当前外观的参照；已有控件必须在同一刷新流程中达到相同状态。
    reference_card = reference.findChild(QFrame, "lockOverlayCard")
    assert overlay.findChild(QFrame, "lockOverlayCard") is original_card
    assert original_card.palette().color(QPalette.ColorRole.Window) == reference_card.palette().color(
        QPalette.ColorRole.Window
    )
    for actual, expected in zip(overlay.findChildren(QLabel), reference.findChildren(QLabel), strict=True):
        assert actual.palette().color(QPalette.ColorRole.WindowText) == expected.palette().color(
            QPalette.ColorRole.WindowText
        )
        assert actual.pixmap().toImage() == expected.pixmap().toImage()
    reference.hide()
    with qtbot.waitSignal(overlay.go_to_game_requested):
        qtbot.mouseClick(overlay.findChild(QPushButton), Qt.MouseButton.LeftButton)
    window.close()
