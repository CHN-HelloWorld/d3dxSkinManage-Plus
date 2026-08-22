import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QPixmapCache
from PySide6.QtWidgets import QScrollArea, QWidget

from src.ui.backgrounds.foreground import capture_foreground
from src.ui.backgrounds.render_worker import ForegroundLayer
from src.ui.widgets.background_overlay import BackgroundOverlay
from src.ui.widgets.scroll_edge_fade import _CardEdgeFadeEffect


class _ColorCard(QWidget):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("red"))


@pytest.fixture
def background_theme(qapp, monkeypatch):
    from src.ui import theme

    previous_sheet = qapp.styleSheet()
    monkeypatch.setattr(theme, "_current_theme", "light")
    qapp.setStyleSheet(theme.render_qss("light") + theme.background_overlay_qss("light"))
    yield
    qapp.setStyleSheet(previous_sheet)


@pytest.mark.parametrize("native_root", [False, True])
@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("kind", ["plain", "mod", "category"])
def test_capture_preserves_hovered_card_position(qtbot, background_theme, native_root, live, kind):
    window = QWidget()
    qtbot.addWidget(window)
    window.resize(700, 520)
    root = QWidget(window)
    root.setGeometry(30, 40, 640, 450)
    if native_root:
        root.winId()
    overlay = BackgroundOverlay(root)
    overlay.setGeometry(root.rect())
    area = QScrollArea(root)
    area.setGeometry(50, 20, 400, 400)
    content = QWidget()
    content.resize(360, 1000)
    area.setWidget(content)
    if kind == "mod":
        from src.models.mod import Mod
        from src.ui.widgets.mod_card import ModCard

        card = ModCard(Mod(mod_id="capture", name="Capture"), parent=content)
    elif kind == "category":
        from src.ui.pages.overview import CharacterCard

        card = CharacterCard(dict(id="capture", name="Capture"), content)
    else:
        card = _ColorCard(content)
        card.resize(80, 100)
    if kind != "plain":
        preview = QPixmap(200, 180)
        preview.fill(QColor("red"))
        card.set_preview_pixmap(preview)
    card.move(40, 60)
    effect = _CardEdgeFadeEffect(card, area, 40, lambda widget: "red")
    card.setGraphicsEffect(effect)
    window.show()
    area.verticalScrollBar().setValue(20)
    qtbot.wait(20)
    # 先建立未悬停路径的缓存，悬停时会绕过卡片缓存。
    card.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
    capture_foreground(overlay)
    # 使 Qt 内部源图像缓存失效，验证绕过卡片缓存后真实控件绘制路径仍有效。
    QPixmapCache.clear()
    card.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, live)
    image = capture_foreground(overlay)
    position = card.mapTo(root, QPoint())
    ratio = image.devicePixelRatio()
    x = round((position.x() + 30) * ratio)
    y = round((position.y() + 60) * ratio)
    assert image.pixelColor(x, y) == QColor("red"), (
        f"card missing at layout position {position}; native={native_root}, live={live}"
    )
    x, y = round((position.x() + card.width() + 10) * ratio), round((position.y() + 10) * ratio)
    assert image.pixelColor(x, y) != QColor("red"), "card pixels escaped their layout rectangle"


def test_foreground_keeps_pixel_positions_until_matching_resize_frame(qtbot):
    host = QWidget()
    qtbot.addWidget(host)
    host.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    host.resize(400, 300)
    layer = ForegroundLayer(host)
    layer.setGeometry(host.rect())
    image = QImage(200, 150, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.fillRect(30, 40, 20, 20, QColor("red"))
    painter.end()
    layer.image = image
    host.show()
    captured = layer.grab().toImage()
    ratio = captured.devicePixelRatio()
    assert captured.pixelColor(round(35 * ratio), round(45 * ratio)) == QColor("red"), (
        "a previous-size foreground must not be stretched while the next image is in flight"
    )
    assert captured.pixelColor(round(70 * ratio), round(90 * ratio)) != QColor("red")


@pytest.mark.parametrize("suspended", [False, True])
def test_resize_configuration_waits_for_its_complete_foreground(qtbot, monkeypatch, background_theme, suspended):
    from src.ui.backgrounds import render_worker
    from src.ui.backgrounds.process_protocol import PacketReader, encode_image, encode_json

    # 仅替换平台窗口句柄边界，保留画布、控制器和流读取，包括分片图像传输。
    monkeypatch.setattr(render_worker, "attach_child", lambda *args: None)
    monkeypatch.setattr(render_worker, "position_child", lambda *args: None)
    canvas = render_worker.VideoCanvas(0)
    qtbot.addWidget(canvas)

    class Socket:
        data = b""

        def readAll(self):
            data, self.data = self.data, b""
            return data

        def write(self, packet):
            return len(packet)

    session = render_worker.RenderSession.__new__(render_worker.RenderSession)
    session.canvas = canvas
    session.reader = PacketReader()
    session.socket = Socket()
    configuration = dict(
        parent=1,
        rect=[0, 0, 200, 150],
        size=[200, 150],
        theme="light",
        options=dict(source="off"),
        suspended=False,
        paused=False,
    )
    canvas.configure(configuration)
    original = QImage(200, 150, QImage.Format.Format_RGB32)
    original.fill(QColor("red"))
    canvas.set_foreground(original)
    replacement = QImage(400, 300, QImage.Format.Format_RGB32)
    replacement.fill(QColor("blue"))
    packet = encode_image(replacement)
    session.socket.data = (
        encode_json(
            configuration | dict(action="configure", size=[400, 300], rect=[0, 0, 400, 300], suspended=suspended)
        )
        + packet[:100]
    )
    session.read()
    assert canvas.size() == original.size(), "new geometry became visible before its image arrived"
    assert canvas.foreground.image.pixelColor(0, 0) == QColor("red")
    assert canvas._suspended is suspended, "minimization must not wait for another screenshot"
    assert canvas.controller._suspended is suspended
    session.socket.data = packet[100:]
    session.read()
    assert canvas.size() == replacement.size()
    assert canvas.foreground.image.pixelColor(0, 0) == QColor("blue")
    canvas.dispose()
