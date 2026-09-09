import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QMenu, QScrollArea, QWidget

from src.ui import acrylic, theme
from src.ui.pages.overview import CharacterCard
from src.ui.widgets.background_overlay import BackgroundOverlay
from src.ui.widgets.scroll_edge_fade import ScrollEdgeFade


def test_card_menu_keeps_backdrop_fixed_during_scroll_before_repaint(qtbot, qapp):
    if qapp.platformName() != "windows":
        pytest.skip("Requires the native Windows scroll blit and screen capture")
    old_sheet = qapp.styleSheet()
    popup_filter = acrylic.AcrylicPopupFilter()
    qapp.installEventFilter(popup_filter)
    host = QWidget()
    qtbot.addWidget(host)
    host.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
    host.resize(400, 400)
    try:
        qapp.setStyleSheet(theme.render_qss("light") + theme.background_overlay_qss("light"))
        background = BackgroundOverlay(host)
        background.setGeometry(host.rect())
        background.configure_dynamic(0, 0, "cover")
        background.set_enabled(True)
        image = QImage(400, 400, QImage.Format.Format_RGB32)
        painter = QPainter(image)
        for y in range(400):
            painter.fillRect(0, y, 400, 1, QColor(y % 255, 80, 200))
        painter.end()
        background.set_dynamic_frame(image)
        area = QScrollArea(host)
        area.setGeometry(20, 20, 320, 340)
        area.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        content = QWidget()
        content.resize(300, 1500)
        area.setWidget(content)
        card = CharacterCard({"id": "category", "name": "Category"}, content)
        card.move(20, 300)
        ScrollEdgeFade(area, lambda: [card], cache_key_provider=lambda c: c.scroll_render_key())
        host.show()
        area.verticalScrollBar().setValue(280)
        qtbot.wait(60)
        menu = QMenu(card)
        menu.addAction("Action")
        menu.popup(host.mapToGlobal(QPoint(30, 30)))
        qtbot.waitUntil(menu.isVisible)
        menu.hide()
        qtbot.wait(60)

        def fixed_pixel():
            # 截图或等待都会先重绘并掩盖问题，须直接检查已显示帧。
            shot = host.screen().grabWindow(int(host.winId())).toImage()
            ratio = host.devicePixelRatioF()
            return shot.pixelColor(round(300 * ratio), round(150 * ratio))

        expected = QColor(150, 80, 200)
        assert fixed_pixel() == expected
        for value in (290, 330, 380):
            area.verticalScrollBar().setValue(value)
            assert fixed_pixel() == expected, "scroll blit moved the already-composited backdrop"
            qtbot.wait(40)
    finally:
        host.hide()
        qapp.removeEventFilter(popup_filter)
        qapp.setStyleSheet(old_sheet)
