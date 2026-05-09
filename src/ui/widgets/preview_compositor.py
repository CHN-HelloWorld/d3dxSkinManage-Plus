"""在工作线程合成缩略图，仅使用 QImage 像素。"""

from PIL import Image, ImageFilter
from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath


def _blur(image: QImage, radius: float) -> QImage:
    # 共同处理预乘颜色通道，避免透明边缘泄露隐藏颜色，输出图像独立拥有缓冲。
    """将图像转换为预乘 RGBA 后交由 Pillow 高斯模糊，并复制为独立存储的 QImage。"""
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888_Premultiplied)
    pixels = Image.frombytes("RGBA", (rgba.width(), rgba.height()), bytes(rgba.constBits()))
    pixels = pixels.filter(ImageFilter.GaussianBlur(radius))
    data = pixels.tobytes()
    return QImage(
        data, pixels.width, pixels.height, pixels.width * 4, QImage.Format.Format_RGBA8888_Premultiplied
    ).copy()


def _cover(image: QImage, width: int, height: int) -> QImage:
    # 直接将源中心区域采样到有界目标，避免先缩放极端长宽比图片造成大额分配。
    """按居中裁剪方式将原图铺满目标尺寸，返回独立的预乘透明通道图像。"""
    scale = max(width / image.width(), height / image.height())
    sw, sh = width / scale, height / scale
    source_rect = QRectF((image.width() - sw) / 2, (image.height() - sh) / 2, sw, sh)
    out = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(QRectF(0, 0, width, height), image, source_rect)
    finally:
        painter.end()
    return out


def compose_preview(image: QImage, width: int, height: int, mode: str, *, dpr: float = 1.0) -> QImage:
    """按目标设备比例保持完整显示、模糊或填充裁剪几何。"""
    if image is None or image.isNull() or width <= 0 or height <= 0:
        return QImage()
    width, height = max(1, round(width * dpr)), max(1, round(height * dpr))
    source = QImage(image)
    source.setDevicePixelRatio(1.0)
    out = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        clip = QPainterPath()
        radius = min(8 * dpr, width / 2, height / 2)
        clip.addRoundedRect(QRectF(0, 0, width, height), radius, radius)
        painter.setClipPath(clip)
        if mode == "crop":
            painter.drawImage(0, 0, _cover(source, width, height))
        else:
            radius = max(8.0 * dpr, min(width, height) * 0.16)
            margin = int(radius) + round(2 * dpr)
            bw, bh = width + 2 * margin, height + 2 * margin
            background = _blur(_cover(source, bw, bh), radius)
            painter.drawImage(-margin, -margin, background)
            painter.fillRect(0, 0, width, height, QColor(0, 0, 0, 72))
            foreground = source.scaled(
                QSize(width, height), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            painter.drawImage((width - foreground.width()) // 2, (height - foreground.height()) // 2, foreground)
    finally:
        painter.end()
    out.setDevicePixelRatio(dpr)
    return out
