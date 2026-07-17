"""滚动卡片区上下边界的真实透明渐隐。"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Slot
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import QAbstractScrollArea, QGraphicsEffect, QWidget

from src.ui.widgets.card_render_cache import CardSourceCache


class _CardEdgeFadeEffect(QGraphicsEffect):
    """只把单张卡片落入视口边缘的那一部分擦成透明。"""

    def __init__(
        self,
        card: QWidget,
        scroll_area: QAbstractScrollArea,
        fade_height: int,
        cache_key_provider: Callable[[QWidget], object] | None = None,
    ) -> None:
        """关联卡片与视口，初始化局部源图缓存、缩放快照和入场透明度状态。"""
        super().__init__(card)
        self._card = card
        self._scroll_area = scroll_area
        self._viewport = scroll_area.viewport()
        self._fade_height = float(fade_height)
        self._source_cache = CardSourceCache(card, cache_key_provider) if cache_key_provider else None
        self._resizing = False
        self._resize_finishing = False
        self._resize_source = QPixmap()
        self._resize_frame = QPixmap()
        self._had_edge_mask = False
        self.reveal_opacity = 1.0
        self.reveal_painted = False
        if self._source_cache is not None:
            card.installEventFilter(self)

    def invalidate_source(self) -> None:
        """清空卡片源图及缩放表面，防止重新绑定后显示旧记录的像素。"""
        # 重新绑定也使缓存失效，缩放用表面不能保留旧记录。
        self._resize_source = QPixmap()
        self._resize_frame = QPixmap()
        if self._source_cache is not None:
            self._source_cache.clear()

    def set_resizing(self, resizing: bool) -> None:
        """进入尺寸调整时保留源图快照，退出时作废缓存并请求按真实控件重新绘制。"""
        self._resize_finishing = False
        if self._resizing == resizing:
            return
        self._resizing = resizing
        if resizing and self._source_cache is not None:
            self._resize_source = self._source_cache.snapshot()
        else:
            self.invalidate_source()
        self.update()

    def set_reveal_opacity(self, opacity: float) -> None:
        """限制并更新入场透明度，完全隐藏时重置已绘制标记。"""
        opacity = min(1.0, max(0.0, opacity))
        if opacity == 0.0:
            self.reveal_painted = False
        if self.reveal_opacity != opacity:
            self.reveal_opacity = opacity
            self.update()

    def eventFilter(self, obj, event):
        """外观或布局变化时使普通源图缓存失效，交互到达时提前结束延后的缩放恢复。"""
        if event.type() in (
            QEvent.Type.Hide,
            QEvent.Type.Show,
            QEvent.Type.Resize,
            QEvent.Type.LayoutRequest,
            QEvent.Type.StyleChange,
            QEvent.Type.PaletteChange,
            QEvent.Type.FontChange,
            QEvent.Type.EnabledChange,
            QEvent.Type.ChildAdded,
            QEvent.Type.ChildRemoved,
            QEvent.Type.ChildPolished,
            QEvent.Type.Enter,
            QEvent.Type.Leave,
            QEvent.Type.FocusIn,
            QEvent.Type.FocusOut,
        ):
            # 缩放期间保留已有像素，字体和布局变化使普通缓存键失效。
            if self._source_cache is not None:
                self._source_cache.invalidate()
        if self._resize_finishing and event.type() in (
            QEvent.Type.Enter,
            QEvent.Type.FocusIn,
            QEvent.Type.MouseButtonPress,
        ):
            self.set_resizing(False)
        return False

    def draw(self, painter: QPainter) -> None:
        """在保留调用方绘制器状态的前提下应用入场透明度，再绘制卡片及边缘遮罩。"""
        if self.reveal_opacity == 0.0:
            return
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * self.reveal_opacity)
            self._draw_card(painter)
            self.reveal_painted = True
        finally:
            painter.restore()

    def _draw_card(self, painter: QPainter) -> None:
        """获取缓存或实时卡片像素，将视口上下边缘映射到局部坐标，并以透明擦除实现渐隐。"""
        try:
            viewport = self._viewport
            bar = self._scroll_area.verticalScrollBar()
            card_y = float(self._card.mapTo(viewport, QPoint(0, 0)).y())
        except RuntimeError:
            self.drawSource(painter)
            return

        fade = self._fade_height
        viewport_h = float(viewport.height())
        card_bottom = card_y + self._card.height()
        scrollable = bar.maximum() > bar.minimum()
        top_mask = scrollable and bar.value() > bar.minimum() and card_bottom > 0 and card_y < fade
        bottom_mask = (
            scrollable and bar.value() < bar.maximum() and card_bottom > viewport_h - fade and card_y < viewport_h
        )
        cache_key = self._source_cache.key(painter) if self._source_cache and not self._resizing else None
        if self._resizing:
            if self._resize_source.isNull():
                self._resize_source = self.sourcePixmap(
                    Qt.CoordinateSystem.LogicalCoordinates,
                    None,
                    QGraphicsEffect.PixmapPadMode.NoPad,
                )
            dpr = painter.device().devicePixelRatioF()
            # 在显式透明表面绘制缩放结果，使边缘遮罩真正移除像素而非加深底色。
            size = QSize(round(self._card.width() * dpr), round(self._card.height() * dpr))
            if self._resize_frame.size() != size or self._resize_frame.devicePixelRatioF() != dpr:
                self._resize_frame = QPixmap(size)
                self._resize_frame.setDevicePixelRatio(dpr)
                self._resize_frame.fill(Qt.GlobalColor.transparent)
                resize_painter = QPainter(self._resize_frame)
                resize_painter.drawPixmap(
                    QRectF(self._card.rect()), self._resize_source, QRectF(self._resize_source.rect())
                )
                resize_painter.end()
            pixmap = QPixmap(self._resize_frame)
            offset = QPoint()
        elif cache_key is None:
            self.invalidate_source()
            # 局部源图保留原点与图形效果，避免共享绘制器重复应用控件偏移。
            offset = QPoint()
            pixmap = self.sourcePixmap(
                Qt.CoordinateSystem.LogicalCoordinates,
                offset,
                QGraphicsEffect.PixmapPadMode.NoPad,
            )
        else:
            pixmap, offset = self._source_cache.pixmap(self, cache_key)
        if pixmap.isNull():
            return
        if not top_mask and not bottom_mask:
            painter.drawPixmap(offset if offset is not None else QPointF(), pixmap)
            return
        dpr = pixmap.devicePixelRatio() or 1.0
        pixmap_w = pixmap.width() / dpr
        offset_y = float(offset.y()) if offset is not None else 0.0

        qp = QPainter(pixmap)
        qp.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
        try:
            if top_mask:
                # 渐变锚定在 viewport 顶边；换算到当前卡片/源 pixmap 的局部坐标。
                top = -card_y - offset_y
                gradient = QLinearGradient(0.0, top, 0.0, top + fade)
                gradient.setColorAt(0.0, QColor(0, 0, 0, 255))
                gradient.setColorAt(1.0, QColor(0, 0, 0, 0))
                qp.fillRect(
                    QRectF(0.0, top, pixmap_w, fade),
                    QBrush(gradient),
                )

            if bottom_mask:
                bottom = viewport_h - card_y - offset_y
                start = bottom - fade
                gradient = QLinearGradient(0.0, start, 0.0, bottom)
                gradient.setColorAt(0.0, QColor(0, 0, 0, 0))
                gradient.setColorAt(1.0, QColor(0, 0, 0, 255))
                qp.fillRect(
                    QRectF(0.0, start, pixmap_w, fade),
                    QBrush(gradient),
                )
        finally:
            qp.end()

        painter.drawPixmap(offset if offset is not None else QPointF(), pixmap)


class ScrollEdgeFade(QObject):
    """为接近视口上下边缘的卡片应用局部透明度遮罩。

    直接调整卡片像素，使底层材质透出；静态卡片复用原始缓存，交互卡片实时绘制。
    缓存以单张卡片管理，不为整个虚拟区域分配图像。
    """

    def __init__(
        self,
        scroll_area: QAbstractScrollArea,
        card_provider: Callable[[], Iterable[QWidget]] | None = None,
        fade_height: int = 58,
        *,
        cache_key_provider: Callable[[QWidget], object] | None = None,
    ) -> None:
        """监听视口与滚动范围变化，建立渐隐刷新、动画让行重试和分批恢复定时器。"""
        super().__init__(scroll_area)
        self._scroll_area = scroll_area
        self._viewport = scroll_area.viewport()
        self._card_provider = card_provider
        self._cache_key_provider = cache_key_provider
        self._resizing = False
        self._resize_finish_queue: list[_CardEdgeFadeEffect] = []
        self._resize_finish_timer = QTimer(self)
        self._resize_finish_timer.setInterval(16)
        self._resize_finish_timer.timeout.connect(self._finish_resize_batch)
        self._fade_height = max(24, int(fade_height))
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.setInterval(420)
        self._retry_timer.timeout.connect(self.refresh)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(0)
        self._refresh_timer.timeout.connect(self.refresh)

        self._viewport.installEventFilter(self)
        bar = scroll_area.verticalScrollBar()
        bar.valueChanged.connect(self._on_scroll_value_changed)
        bar.rangeChanged.connect(self._on_scroll_range_changed)
        self._refresh_timer.start()

    @Slot(int)
    def _on_scroll_value_changed(self, _value: int) -> None:
        """滚动位置变化后重新计算卡片与渐隐边缘的交集。"""
        self.refresh()

    @Slot(int, int)
    def _on_scroll_range_changed(self, _minimum: int, _maximum: int) -> None:
        """内容滚动范围变化后更新上下边缘是否需要渐隐。"""
        self.refresh()
