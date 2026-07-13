"""为系统模糊无法按圆角裁切的下拉窗口生成实时磨砂背景。

应用内容从所属窗口绘制，避免屏幕采样反复包含浮层自身。窗口之外的区域使用
展开前的桌面快照；关闭浮层后停止采样并释放图像。
"""

from __future__ import annotations

from PIL import Image, ImageFilter
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QImage, QPainter, QRegion
from PySide6.QtWidgets import QWidget

from src.ui import theme

_PADDING = 40
_BLUR_RADIUS = 14


def _blur_image(image: QImage) -> QImage:
    """按逻辑像素模糊并限制工作尺寸，保留原始尺寸与屏幕缩放比例。"""
    size = image.size()
    small = image
    if size.width() > 640 or size.height() > 640:
        small = image.scaled(640, 640, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    scale = small.width() / size.width()
    small = small.convertToFormat(QImage.Format.Format_RGBA8888)
    pixels = Image.frombytes("RGBA", (small.width(), small.height()), bytes(small.constBits()))
    pixels = pixels.filter(ImageFilter.GaussianBlur(_BLUR_RADIUS * image.devicePixelRatio() * scale))
    data = pixels.tobytes()
    result = QImage(data, pixels.width, pixels.height, pixels.width * 4, QImage.Format.Format_RGBA8888).copy()
    if result.size() != size:
        result = result.scaled(size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    result.setDevicePixelRatio(image.devicePixelRatio())
    return result


class PopupBackdrop(QObject):
    """在浮层显示期间更新背景，并缓存相同内容以避免重复模糊和重绘。"""

    def __init__(self, popup: QWidget) -> None:
        """让窗口拥有采样定时器和图像，Qt 销毁窗口时一并取消后续回调。"""
        super().__init__(popup)
        self._popup = popup
        self._desktop = QImage()
        self._desktop_origin = QPoint()
        self._source = QImage()
        self._layer_blurs: dict[QWidget, tuple[QImage, QImage]] = {}
        self.image = QImage()
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._refresh)
        popup.installEventFilter(self)

    def eventFilter(self, popup, event):
        """显示前准备首帧；显示期间跟随几何变化，关闭时停止采样。"""
        kind = event.type()
        if kind == QEvent.Type.Show:
            screen = popup.screen()
            if screen is not None:
                self._desktop = screen.grabWindow(0).toImage()
                self._desktop_origin = screen.geometry().topLeft()
            self._refresh()
            self._timer.start()
        elif kind == QEvent.Type.Hide:
            self._timer.stop()
            self._desktop = QImage()
            self._source = QImage()
            self._layer_blurs.clear()
            self.image = QImage()
        elif kind in (QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.DevicePixelRatioChange):
            if popup.isVisible():
                self._refresh()
        return False

    def _capture(self, rect: QRect) -> QImage:
        """合成所需的父窗口区域，并为透明对话框保留其下层内容和模糊。"""
        popup = self._popup
        ratio = popup.devicePixelRatioF()
        result = QImage(
            round(rect.width() * ratio), round(rect.height() * ratio), QImage.Format.Format_ARGB32_Premultiplied
        )
        result.setDevicePixelRatio(ratio)
        result.fill(theme.color("bg.base"))
        painter = QPainter(result)
        painter.drawImage(self._desktop_origin - rect.topLeft(), self._desktop)
        painter.end()

        windows = []
        owner = popup.parentWidget()
        while owner is not None:
            window = owner.window()
            windows.append(window)
            if not window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground):
                break
            owner = window.parentWidget()
        # 透明根窗口的系统背板无法由 QWidget 重绘取得，保留展开前的实际合成。
        if windows and windows[-1].testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground):
            return result
        for window in reversed(windows):
            origin = window.mapToGlobal(QPoint())
            area = rect.intersected(QRect(origin, window.size()))
            if area.isEmpty():
                continue
            local = area.translated(-origin)
            contents = window.grab(local)
            if window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground):
                cached = self._layer_blurs.get(window)
                if cached is None or cached[0] != result or cached[0].devicePixelRatio() != ratio:
                    # 保存独立的 QImage 包装，后续合成写入 result 时由 Qt 分离共享像素。
                    cached = QImage(result), _blur_image(result)
                    self._layer_blurs[window] = cached
                background = cached[1]
                painter = QPainter(result)
                region = window.mask() if not window.mask().isEmpty() else QRegion(window.rect())
                painter.setClipRegion(region.translated(origin - rect.topLeft()))
                painter.drawImage(QPoint(), background)
                painter.end()
            painter = QPainter(result)
            painter.drawPixmap(area.topLeft() - rect.topLeft(), contents)
            painter.end()
        return result

    def _refresh(self) -> None:
        """只采集浮层附近的内容，变化时更新磨砂图像并请求一次重绘。"""
        popup = self._popup
        if not popup.isVisible() or popup.size().isEmpty():
            return
        rect = QRect(popup.mapToGlobal(QPoint()), popup.size()).adjusted(-_PADDING, -_PADDING, _PADDING, _PADDING)
        source = self._capture(rect)
        if source == self._source:
            return
        self._source = source
        blurred = _blur_image(source)
        ratio = source.devicePixelRatio()
        crop = QRectF(_PADDING * ratio, _PADDING * ratio, popup.width() * ratio, popup.height() * ratio).toAlignedRect()
        self.image = blurred.copy(crop)
        self.image.setDevicePixelRatio(ratio)
        popup.update()
