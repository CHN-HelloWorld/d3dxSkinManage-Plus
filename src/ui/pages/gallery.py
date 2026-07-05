"""通过虚拟滚动显示游戏资源的预览图库。"""

from __future__ import annotations

import queue as _queue
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsBlurEffect,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.models.mod import AgeRating, Mod
from src.ui import theme
from src.ui.smooth_scroll import SmoothScroller
from src.ui.widgets.card_pool import CardPool
from src.ui.widgets.card_render_cache import preview_cache_key
from src.ui.widgets.card_reveal import CardRevealQueue
from src.ui.widgets.gallery_loading import GalleryLoadingPanel
from src.ui.widgets.icon_combo import LeadingIconComboBox
from src.ui.widgets.image_utils import (
    fill_blur_rounded,
    load_pixmap_file,
    load_scaled_qimage_file,
)
from src.ui.widgets.local_preview_cache import (
    DecodedPreview,
    cached_preview,
    load_preview,
    preview_cache_token,
    rendered_preview,
    retain_viewed_preview,
)
from src.ui.widgets.scroll_edge_fade import ScrollEdgeFade
from src.ui.widgets.spinner import ShimmerLabel
from src.ui.worker_lifecycle import manage_page_worker
from src.utils import fs
from src.utils.i18n import N_, t
from src.utils.i18n import t as tr
from src.utils.image_cache_budget import image_cache_budget
from src.utils.qt_timers import call_later

# 后台图片加载线程。


class _GalleryPixmapWorker(QThread):
    """后台线程：批量读取磁盘图片并解码为 QImage，完成后逐张通知主线程"""

    image_loaded = Signal(str, str, int, object)  # mod_id, path, request_id, DecodedPreview

    def __init__(self, parent=None):
        """建立预览解码任务队列，初始允许接收和处理请求。"""
        super().__init__(parent)
        self._queue: _queue.Queue = _queue.Queue()
        self._stop = False

    def enqueue(self, mod_id: str, path: str, target_w: int, target_h: int, request_id: int = 0):
        """排入未停止的预览任务，捕获请求编号与缓存代次供结果校验使用。"""
        if not self._stop:
            self._queue.put((mod_id, path, target_w, target_h, request_id, preview_cache_token()))

    def clear_queue(self):
        """清空尚未取出的预览任务，不中断当前解码。"""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except _queue.Empty:
                break

    def stop(self):
        """标记停止并清空等待任务，用结束哨兵唤醒可能阻塞的队列读取。"""
        self._stop = True
        self.clear_queue()
        self._queue.put(None)

    def run(self):
        """顺序解码目标尺寸的预览并携带请求身份发布结果，停止后不再发送图片。"""
        while not self._stop:
            item = self._queue.get()
            if item is None:
                break
            mod_id, path, tw, th, request_id, token = item
            preview = None
            try:
                # 直接从原生长路径按显示尺寸解码，避免 read_bytes + loadFromData
                # 在大图上长时间占用 Python 回调调度。
                target_size = QSize(max(1, tw), max(1, th))
                preview = load_preview(
                    path,
                    target_size,
                    load_scaled_qimage_file,
                    auto_transform=True,
                    cache_token=token,
                    cancelled=lambda: self._stop,
                )
            except Exception:
                preview = None
            if not self._stop:
                self.image_loaded.emit(mod_id, path, request_id, preview)
            preview = None


