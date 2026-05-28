import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QScrollArea, QWidget

from src.ui.widgets import deploy_overlay
from src.ui.widgets.scroll_edge_fade import ScrollEdgeFade


@pytest.fixture
def overlay_image(qtbot, monkeypatch):
    host = QWidget()
    qtbot.addWidget(host)
    overlay = deploy_overlay.DeployOverlay(host)
    overlay.resize(180, 140)
    image = QImage(180, 140, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painters = []

    class ImagePainter(QPainter):
        def __init__(self, widget):
            # 保持真实画笔存活，避免断言异常导致 Qt 销毁正在使用的绘图设备。
            super().__init__(image)
            painters.append(self)

    monkeypatch.setattr(deploy_overlay, "QPainter", ImagePainter)
    monkeypatch.setattr(deploy_overlay.theme, "color", lambda _token: "#0088ff")
    yield overlay, image, painters
    for painter in painters:
        if painter.isActive():
            painter.end()


@pytest.mark.parametrize("progress", [0.0, 0.5, 1.0])
def test_progress_paint_finishes_with_a_visible_ring_and_label(overlay_image, progress):
    overlay, image, painters = overlay_image
    overlay.set_progress(progress)

    overlay.paintEvent(None)

    assert not any(painter.isActive() for painter in painters)
    assert image.pixelColor(4, 4).getRgb() == (0, 0, 0, 130)
    if progress:
        assert image.pixelColor(112, 70) == QColor("#0088ff")
    label_pixels = [image.pixelColor(x, y) for x in range(74, 107) for y in range(62, 79)]
    assert any(pixel.red() > 200 and pixel.green() > 200 for pixel in label_pixels)


def test_paint_error_releases_the_device_before_propagating(overlay_image, monkeypatch):
    overlay, _image, painters = overlay_image

    def fail_color(_token):
        raise RuntimeError("theme lookup failed")

    monkeypatch.setattr(deploy_overlay.theme, "color", fail_color)
    with pytest.raises(RuntimeError, match="theme lookup failed"):
        overlay.paintEvent(None)

    assert not any(painter.isActive() for painter in painters)


def test_progress_repaints_and_closes_inside_scroll_edge_effect(qtbot, monkeypatch):
    monkeypatch.setattr(deploy_overlay.theme, "color", lambda _token: "#0088ff")
    area = QScrollArea()
    qtbot.addWidget(area)
    area.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    area.resize(260, 250)
    content = QWidget()
    content.resize(230, 1000)
    card = QWidget(content)
    card.setGeometry(10, 80, 200, 200)
    card.setStyleSheet("background: #cc2200;")
    card.show()
    area.setWidget(content)
    fade = ScrollEdgeFade(area, lambda: [card], fade_height=60)
    overlay = deploy_overlay.DeployOverlay(card)
    area.show()
    area.verticalScrollBar().setValue(100)
    fade.refresh()

    for _ in range(3):
        overlay.cover()
        for progress in (0.0, 0.5, 1.0):
            overlay.set_progress(progress)
            image = area.viewport().grab().toImage()
            if progress:
                assert image.pixelColor(132, 80) == QColor("#0088ff")
        overlay.hide()
        image = area.viewport().grab().toImage()
        assert image.pixelColor(132, 80) == QColor("#cc2200")
