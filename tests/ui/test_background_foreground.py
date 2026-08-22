from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget

from src.ui.widgets.background_overlay import BackgroundOverlay


def test_foreground_capture_excludes_video_and_keeps_transparent_controls(qtbot):
    from src.ui.backgrounds.foreground import capture_foreground

    root = QWidget()
    qtbot.addWidget(root)
    root.resize(80, 60)
    overlay = BackgroundOverlay(root)
    overlay.setGeometry(root.rect())
    overlay.configure_dynamic(0, 0, "cover")
    frame = QImage(80, 60, QImage.Format.Format_RGB32)
    frame.fill(QColor("blue"))
    overlay.set_dynamic_frame(frame)
    overlay.set_enabled(True)

    class Tint(QWidget):
        def paintEvent(self, event):
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor(255, 0, 0, 128))

    tint = Tint(root)
    tint.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
    tint.setGeometry(10, 10, 20, 20)
    root.show()
    qtbot.wait(20)
    image = capture_foreground(overlay)
    assert image.pixelColor(60, 40).alpha() == 0, "a baked video frame would freeze beneath the real video"
    assert image.pixelColor(15, 15).getRgb() == (255, 0, 0, 128)
    assert overlay._enabled and not overlay._dynamic_image.isNull()
