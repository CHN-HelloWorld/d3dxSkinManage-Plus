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


class GalleryPage(QWidget):
    """画廊视图页面 - 虚拟滚动优化：只创建可视区域内的卡片，预览图后台线程加载"""

    _INITIAL_CARD_BATCH_SIZE = 1
    _INITIAL_CARD_INTERVAL_MS = 20
    _PIXMAP_BATCH_SIZE = 4
    _PIXMAP_INTERVAL_MS = 32
    _MAX_IMAGE_REQUESTS = 8
    _MAX_PREFETCH_REQUESTS = 2

    mod_selected = Signal(str)
    mod_toggle_requested = Signal(str)
    back_requested = Signal()
    adult_mode_changed = Signal(str)  # 同步到其他页面（"show"|"hide"|"blur"）

    def __init__(self, parent: QWidget | None = None):
        """建立虚拟卡片窗口、复用池和图片请求状态，启动受页面生命周期管理的预览工作线程。"""
        super().__init__(parent)
        self._mods: list[Mod] = []
        self._mods_signature: tuple | None = None
        self._game_id: str | None = None
        self._load_state = "pending"
        self._adult_mode = "show"
        # 虚拟滚动状态。
        self._vs_card_cache: dict[str, GalleryItem] = {}  # mod_id → GalleryItem
        self._vs_card_pool = CardPool()
        self._vs_window: tuple | None = None
        self._vs_image_request_id = 0
        self._vs_image_requests: dict[str, tuple[int, GalleryItem | None, str]] = {}
        self._vs_pending_prefetch: list[tuple[str, str]] = []
        self._vs_cols: int = 1
        self._vs_row_h: int = GalleryItem.ITEM_HEIGHT + 12
        self._vs_total_rows: int = 0
        # 滚动防抖定时器
        self._vs_scroll_timer = QTimer(self)
        self._vs_scroll_timer.setSingleShot(True)
        self._vs_scroll_timer.setInterval(8)
        self._vs_scroll_timer.timeout.connect(self._vs_update_visible)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(0)
        self._resize_timer.timeout.connect(self._check_and_refresh_layout)
        # 首次进入只同步构造一张卡片，剩余首屏内容按帧补齐。
        self._vs_population_timer = QTimer(self)
        self._vs_population_timer.setInterval(self._INITIAL_CARD_INTERVAL_MS)
        self._vs_population_timer.timeout.connect(self._vs_update_visible)
        self._vs_population_anchor_value = 0
        # 延迟加载预览图定时器
        self._vs_pixmap_timer = QTimer(self)
        self._vs_pixmap_timer.setSingleShot(True)
        self._vs_pixmap_timer.setInterval(self._PIXMAP_INTERVAL_MS)
        self._vs_pixmap_timer.timeout.connect(self._vs_load_deferred_pixmaps)
        self._vs_pending_pixmap_items: list[tuple[str, GalleryItem]] = []
        # 后台图片加载线程
        self._pixmap_worker = _GalleryPixmapWorker(self)
        manage_page_worker(self, self._pixmap_worker)
        self._pixmap_worker.image_loaded.connect(self._on_bg_pixmap_loaded)
        self._pixmap_worker.start()
        self._setup_ui()

    def _setup_ui(self):
        """构建成人预览模式、虚拟滚动区域、卡片出现效果以及加载和空状态提示。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        toolbar = QHBoxLayout()
        title = QLabel(t("画廊视图"))
        title.setStyleSheet("font-size: 18px; font-weight: bold;")
        toolbar.addWidget(title)
        toolbar.addStretch()

        # 成人级Mod预览模式（与 Mod 商店一致：下拉框内前置眼睛图标，图标不进下拉项）
        self._adult_mode_combo = LeadingIconComboBox("status/visible")
        self._adult_mode_combo.addItem(tr("显示 NSFW"), "show")
        self._adult_mode_combo.addItem(tr("隐藏 NSFW"), "hide")
        self._adult_mode_combo.addItem(tr("模糊 NSFW"), "blur")
        self._adult_mode_combo.setToolTip(
            tr("显示：正常显示 NSFW Mod 预览\n隐藏：用占位遮挡\n模糊：模糊预览（与在线 Mod 一致）")
        )
        self._adult_mode_combo.currentIndexChanged.connect(self._on_adult_mode_changed)
        toolbar.addWidget(self._adult_mode_combo)

        layout.addLayout(toolbar)

        # 滚动区域
        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        # 透明滚动区：去掉默认 StyledPanel 视口底色，让卡片直接浮在背景图上
        self._scroll_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll_value_changed)
        # 滚轮平滑缓动（与商店/总览一致的柔和滚动手感）
        SmoothScroller.attach(self._scroll_area)

        self._scroll_content = QWidget()
        self._scroll_content.setMinimumHeight(0)
        self._scroll_area.setWidget(self._scroll_content)
        self._scroll_area.viewport().installEventFilter(self)
        self._scroll_edge_fade = ScrollEdgeFade(
            self._scroll_area,
            lambda cache=self._vs_card_cache: cache.values(),
        )
        self._card_reveal = CardRevealQueue(
            self, self._scroll_edge_fade, lambda card, mod_id: self._vs_card_cache.get(mod_id) is card
        )

        # 空状态提示
        self._empty_label = QLabel(t("暂无 Mod 数据\n请先在类别页面导入 Mod"))
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet(f"color: {theme.color('text.tertiary')}; font-size: 14px; padding: 60px;")
        self._empty_label.setParent(self._scroll_content)
        self._empty_label.hide()

        self._loading_panel = GalleryLoadingPanel(
            (GalleryItem.ITEM_WIDTH, GalleryItem.ITEM_HEIGHT), self._scroll_content
        )

        layout.addWidget(self._scroll_area)
        self._update_empty_state()

    @staticmethod
    def _same_path(left: str, right: str) -> bool:
        """将非空路径规范化为绝对路径后比较，仅判断路径表示而不读取文件身份。"""
        if not left or not right:
            return False
        return Path(fs.normal_path(left)).absolute() == Path(fs.normal_path(right)).absolute()

    def set_mods(self, mods: list[Mod]):
        """按名称、预览和分级签名判断是否重建；签名不变时仅替换模型并刷新启用状态。"""
        self._load_state = "ready"
        signature = tuple(
            (
                mod.mod_id,
                mod.display_name,
                mod.primary_preview or "",
                mod.info.age_rating.value,
            )
            for mod in mods
        )
        if signature == self._mods_signature:
            self._mods = mods
            current = {mod.mod_id: mod for mod in mods}
            for mod_id, card in self._vs_card_cache.items():
                if mod_id in current:
                    card._mod = current[mod_id]
                    self._update_card_status(card)
            self._update_empty_state()
            return
        self._mods_signature = signature
        self._mods = mods
        self._refresh()

    def set_loading(self, game_id: str) -> None:
        """同步显示加载状态；同一游戏刷新时保留已显示的卡片。"""
        self._load_state = "loading"
        self._loading_panel.set_progress(-1)
        if game_id != self._game_id:
            self._game_id = game_id
            self._mods = []
            self._mods_signature = None
            self._refresh()
        else:
            self._update_empty_state()

    def set_load_failed(self) -> None:
        """将本轮库加载标记为失败，并更新空状态提示。"""
        self._load_state = "error"
        self._update_empty_state()

    def set_loading_progress(self, count: int) -> None:
        """仅在加载状态下更新已读取条目数量，忽略迟到进度。"""
        if self._load_state == "loading":
            self._loading_panel.set_progress(count)

    def _update_empty_state(self) -> None:
        """按数据与加载状态显示加载面板、空库或错误提示，无内容时恢复视口高度约束。"""
        key = {
            "ready": N_("暂无 Mod 数据\n请先在类别页面导入 Mod"),
            "error": N_("Mod 数据加载失败\n请按 F5 重试"),
        }.get(self._load_state)
        loading = not self._mods and self._load_state in ("pending", "loading")
        self._loading_panel.setVisible(loading)
        if self._mods or key is None:
            self._empty_label.hide()
        else:
            self._empty_label.setText(t(key))
            self._empty_label.show()
        if self._mods:
            return
        self._scroll_content.setMinimumHeight(0)
        self._scroll_content.setMaximumHeight(16777215)
        self._resize_empty_label()
        # 隐藏页面在导航显示时才获得最终视口尺寸。
        call_later(0, self, self._resize_empty_label)

    @staticmethod
    def _update_card_status(card: GalleryItem) -> None:
        """刷新卡片启用状态文字与颜色，仅在样式变化时重新设置样式表。"""
        enabled = card._mod.is_enabled
        card._status_label.setText(t("● 已启用") if enabled else t("○ 已禁用"))
        color = theme.color("success") if enabled else theme.color("text.tertiary")
        style = f"color: {color}; font-size: {theme.font('size_secondary')}px;"
        if card._status_label.styleSheet() != style:
            card._status_label.setStyleSheet(style)

    def update_mods_in_place(self, mods: list[Mod]) -> None:
        """原位替换已有 Mod 模型并更新当前卡片的启用标记，保留布局和预览。"""
        changed = {mod.mod_id: mod for mod in mods}
        if not changed:
            return
        self._mods = [changed.get(mod.mod_id, mod) for mod in self._mods]
        for mod_id, mod in changed.items():
            card = self._vs_card_cache.get(mod_id)
            if card is not None:
                card._mod = mod
                self._update_card_status(card)

    def refresh_from_index(self) -> None:
        """重新刷新视图（响应预设切换等外部触发）"""
        self._refresh()

    def _refresh(self):
        """虚拟滚动版：计算布局参数，按需创建可视区域卡片"""
        self._card_reveal.cancel()
        self._vs_scroll_timer.stop()
        self._vs_population_timer.stop()
        self._vs_pixmap_timer.stop()
        self._vs_pending_pixmap_items.clear()
        self._vs_pending_prefetch.clear()
        self._pixmap_worker.clear_queue()

        # 清除旧缓存卡片
        for card in self._vs_card_cache.values():
            card.hide()
            card.setParent(None)
            card.deleteLater()
        self._vs_card_cache.clear()
        self._vs_card_pool.clear(lambda card: card.deleteLater())
        self._vs_window = None
        self._vs_image_requests.clear()

        if not self._mods:
            self._update_empty_state()
            return

        self._empty_label.hide()
        self._loading_panel.hide()

        # 计算虚拟滚动布局参数
        viewport_w = self._scroll_area.viewport().width()
        avail_w = viewport_w if viewport_w > 100 else self.width() - 48
        spacing = 12
        cols = max(1, (avail_w - 24) // (GalleryItem.ITEM_WIDTH + spacing))
        self._vs_cols = cols
        self._vs_row_h = GalleryItem.ITEM_HEIGHT + spacing
        self._vs_total_rows = (len(self._mods) + cols - 1) // cols
        total_h = self._vs_total_rows * self._vs_row_h + spacing
        self._scroll_content.setMinimumHeight(total_h)
        self._scroll_content.setMaximumHeight(total_h)

        # 初始渲染可视区域
        # 标记全量重建：_vs_update_visible 据此对新建可视卡片做一次交错淡入。
        self._vs_population_anchor_value = self._scroll_area.verticalScrollBar().value()
        self._fresh_population = True
        self._vs_update_visible()

    def _vs_update_visible(self):
        """根据滚动位置，创建/显示可视区域内的卡片，回收远离的卡片"""
        if not self._mods or not self.isVisible():
            return

        scroll_y = self._scroll_area.verticalScrollBar().value()
        viewport_h = self._scroll_area.viewport().height()
        row_h = self._vs_row_h if self._vs_row_h > 0 else 1
        cols = self._vs_cols if self._vs_cols > 0 else 1

        # 保持固定行窗口，并在两侧各预取一行。
        first_row = max(0, scroll_y // row_h - 1)
        last_row = min(self._vs_total_rows - 1, scroll_y // row_h + (viewport_h + row_h - 1) // row_h + 1)

        first_idx = first_row * cols
        last_idx = min(len(self._mods) - 1, (last_row + 1) * cols - 1)

        initial_population = bool(getattr(self, "_fresh_population", False))
        window = (first_idx, last_idx, cols)
        if window == self._vs_window and not initial_population:
            return
        if window != self._vs_window:
            self._vs_pending_pixmap_items.clear()
            self._vs_image_requests = {
                mod_id: (request_id, None, path)
                for mod_id, (request_id, _card, path) in self._vs_image_requests.items()
            }
            for card in self._vs_card_cache.values():
                card._preview_requested = False
        self._vs_window = window
        visible_ids = {self._mods[i].mod_id for i in range(first_idx, last_idx + 1)}
        for mod_id in list(self._vs_card_cache):
            if mod_id in visible_ids:
                continue
            card = self._vs_card_cache.pop(mod_id)
            self._vs_card_pool.release(card)

        spacing = 12
        x_pad = 12
        viewport = self._scroll_area.viewport().rect().translated(0, scroll_y)
        creation_budget = self._INITIAL_CARD_BATCH_SIZE if initial_population else None
        population_remaining = False

        for i in range(first_idx, last_idx + 1):
            mod = self._mods[i]
            row = i // cols
            col = i % cols
            x = x_pad + col * (GalleryItem.ITEM_WIDTH + spacing)
            y = spacing // 2 + row * self._vs_row_h

            item = self._vs_card_cache.get(mod.mod_id)
            is_new = item is None
            if item is None:
                if creation_budget is not None and creation_budget <= 0:
                    population_remaining = True
                    continue
                item = self._vs_card_pool.take(GalleryItem)
                if item is None:
                    item = GalleryItem(mod, parent=self._scroll_content, adult_mode=self._adult_mode, defer_pixmap=True)
                    item.clicked.connect(self.mod_selected.emit)
                    item.double_clicked.connect(self.mod_toggle_requested.emit)
                else:
                    item.set_mod(mod, adult_mode=self._adult_mode)
                self._vs_restore_preview(item)
                self._vs_card_cache[mod.mod_id] = item
                if creation_budget is not None:
                    creation_budget -= 1
            if item.x() != x or item.y() != y:
                item.move(x, y)
            if initial_population and is_new and item.geometry().intersects(viewport):
                self._card_reveal.add(item, mod.mod_id)
            if item.isHidden():
                item.show()

        # 收集所有需要加载预览图的可见卡片（包括因滚动事件
        # 清空 pending 列表后仍未加载预览图的已缓存卡片）
        items_need_pixmap: list[tuple[str, GalleryItem]] = []
        for i in range(first_idx, last_idx + 1):
            mod = self._mods[i]
            card = self._vs_card_cache.get(mod.mod_id)
            if card and not card._preview_resolved and not card._preview_requested:
                card._preview_requested = True
                request = self._vs_image_requests.get(mod.mod_id)
                if request is not None and request[2] == str(mod.primary_preview):
                    self._vs_image_requests[mod.mod_id] = (request[0], card, request[2])
                else:
                    items_need_pixmap.append((mod.mod_id, card))

        self._vs_pending_pixmap_items.extend(items_need_pixmap)
        indices = {self._mods[i].mod_id: i for i in range(first_idx, last_idx + 1)}
        visible_first = scroll_y // row_h * cols
        visible_last = ((scroll_y + viewport_h - 1) // row_h + 1) * cols - 1

        def image_priority(entry):
            """优先加载实际可见卡片，其余图片按距可见区的距离排序。"""
            index = indices.get(entry[0], last_idx + 1)
            visible = visible_first <= index <= visible_last
            return not visible, index if visible else min(abs(index - visible_first), abs(index - visible_last))

        self._vs_pending_pixmap_items.sort(key=image_priority)
        self._vs_prepare_prefetch(first_idx, last_idx)
        self._vs_schedule_pixmaps()

        if initial_population:
            if population_remaining:
                if not self._vs_population_timer.isActive():
                    self._vs_population_timer.start()
            else:
                self._fresh_population = False
                self._vs_population_timer.stop()
                self._scroll_edge_fade.refresh_later()
        self._scroll_edge_fade.refresh()

    @staticmethod
    def _preview_size() -> QSize:
        """返回画廊图片解码和缓存使用的目标尺寸。"""
        return QSize(GalleryItem.ITEM_WIDTH - 8, GalleryItem.ITEM_HEIGHT - 60)

    def _vs_restore_preview(self, item: GalleryItem) -> bool:
        """为未完成预览的卡片尝试读取共享缓存，命中时立即应用并返回成功。"""
        if item._preview_resolved:
            return True
        path = item._mod.primary_preview
        image = cached_preview(path, self._preview_size(), auto_transform=True) if path else None
        if image is None:
            return False
        self._vs_apply_preview(item, image)
        return True

    def _vs_prepare_prefetch(self, first_idx: int, last_idx: int) -> None:
        """按可见区距离准备邻近图片预取，跳过隐藏成人预览、已有缓存和正在请求的路径。"""
        if not image_cache_budget.prefetch_allowed:
            self._vs_pending_prefetch = []
            return
        rows = max(1, (self._scroll_area.viewport().height() + self._vs_row_h - 1) // self._vs_row_h)
        row = self._scroll_area.verticalScrollBar().value() // self._vs_row_h
        first = max(0, row - rows) * self._vs_cols
        last = min(len(self._mods), (row + 2 * rows + 1) * self._vs_cols)
        indices = [i for i in range(first, last) if self._mods[i].mod_id not in self._vs_card_cache]
        indices.sort(key=lambda i: max(first_idx - i, i - last_idx, 0))
        self._vs_pending_prefetch = []
        for i in indices:
            mod = self._mods[i]
            if self._adult_mode == "hide" and mod.info.age_rating == AgeRating.X:
                continue
            path = str(mod.primary_preview or "")
            request = self._vs_image_requests.get(mod.mod_id)
            if (
                path
                and not (request and request[2] == path)
                and cached_preview(path, self._preview_size(), auto_transform=True, validate=False) is None
            ):
                self._vs_pending_prefetch.append((mod.mod_id, path))

    def _vs_schedule_pixmaps(self) -> None:
        """在总请求和预取上限内安排下一批图片加载，已有定时任务时合并调度。"""
        prefetch_count = sum(card is None for _req, card, _path in self._vs_image_requests.values())
        has_work = self._vs_pending_pixmap_items or (
            image_cache_budget.prefetch_allowed
            and self._vs_pending_prefetch
            and prefetch_count < self._MAX_PREFETCH_REQUESTS
        )
        if (
            has_work
            and len(self._vs_image_requests) < self._MAX_IMAGE_REQUESTS
            and not self._vs_pixmap_timer.isActive()
        ):
            self._vs_pixmap_timer.start()

    def _vs_load_deferred_pixmaps(self):
        """优先加载可见预览，再利用空闲容量预取。"""
        if not image_cache_budget.prefetch_allowed:
            self._vs_pending_prefetch = []
        for _ in range(self._PIXMAP_BATCH_SIZE):
            if len(self._vs_image_requests) >= self._MAX_IMAGE_REQUESTS:
                break
            if self._vs_pending_pixmap_items:
                mod_id, item = self._vs_pending_pixmap_items.pop(0)
                if self._vs_card_cache.get(mod_id) is not item:
                    continue
                if item._preview_resolved:
                    item._preview_requested = False
                    continue
                mod = item._mod
                if self._adult_mode == "hide" and mod.info.age_rating == AgeRating.X:
                    item._preview_requested = False
                    item._preview_resolved = True
                    continue
                path = mod.primary_preview
                if not path:
                    item.set_no_preview()
                    continue
                if self._vs_restore_preview(item):
                    continue
            else:
                prefetch_count = sum(c is None for _r, c, _p in self._vs_image_requests.values())
                if not self._vs_pending_prefetch or prefetch_count >= self._MAX_PREFETCH_REQUESTS:
                    break
                mod_id, path = self._vs_pending_prefetch.pop(0)
                item = None
                if cached_preview(path, self._preview_size(), auto_transform=True, validate=False) is not None:
                    continue
            request = self._vs_image_requests.get(mod_id)
            if request is not None and request[2] == str(path):
                if item is not None:
                    self._vs_image_requests[mod_id] = (request[0], item, request[2])
                continue
            self._vs_image_request_id += 1
            request_id = self._vs_image_request_id
            self._vs_image_requests[mod_id] = (request_id, item, str(path))
            size = self._preview_size()
            self._pixmap_worker.enqueue(mod_id, str(path), size.width(), size.height(), request_id)
        self._vs_schedule_pixmaps()

    def _on_bg_pixmap_loaded(
        self,
        mod_id: str,
        path: str,
        request_id: int,
        result: DecodedPreview | None,
    ):
        """后台线程完成图片加载 → 主线程设置 QPixmap"""
        request = self._vs_image_requests.get(mod_id)
        if request is None or request[0] != request_id or request[2] != path:
            return
        self._vs_image_requests.pop(mod_id, None)
        self._vs_schedule_pixmaps()
        item = self._vs_card_cache.get(mod_id)
        if item is None or item is not request[1]:
            return
        mod = item._mod
        if mod.mod_id != mod_id or not self._same_path(mod.primary_preview or "", path):
            return
        if self._adult_mode == "hide" and mod.info.age_rating == AgeRating.X:
            item._preview_requested = False
            item._preview_resolved = True
            return
        if result is not None and not result.is_current(path):
            item._preview_resolved = False
            item._preview_requested = True
            item._cached_pixmap = None
            self._vs_pending_pixmap_items.insert(0, (mod_id, item))
            self._vs_schedule_pixmaps()
            return
        retain_viewed_preview(path, self._preview_size(), result, auto_transform=True)
        self._vs_apply_preview(
            item,
            result.image if result is not None else None,
            cache_generation=result.cache_token[2] if result is not None and result.cache_token else None,
        )

    @staticmethod
    def _vs_apply_preview(item: GalleryItem, qimage: object, *, cache_generation: int | None = None) -> None:
        """在界面线程合成含模糊填充和圆角的预览，图像无效时显示无预览状态。"""
        try:
            pixmap = QPixmap.fromImage(qimage) if qimage is not None else QPixmap()
        except (TypeError, RuntimeError):
            pixmap = QPixmap()
        if pixmap.isNull():
            item.set_no_preview()
            return
        # 后台已把图缩到工作尺寸（contain、未裁切）；主线程做模糊填充合成 + 圆角：
        # 完整看到整张预览图，比例不一致处由同图高斯模糊垫底，无 letterbox 黑边。
        item.set_pixmap(
            rendered_preview(
                qimage,
                GalleryItem.ITEM_WIDTH - 8,
                GalleryItem.ITEM_HEIGHT - 60,
                "contain",
                fill_blur_rounded,
                cache_generation=cache_generation,
            )
        )

    def _on_scroll_value_changed(self, _value: int):
        """滚动条变化时，延迟触发虚拟滚动更新"""
        if _value != self._vs_population_anchor_value:
            self._card_reveal.cancel()
        if (
            getattr(self, "_fresh_population", False)
            and self._vs_population_timer.isActive()
            and _value != self._vs_population_anchor_value
        ):
            self._fresh_population = False
            self._vs_population_timer.stop()
        self._vs_schedule_pixmaps()
        if not self._vs_scroll_timer.isActive():
            self._vs_scroll_timer.start()

    def hideEvent(self, event):
        """页面隐藏时停止卡片出现效果与首屏渐进填充。"""
        self._card_reveal.cancel()
        self._vs_population_timer.stop()
        super().hideEvent(event)

    def showEvent(self, event):
        """页面可见时，延迟校正布局（切换页面后 viewport 尺寸可能尚未更新）"""
        super().showEvent(event)
        if self._mods:
            self._vs_population_anchor_value = self._scroll_area.verticalScrollBar().value()
            self._fresh_population = True
        self._resize_timer.start()

    def _check_and_refresh_layout(self):
        """更新几何并补齐可视行，保留已有控件和图片。"""
        if not self._mods:
            self._resize_empty_label()
            return
        avail_w = self._scroll_area.viewport().width()
        if avail_w <= 100:
            avail_w = self.width() - 48
        new_cols = max(1, (avail_w - 24) // (GalleryItem.ITEM_WIDTH + 12))
        if new_cols != self._vs_cols:
            self._vs_cols = new_cols
            self._vs_total_rows = (len(self._mods) + new_cols - 1) // new_cols
            total_h = self._vs_total_rows * self._vs_row_h + 12
            self._scroll_content.setMinimumHeight(total_h)
            self._scroll_content.setMaximumHeight(total_h)
        self._vs_update_visible()

    def eventFilter(self, obj, event):
        """视口尺寸变化时合并安排延后的布局检查，避免同步布局事件递归。"""
        if obj is self._scroll_area.viewport() and event.type() == QEvent.Type.Resize:
            if not self._resize_timer.isActive():
                self._resize_timer.start()
        return super().eventFilter(obj, event)

    def _resize_empty_label(self) -> None:
        """调整空状态标签使其填满可视区域并居中"""
        if not self._mods:
            vp = self._scroll_area.viewport()
            w = vp.width() if vp.width() > 100 else self.width() - 48
            h = vp.height() if vp.height() > 100 else self.height() - 60
            self._empty_label.move(0, 0)
            self._empty_label.resize(w, h)
            self._loading_panel.setGeometry(0, 0, w, h)
            self._scroll_content.setMinimumHeight(h)
            self._scroll_content.setMaximumHeight(h)

    def resizeEvent(self, event):
        """窗口大小变化时，重新计算布局"""
        super().resizeEvent(event)
        if not self._resize_timer.isActive():
            self._resize_timer.start()

    def set_adult_mode(self, mode: str) -> None:
        """外部同步成人级预览模式（show|hide|blur）"""
        if mode not in ("show", "hide", "blur"):
            mode = "show"
        self._adult_mode = mode
        self._adult_mode_combo.blockSignals(True)
        idx = self._adult_mode_combo.findData(mode)
        if idx >= 0:
            self._adult_mode_combo.setCurrentIndex(idx)
        self._adult_mode_combo.blockSignals(False)
        self._refresh()

    def _on_adult_mode_changed(self, _index: int = 0) -> None:
        """画廊中切换成人级预览模式"""
        mode = self._adult_mode_combo.currentData() or "show"
        self._adult_mode = mode
        self._refresh()
        self.adult_mode_changed.emit(mode)

    def closeEvent(self, event) -> None:
        """确保后台图片线程先退出，再允许页面析构。"""
        self._card_reveal.cancel()
        self._vs_population_timer.stop()
        if self._pixmap_worker.isRunning():
            self._pixmap_worker.stop()
            self._pixmap_worker.wait(1000)
        super().closeEvent(event)
