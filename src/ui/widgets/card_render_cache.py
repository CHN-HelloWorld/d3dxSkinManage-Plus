"""仅在视觉状态未变时复用未蒙版的卡片像素。

动画返回不可缓存标记，原控件树继续处理输入，避免每次请求都重新绘制。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QGraphicsEffect, QLabel, QWidget


def preview_cache_key(label: QLabel) -> tuple | None:
    """已加载图像和静态占位可缓存，加载及淡入动画不可缓存。"""
    shimmer = getattr(label, "_shimmer", None)
    if shimmer is not None and not shimmer.isHidden():
        return None
    pixmap = label.pixmap()
    return label.text(), pixmap.cacheKey() if pixmap is not None else 0


class CardSourceCache:
    """按卡片内容、尺寸和设备像素比缓存图形效果的源像素图。"""

    def __init__(self, card: QWidget, key_provider: Callable[[QWidget], object]) -> None:
        """绑定卡片及其内容键提供器，初始化空像素图和绘制偏移。"""
        self._card = card
        self._key_provider = key_provider
        self._key = None
        self._pixmap = QPixmap()
        self._offset = QPoint()

    def clear(self) -> None:
        """清空源像素图及其内容键，使下一次绘制重新捕获卡片。"""
        self._key = None
        self._pixmap = QPixmap()

    def invalidate(self) -> None:
        """下次使用时重绘，缩放期间保留上一张表面。"""
        self._key = None

    def snapshot(self) -> QPixmap:
        """尺寸变化使缓存键失效前复制最后的未蒙版表面。"""
        return QPixmap(self._pixmap)

    def key(self, painter: QPainter) -> tuple | None:
        # 悬停、按下、焦点和子控件交互始终实时绘制。
        """返回卡片内容、尺寸和像素比组成的缓存键；悬停或焦点位于卡片内时禁用缓存。"""
        focus = QApplication.focusWidget()
        if self._card.underMouse() or (focus is not None and (focus is self._card or self._card.isAncestorOf(focus))):
            return None
        key = self._key_provider(self._card)
        if key is None:
            return None
        return (key, self._card.width(), self._card.height(), painter.device().devicePixelRatioF())

    def pixmap(self, effect: QGraphicsEffect, key: tuple) -> tuple[QPixmap, QPoint]:
        """按缓存键获取图形效果源像素图和偏移，键变化或缓存为空时重新采样。"""
        if key != self._key or self._pixmap.isNull():
            self._offset = QPoint()
            self._pixmap = effect.sourcePixmap(
                Qt.CoordinateSystem.LogicalCoordinates,
                self._offset,
                QGraphicsEffect.PixmapPadMode.NoPad,
            )
            self._key = key
        # 透明遮罩操作使用独立副本，不在缓存中累积淡化。
        return QPixmap(self._pixmap), self._offset
