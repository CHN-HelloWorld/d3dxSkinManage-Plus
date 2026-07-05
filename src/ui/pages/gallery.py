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
