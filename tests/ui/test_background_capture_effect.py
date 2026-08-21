import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from src.ui.widgets.background_overlay import BackgroundOverlay


class Panel(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.color = QColor(255, 0, 0, 128)
        self.paints = 0
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

    def paintEvent(self, event):
        self.paints += 1
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.color)


def test_foreground_is_painted_once_and_published_with_its_alpha(qtbot):
    from src.ui.backgrounds.foreground import ForegroundCaptureEffect

    root = QWidget()
    qtbot.addWidget(root)
    root.resize(160, 100)
    panel = Panel(root)
    panel.setGeometry(10, 20, 40, 30)
    frames = []
    effect = ForegroundCaptureEffect(root)
    effect.frame_ready.connect(frames.append)
    root.setGraphicsEffect(effect)
    root.show()
    qtbot.waitUntil(lambda: bool(frames))
    ratio = root.devicePixelRatioF()
    assert frames[-1].pixelColor(round(20 * ratio), round(30 * ratio)).getRgb() == (255, 0, 0, 128)
    assert frames[-1].pixelColor(round(120 * ratio), round(70 * ratio)).alpha() == 0

    frames.clear()
    panel.paints = 0
    panel.color = QColor(0, 0, 255, 128)
    for _ in range(10):
        panel.update()
    qtbot.waitUntil(lambda: bool(frames))
    assert panel.paints == len(frames), "the compositor must not paint the form a second time"
    assert frames[-1].pixelColor(round(20 * ratio), round(30 * ratio)).getRgb() == (0, 0, 255, 128)

    frames.clear()
    panel.move(80, 20)
    qtbot.waitUntil(lambda: bool(frames))
    assert frames[-1].pixelColor(round(20 * ratio), round(30 * ratio)).alpha() == 0
    assert frames[-1].pixelColor(round(90 * ratio), round(30 * ratio)).getRgb() == (0, 0, 255, 128)
    frames.clear()
    panel.hide()
    qtbot.waitUntil(lambda: bool(frames))
    assert frames[-1].pixelColor(round(90 * ratio), round(30 * ratio)).alpha() == 0


@pytest.mark.parametrize("native_root", [False, True])
def test_capture_effect_matches_transparent_scroll_edge_and_hover_pixels(qtbot, native_root):
    from PySide6.QtWidgets import QScrollArea

    from src.ui.backgrounds.foreground import ForegroundCaptureEffect, capture_foreground
    from src.ui.widgets.scroll_edge_fade import _CardEdgeFadeEffect

    window = QWidget()
    qtbot.addWidget(window)
    window.resize(390, 330)
    root = QWidget(window)
    root.resize(320, 240)
    root.move(30, 40)
    if native_root:
        root.winId()
    overlay = BackgroundOverlay(root)
    overlay.setGeometry(root.rect())
    area = QScrollArea(root)
    area.setGeometry(30, 20, 240, 180)
    area.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: 0; }")
    content = QWidget()
    content.resize(200, 600)
    area.setWidget(content)
    panel = Panel(content)
    panel.setGeometry(20, 40, 100, 120)
    panel.setGraphicsEffect(_CardEdgeFadeEffect(panel, area, 32, lambda _: "panel"))
    window.show()
    area.verticalScrollBar().setValue(70)
    panel.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
    qtbot.wait(20)
    expected = capture_foreground(overlay)

    frames = []
    effect = ForegroundCaptureEffect(root)
    effect.frame_ready.connect(frames.append)
    root.setGraphicsEffect(effect)
    root.update()
    qtbot.waitUntil(lambda: bool(frames))
    assert frames[-1] == expected
    position = area.viewport().mapTo(root, QPoint(30, 1))
    ratio = root.devicePixelRatioF()
    assert frames[-1].pixelColor(round(position.x() * ratio), round(position.y() * ratio)).alpha() < 128

    root.resize(400, 280)
    qtbot.waitUntil(lambda: frames[-1].width() == round(400 * ratio))
    assert frames[-1].devicePixelRatio() == ratio
