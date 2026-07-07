"""固定格位的虚拟卡片网格，支持无限滚动和自动回填。

仅保留视口和相邻行的活动卡片，离屏卡片交还池中，进入新行时重绑数据。
同一行内的像素滚动无需遍历或布局卡片；边缘透明度由 ScrollEdgeFade 处理。
``card_binder`` 提供当前安装、选中等状态，也支持单独使用自定义工厂。
``iter_cards`` 只返回活动卡片，离屏条目的状态需保留在记录或页面中。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import (
    QEvent,
    QPoint,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtWidgets import (
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.services.gamebanana.models import GbModRecord
from src.ui import anim
from src.ui.smooth_scroll import SmoothScroller
from src.ui.widgets.card_pool import CardPool, suspend_card
from src.ui.widgets.card_reveal import CardRevealQueue
from src.ui.widgets.gb_mod_card import GbModCard
from src.ui.widgets.scroll_edge_fade import ScrollEdgeFade
from src.utils.qt_timers import call_later


class GbAdaptiveGrid(QWidget):
    """仅为可见行及相邻缓冲行保留控件的虚拟卡片网格。"""

    request_next_page = Signal()
    # 已有内容的分页请求通过信号显示加载条，首次加载由浏览视图统一处理。
    loading_more_started = Signal()
    loading_more_finished = Signal()

    SCROLL_IDLE_MS = 200  # 最后一次滚动后的空闲判定时间。
    PRELOAD_VIEWPORTS = 2  # 距底部小于指定视口高度倍数时预取下一页。
    VS_UPDATE_MS = 8  # 合并连续滚动事件，滚动过程中仍及时补齐新行
    VISIBLE_BUFFER_ROWS = 1  # 视口上下各保留一行缓冲
    MARGIN = 8  # 内容区四周留白
    SPACING = 8  # 卡片间距
    # 图片请求随滚动分批推进；数据预取比控件窗口多覆盖上下各一屏。
    IMG_BATCH_MS = 32
    IMG_BATCH_SIZE = 4
    IMAGE_PRELOAD_VIEWPORTS = 1
    # 列数变化时，可见卡片以指数缓动移动到新位置；连续调整宽度时更新同一组目标。
    REFLOW_FPS_MS = 16
    REFLOW_ALPHA = 0.30  # 每帧向目标位置移动的比例。
    REFLOW_SNAP_PX = 1  # 距目标 ≤ 此像素即吸附收尾

    def __init__(self, parent=None) -> None:
        """建立虚拟卡片池、图片预取和滚动空闲队列，连接可见区更新、重排及边缘渐隐。"""
        super().__init__(parent)
        self._has_more = False
        self._loading = False
        self._loading_more_shown = False

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("gbGridScroll")
        self.scroll_area.setWidgetResizable(True)
        # 滚动区保持透明，背景由页面提供。
        # 样式须限定到控件标识，避免向后代传播透明背景规则并覆盖卡片文本区底色。
        self.scroll_area.setStyleSheet("#gbGridScroll { border: none; background: transparent; }")
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content = QWidget()
        self._content.setObjectName("gbGridContent")
        # 使用控件标识限定透明规则，防止样式继承到卡片子控件。
        self._content.setStyleSheet("#gbGridContent { background: transparent; }")
        self.scroll_area.setWidget(self._content)
        # 视口尺寸变化时重新计算列数和内容高度，并布局可见卡片。
        self.scroll_area.viewport().installEventFilter(self)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.scroll_area)

        # 虚拟滚动模型
        self._records: list[GbModRecord] = []
        self._card_factory: Callable | None = None
        self._card_binder: Callable | None = None
        self._image_prefetcher: Callable | None = None
        self._image_prefetch_canceller: Callable | None = None
        self._prefetch_bounds: tuple[int, int] | None = None
        self._prefetch_requested: dict[int, GbModRecord] = {}
        self._prefetch_queue: list[int] = []
        self._image_requested: dict[int, QWidget] = {}
        self._cards: dict[int, QWidget] = {}  # 记录下标对应可见行及缓冲行中的卡片控件。
        self._card_pool = CardPool()
        self._visible_window: tuple | None = None
        self._card_w = GbModCard.CARD_WIDTH
        self._card_h = GbModCard.CARD_HEIGHT
        self._cols = 1
        self._row_h = self._card_h + self.SPACING
        self._total_rows = 0
        self._last_viewport_width = -1
        self._pending_reveal = False  # 数据到达时为新建的可见卡片安排交错淡入。
        self._updating_items = False

        # 合并滚动事件，但不反复推迟已安排的更新，避免快速滚动露出空行。
        self._vs_timer = QTimer(self)
        self._vs_timer.setSingleShot(True)
        self._vs_timer.setInterval(self.VS_UPDATE_MS)
        self._vs_timer.timeout.connect(self._update_visible)

        # 合并请求而不延后已有截止时间，保证持续滚动时图片请求仍能执行。
        self._img_timer = QTimer(self)
        self._img_timer.setSingleShot(True)
        self._img_timer.setInterval(self.IMG_BATCH_MS)
        self._img_timer.timeout.connect(self._load_visible_images)

        # 列数变化后按帧更新卡片位置，所有卡片到达目标后停止。
        self._reflow_targets: dict = {}
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setInterval(self.REFLOW_FPS_MS)
        self._reflow_timer.timeout.connect(self._reflow_tick)

        # 滚动期间延迟执行分页内容提交，停止滚动后继续。
        self._scrolling = False
        self._scroll_idle = QTimer(self)
        self._scroll_idle.setSingleShot(True)
        self._scroll_idle.setInterval(self.SCROLL_IDLE_MS)
        self._scroll_idle.timeout.connect(self._clear_scrolling)
        self._idle_jobs: list = []

        self.scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll)
        # 通过指数缓动更新滚动位置。
        SmoothScroller.attach(self.scroll_area)
        self._scroll_edge_fade = ScrollEdgeFade(
            self.scroll_area,
            lambda cards=self._cards: cards.values(),
        )
        self._card_reveal = CardRevealQueue(
            self, self._scroll_edge_fade, lambda card, index: self._cards.get(index) is card
        )

    # 滚动状态
    def _on_scroll(self, _value: int = 0) -> None:
        """滚动时取消入场揭示，标记滚动状态并合并可见区更新，批量数据更新期间忽略回调。"""
        if self._updating_items:
            return
        self._card_reveal.cancel()
        self._scrolling = True
        self._pending_reveal = False  # 滚动开始后停止为后续卡片安排入场淡入。
        self._scroll_idle.start()
        if not self._vs_timer.isActive():
            self._vs_timer.start()

    def _clear_scrolling(self) -> None:
        """结束滚动状态并隔离执行当前空闲任务批次，随后补齐可见卡片。"""
        self._scrolling = False
        # 先取出待执行任务并清空队列，执行期间新加入的任务留给下一轮。
        jobs, self._idle_jobs = self._idle_jobs, []
        for job in jobs:
            try:
                job()
            except Exception:
                pass
        # 滚动结束后重新检查可见范围并补齐卡片。
        self._update_visible()

    def is_scrolling(self) -> bool:
        """返回网格是否仍处于等待滚动空闲的状态。"""
        return self._scrolling

    def run_when_idle(self, fn) -> None:
        """滚动期间将任务延迟至停止后执行，空闲时立即执行。"""
        if self._scrolling:
            self._idle_jobs.append(fn)
        else:
            fn()

    # 几何 / 布局
    def _viewport_width(self) -> int:
        """返回视口有效宽度，布局尚未提供有效值时使用网格宽度。"""
        w = self.scroll_area.viewport().width()
        return w if w > 0 else self.width()

    def _columns_for(self, width: int) -> int:
        """扣除边距后按卡片尺寸与间距计算列数，结果至少为一列。"""
        usable = width - self.MARGIN * 2
        step = self._card_w + self.SPACING
        if step <= 0:
            return 1
        return max(1, (usable + self.SPACING) // step)

    def _relayout(self) -> None:
        """根据视口宽度和全部记录数量计算列数及内容高度。

        滚动范围涵盖全部记录，确保滚动到未创建卡片的位置时可以加载对应行并触发分页。
        """
        width = self._viewport_width()
        self._cols = self._columns_for(width)
        self._row_h = self._card_h + self.SPACING
        n = len(self._records)
        self._total_rows = (n + self._cols - 1) // self._cols if self._cols else 0
        if self._total_rows > 0:
            total_h = self.MARGIN * 2 + self._total_rows * self._card_h + (self._total_rows - 1) * self.SPACING
        else:
            total_h = 0
        total_h = max(0, total_h)
        # 滚动区管理内容宽度，内容高度须显式设置以形成完整滚动范围。
        self._content.setMinimumHeight(total_h)
        self._content.setMaximumHeight(total_h)

    def _cell_pos(self, idx: int) -> tuple[int, int]:
        """把记录下标转换为当前列数下的卡片左上角坐标。"""
        row = idx // self._cols
        col = idx % self._cols
        x = self.MARGIN + col * (self._card_w + self.SPACING)
        y = self.MARGIN + row * (self._card_h + self.SPACING)
        return x, y