class GalleryItem(QFrame):
    """画廊项 - 支持延迟加载预览图"""

    clicked = Signal(str)
    double_clicked = Signal(str)

    ITEM_WIDTH = 280
    ITEM_HEIGHT = 320

    def __init__(self, mod: Mod, parent: QWidget | None = None, adult_mode: str = "show", defer_pixmap: bool = False):
        """保存 Mod 与成人内容显示策略，初始化预览完成状态并构建卡片。"""
        super().__init__(parent)
        self._mod = mod
        # 成人级(X)预览模式："show"=正常 | "hide"=隐藏占位 | "blur"=模糊
        self._adult_mode = adult_mode
        self._cached_pixmap: QPixmap | None = None
        self._preview_requested = False
        self._preview_resolved = (adult_mode == "hide" and mod.info.age_rating == AgeRating.X) or not bool(
            mod.primary_preview
        )
        self._setup_ui(defer_pixmap)

    def _setup_ui(self, defer_pixmap: bool = False):
        """构建预览、名称和状态区域，按显示策略选择图片、隐藏占位或延迟加载提示。"""
        self.setFixedSize(self.ITEM_WIDTH, self.ITEM_HEIGHT)
        self.setFrameStyle(QFrame.Shape.StyledPanel)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("galleryItem")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 8)
        layout.setSpacing(4)

        self._image = ShimmerLabel()
        self._image.setFixedSize(self.ITEM_WIDTH - 8, self.ITEM_HEIGHT - 70)
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image.setObjectName("galleryImage")

        # 成人级内容隐藏
        if self._adult_mode == "hide" and self._mod.info.age_rating == AgeRating.X:
            self._image.setText(tr("已隐藏"))
            self._image.setStyleSheet(
                f"QLabel {{ background-color: {theme.surface_color('bg.subtle', 'placeholder')}; "
                f"color: {theme.color('danger')}; font-size: 12px; "
                f"border-radius: {theme.radius('control')}px; }}"
            )
        elif defer_pixmap and self._mod.primary_preview:
            # 延迟加载期间显示动态占位。
            self._image.start_loading()
        elif self._mod.primary_preview and fs.exists(self._mod.primary_preview):
            pixmap = load_pixmap_file(self._mod.primary_preview)
            if not pixmap.isNull():
                scaled = fill_blur_rounded(
                    pixmap,
                    self._image.width(),
                    self._image.height(),
                    radius=8,
                )
                self._image.setPixmap(scaled)
                self._cached_pixmap = pixmap
                self._apply_adult_blur()
            else:
                self._image.setText(t("无预览"))
        else:
            self._image.setText(t("无预览"))

        layout.addWidget(self._image)

        from PySide6.QtGui import QFontMetrics

        name = QLabel()
        self._name_label = name
        name.setWordWrap(False)
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setMaximumHeight(24)
        name.setStyleSheet(f"font-weight: 600; font-size: {theme.font('size_card_title')}px;")
        name.ensurePolished()
        fm = QFontMetrics(name.font())
        elided = fm.elidedText(self._mod.display_name, Qt.TextElideMode.ElideRight, self.ITEM_WIDTH - 16)
        name.setText(elided)
        if elided != self._mod.display_name:
            name.setToolTip(self._mod.display_name)
        layout.addWidget(name)

        status = t("● 已启用") if self._mod.is_enabled else t("○ 已禁用")
        color = theme.color("success") if self._mod.is_enabled else theme.color("text.tertiary")
        status_label = QLabel(status)
        self._status_label = status_label
        status_label.setStyleSheet(f"color: {color}; font-size: {theme.font('size_secondary')}px;")
        layout.addWidget(status_label)

    def set_mod(self, mod: Mod, *, adult_mode: str = "show") -> None:
        """复用已有标签和图片，不重新构造控件。"""
        self._mod = mod
        self._adult_mode = adult_mode
        self._cached_pixmap = None
        self._preview_requested = False
        hidden = adult_mode == "hide" and mod.info.age_rating == AgeRating.X
        self._preview_resolved = hidden or not bool(mod.primary_preview)
        if self._image.styleSheet():
            self._image.setStyleSheet("")
        if hidden:
            self._image.setText(tr("已隐藏"))
            self._image.setStyleSheet(
                f"QLabel {{ background-color: {theme.surface_color('bg.subtle', 'placeholder')}; "
                f"color: {theme.color('danger')}; font-size: 12px; "
                f"border-radius: {theme.radius('control')}px; }}"
            )
        elif mod.primary_preview:
            self._image.start_loading()
        else:
            self._image.setText(t("无预览"))
        elided = self._name_label.fontMetrics().elidedText(
            mod.display_name,
            Qt.TextElideMode.ElideRight,
            self.ITEM_WIDTH - 16,
        )
        self._name_label.setText(elided)
        self._name_label.setToolTip(mod.display_name if elided != mod.display_name else "")
        self._status_label.setText(t("● 已启用") if mod.is_enabled else t("○ 已禁用"))
        color = theme.color("success") if mod.is_enabled else theme.color("text.tertiary")
        status_style = f"color: {color}; font-size: {theme.font('size_secondary')}px;"
        if self._status_label.styleSheet() != status_style:
            self._status_label.setStyleSheet(status_style)
        self._apply_adult_blur()

    def scroll_render_key(self) -> tuple | None:
        """按预览标识、标题、启用状态和显示策略构造滚动绘制缓存键，无预览标识时返回空值。"""
        preview = preview_cache_key(self._image)
        if preview is None:
            return None
        return preview, self._name_label.text(), self._status_label.text(), self._adult_mode

    def _apply_adult_blur(self) -> None:
        """blur 模式下给 X 级预览图加模糊（与 Mod 商店 NSFW 模糊一致）；其余模式清除。"""
        want = (
            self._adult_mode == "blur" and self._mod.info.age_rating == AgeRating.X and self._cached_pixmap is not None
        )
        if want:
            if not isinstance(self._image.graphicsEffect(), QGraphicsBlurEffect):
                effect = QGraphicsBlurEffect(self._image)
                effect.setBlurRadius(20)
                self._image.setGraphicsEffect(effect)
        elif self._image.graphicsEffect() is not None:
            self._image.setGraphicsEffect(None)

    def set_pixmap(self, pixmap: QPixmap):
        """设置预览图（由后台线程回调调用）"""
        if not pixmap.isNull():
            self._cached_pixmap = pixmap
            self._preview_requested = False
            self._preview_resolved = True
            self._image.setPixmap(pixmap)
            self._image.setText("")  # 清除占位文字
            self._image.setStyleSheet("")
            self._apply_adult_blur()

    def set_no_preview(self) -> None:
        """清除图片引用并结束加载状态，显示统一的无预览占位。"""
        self._cached_pixmap = None
        self._preview_requested = False
        self._preview_resolved = True
        self._image.setText(t("无预览"))
        self._image.setStyleSheet(
            f"QLabel {{ background-color: {theme.surface_color('bg.subtle', 'placeholder')}; "
            f"color: {theme.color('text.tertiary')}; font-size: 12px; "
            f"border-radius: {theme.radius('control')}px; }}"
        )

    def mousePressEvent(self, event):
        """左键点击时发布当前 Mod 标识，并保留基类鼠标处理。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._mod.mod_id)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """左键双击时发布当前 Mod 标识，供宿主打开详情或预览。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit(self._mod.mod_id)
        super().mouseDoubleClickEvent(event)
