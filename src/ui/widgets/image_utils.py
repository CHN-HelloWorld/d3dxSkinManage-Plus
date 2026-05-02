"""提供预览图的等比缩放、居中裁切及圆角渲染。

卡片使用填充裁切保持网格尺寸一致，详情预览使用完整适配保留全部画面。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QImage,
    QImageReader,
    QPainter,
    QPainterPath,
    QPixmap,
    qAlpha,
    qBlue,
    qGreen,
    qRed,
)
from PySide6.QtWidgets import QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene

from src.utils import fs
from src.utils.fs import read_bytes


def load_pixmap_file(path: str) -> QPixmap:
    """按完整路径读取字节后解码，绕过 Qt 文件名缓存并兼容 Windows 长路径。"""
    pixmap = QPixmap()
    try:
        pixmap.loadFromData(read_bytes(path))
    except OSError:
        pass
    return pixmap


def load_scaled_qimage_file(
    path: str,
    max_size: QSize,
    *,
    auto_transform: bool = False,
) -> QImage:
    """按显示尺寸解码图片，并在返回前关闭源文件。

    工作线程等待下一任务时不保留读取器，避免 Windows 无法删除预览缓存。
    """
    reader = QImageReader(fs.native_path(path))
    try:
        reader.setDecideFormatFromContent(True)
        reader.setAutoTransform(auto_transform)
        source_size = reader.size()
        if (
            source_size.isValid()
            and max_size.isValid()
            and (source_size.width() > max_size.width() or source_size.height() > max_size.height())
        ):
            reader.setScaledSize(
                source_size.scaled(
                    max_size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                )
            )
        return reader.read()
    finally:
        # 释放读取器包装对象时关闭底层文件。
        del reader


def _gaussian_blur(pixmap: QPixmap, radius: float) -> QPixmap:
    """使用高质量高斯模糊生成同尺寸图像。

    输入为已缩小到工作尺寸的背景图，以限制卡片模糊填充的处理开销。
    """
    if pixmap is None or pixmap.isNull() or radius <= 0:
        return pixmap
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(pixmap)
    blur = QGraphicsBlurEffect()
    blur.setBlurRadius(radius)
    blur.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
    item.setGraphicsEffect(blur)
    scene.addItem(item)
    out = QPixmap(pixmap.size())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    try:
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        scene.render(
            p,
            QRectF(0, 0, pixmap.width(), pixmap.height()),
            QRectF(0, 0, pixmap.width(), pixmap.height()),
        )
    finally:
        p.end()
    return out


def fill_crop_rounded(
    pixmap: QPixmap,
    width: int,
    height: int,
    radius: int = 6,
    *,
    smooth: bool = True,
) -> QPixmap:
    """按比例填充目标尺寸、居中裁切并处理圆角。

    返回目标尺寸的图像，圆角之外透明。smooth=False 使用快速缩放，适用于高频尺寸调整。
    """
    if pixmap is None or pixmap.isNull() or width <= 0 or height <= 0:
        return pixmap
    target = QSize(width, height)
    mode = Qt.TransformationMode.SmoothTransformation if smooth else Qt.TransformationMode.FastTransformation
    scaled = pixmap.scaled(target, Qt.AspectRatioMode.KeepAspectRatioByExpanding, mode)
    # 中心裁切到目标尺寸
    x = max(0, (scaled.width() - width) // 2)
    y = max(0, (scaled.height() - height) // 2)
    cropped = scaled.copy(x, y, width, height)
    if radius <= 0:
        return cropped
    # 圆角：画进一张透明底 pixmap，clip 成圆角矩形。
    out = QPixmap(width, height)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        r = min(radius, width / 2, height / 2)
        path.addRoundedRect(QRectF(0, 0, width, height), r, r)
        p.setClipPath(path)
        p.drawPixmap(0, 0, cropped)
    finally:
        p.end()
    return out


def round_corners(pixmap: QPixmap, radius: int = 6) -> QPixmap:
    """为已经完成缩放和裁切的图像添加圆角，保留其像素尺寸。"""
    if pixmap is None or pixmap.isNull() or radius <= 0:
        return pixmap
    w, h = pixmap.width(), pixmap.height()
    out = QPixmap(w, h)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        r = min(radius, w / 2, h / 2)
        path.addRoundedRect(QRectF(0, 0, w, h), r, r)
        p.setClipPath(path)
        p.drawPixmap(0, 0, pixmap)
    finally:
        p.end()
    return out


def fit_contain_rounded(
    pixmap: QPixmap,
    width: int,
    height: int,
    radius: int = 6,
    *,
    smooth: bool = True,
) -> QPixmap:
    """将图像等比适配到目标范围并裁出圆角，保持完整画面。

    返回尺寸为适配后的实际尺寸，居中显示时剩余区域透出控件背景。
    卡片填充裁切使用 fill_crop_rounded。
    """
    if pixmap is None or pixmap.isNull() or width <= 0 or height <= 0:
        return pixmap
    mode = Qt.TransformationMode.SmoothTransformation if smooth else Qt.TransformationMode.FastTransformation
    scaled = pixmap.scaled(QSize(width, height), Qt.AspectRatioMode.KeepAspectRatio, mode)
    return round_corners(scaled, radius)


def fill_blur_rounded(
    pixmap: QPixmap,
    width: int,
    height: int,
    radius: int = 6,
    *,
    smooth: bool = True,
    darken: int = 72,
    blur_radius: float = 0.0,
) -> QPixmap:
    """完整显示前景图片，以同图的放大模糊版本填充留白，并裁出圆角。

    前景按比例缩放而不裁切；背景扩大至少一个模糊半径，使边缘淡出位于可视区域之外。
    模糊后叠加 darken 指定的暗色层，以突出前景。blur_radius 为零时按短边自适应，
    smooth 仅控制前景缩放质量。返回图像与目标尺寸一致，圆角之外透明。
    """
    if pixmap is None or pixmap.isNull() or width <= 0 or height <= 0:
        return pixmap
    mode = Qt.TransformationMode.SmoothTransformation if smooth else Qt.TransformationMode.FastTransformation
    out = QPixmap(width, height)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        r = min(radius, width / 2, height / 2)
        if r > 0:
            path = QPainterPath()
            path.addRoundedRect(QRectF(0, 0, width, height), r, r)
            p.setClipPath(path)
        # 背景扩大至少一个模糊半径，使模糊边缘的透明区域位于可视范围之外。
        rad = blur_radius if blur_radius > 0 else max(8.0, min(width, height) * 0.16)
        margin = int(rad) + 2
        bw, bh = width + 2 * margin, height + 2 * margin
        bg = pixmap.scaled(
            QSize(bw, bh),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        bx = max(0, (bg.width() - bw) // 2)
        by = max(0, (bg.height() - bh) // 2)
        bg = bg.copy(bx, by, bw, bh)
        bg = _gaussian_blur(bg, rad)
        # 居中绘制（背景比框大 margin，超出与淡出部分被圆角 clip 裁掉）。
        p.drawPixmap(-margin, -margin, bg)
        if darken > 0:
            p.fillRect(0, 0, width, height, QColor(0, 0, 0, darken))
        # 前景：contain 完整缩入 + 居中绘制。
        fg = pixmap.scaled(QSize(width, height), Qt.AspectRatioMode.KeepAspectRatio, mode)
        fx = (width - fg.width()) // 2
        fy = (height - fg.height()) // 2
        p.drawPixmap(fx, fy, fg)
    finally:
        p.end()
    return out


def trim_uniform_border(
    pixmap: QPixmap,
    *,
    dark_luma: int = 30,
    alpha_thresh: int = 16,
    dark_ratio: float = 0.90,
    max_trim: float = 0.45,
) -> QPixmap:
    """裁去图片四周近黑或透明的边框，返回处理后的 pixmap。

    从四边逐行或逐列向内扫描，裁去近黑或透明像素比例达到 ``dark_ratio`` 的边缘。
    结果可交给 :func:`fit_contain_rounded` 缩放，避免完整显示图片时保留其自带边框。
    未匹配边框的图片保持原样。

    每边最多裁去 ``max_trim``（默认 45%）；若裁后任一维不足原图的 10%，则返回原图。
    """
    if pixmap is None or pixmap.isNull():
        return pixmap
    img = pixmap.toImage()
    w, h = img.width(), img.height()
    if w < 8 or h < 8:
        return pixmap
    sx = max(1, w // 64)  # 横向抽样步长，用于限制边框检测开销。
    sy = max(1, h // 64)  # 纵向采样步长

    def _dark(px: int) -> bool:
        """依据透明度阈值和加权亮度判断单个像素是否属于可裁剪的暗边。"""
        if qAlpha(px) < alpha_thresh:
            return True
        return (0.299 * qRed(px) + 0.587 * qGreen(px) + 0.114 * qBlue(px)) < dark_luma

    def row_dark(y: int) -> bool:
        """按横向采样间隔统计指定行的暗像素比例，判断其是否达到边框阈值。"""
        n = d = 0
        x = 0
        while x < w:
            n += 1
            if _dark(img.pixel(x, y)):
                d += 1
            x += sx
        return n > 0 and d / n >= dark_ratio

    def col_dark(x: int) -> bool:
        """按纵向采样间隔统计指定列的暗像素比例，判断其是否达到边框阈值。"""
        n = d = 0
        y = 0
        while y < h:
            n += 1
            if _dark(img.pixel(x, y)):
                d += 1
            y += sy
        return n > 0 and d / n >= dark_ratio

    lim_v = int(h * max_trim)
    lim_h = int(w * max_trim)
    top = bottom = left = right = 0
    while top < lim_v and row_dark(top):
        top += 1
    while bottom < lim_v and row_dark(h - 1 - bottom):
        bottom += 1
    while left < lim_h and col_dark(left):
        left += 1
    while right < lim_h and col_dark(w - 1 - right):
        right += 1
    if not (top or bottom or left or right):
        return pixmap
    nw, nh = w - left - right, h - top - bottom
    if nw < w * 0.1 or nh < h * 0.1:  # 整幅图像近黑时保留原图。
        return pixmap
    return pixmap.copy(left, top, nw, nh)
