"""捕获前景控件，不合入独立播放的视频。"""

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QImage, QRegion
from PySide6.QtWidgets import QGraphicsEffect, QWidget


class ForegroundCaptureEffect(QGraphicsEffect):
    """发布正常控件绘制使用的透明像素。

    复用图形效果提供的源图，其中已包含子控件、边缘淡化和悬停状态，避免再次绘制整张页面。
    """

    frame_ready = Signal(QImage)

    def draw(self, painter):
        # 局部源图避免嵌套图形效果共享绘制器时的偏移冲突。
        """绘制 Qt 提供的前景源图，并仅在源图有效时发布图像。"""
        offset = QPoint()
        pixmap = self.sourcePixmap(
            Qt.CoordinateSystem.LogicalCoordinates,
            offset,
            QGraphicsEffect.PixmapPadMode.NoPad,
        )
        if not pixmap.isNull():
            painter.drawPixmap(offset, pixmap)
            self.frame_ready.emit(pixmap.toImage())


def capture_foreground(overlay) -> QImage:
    """按设备像素比捕获父控件及其子控件，捕获期间临时抑制背景叠加。

    父控件缺失或尺寸无效时返回空图；渲染结束或异常时均恢复捕获标志。
    """
    root = overlay.parentWidget()
    if root is None or root.width() <= 0 or root.height() <= 0:
        return QImage()
    ratio = root.devicePixelRatioF()
    image = QImage(
        max(1, round(root.width() * ratio)),
        max(1, round(root.height() * ratio)),
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    image.setDevicePixelRatio(ratio)
    image.fill(0)
    previous = overlay._capturing_foreground
    overlay._capturing_foreground = True
    try:
        root.render(image, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
    finally:
        overlay._capturing_foreground = previous
    return image
