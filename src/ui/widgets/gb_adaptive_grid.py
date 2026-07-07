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

    def _index_window(self) -> tuple[int, int]:
        """返回固定行窗口的首末下标，包含部分可见行和上下缓冲。"""
        bar = self.scroll_area.verticalScrollBar()
        scroll_y = bar.value()
        vh = self.scroll_area.viewport().height() or 1
        row_h = self._row_h or 1
        first_row = max(0, scroll_y // row_h - self.VISIBLE_BUFFER_ROWS)
        last_row = min(self._total_rows - 1, scroll_y // row_h + (vh + row_h - 1) // row_h + self.VISIBLE_BUFFER_ROWS)
        n = len(self._records)
        first_idx = first_row * self._cols
        last_idx = min(n - 1, (last_row + 1) * self._cols - 1)
        return first_idx, last_idx

    def _update_visible(self) -> None:
        """跨行时复用卡片，同一窗口内只检查分页。"""
        if not self.isVisible():
            return  # 网格可绘制前保留显示请求。
        if not self._records or self._card_factory is None or self._total_rows <= 0:
            return
        first_idx, last_idx = self._index_window()
        if last_idx < 0:
            return
        prefetch_changed = self._sync_prefetch_window()
        window = (first_idx, last_idx, self._cols, self._card_w, self._card_h)
        if window == self._visible_window:
            self._pending_reveal = False
            self._maybe_emit_next()
            if prefetch_changed:
                self._rebuild_prefetch_queue()
                self._schedule_images()
            return
        # 先回收再分配，跨越很远的滚动也只需一屏控件。
        for idx in list(self._cards):
            if idx < first_idx or idx > last_idx:
                card = self._cards.pop(idx)
                self._image_requested.pop(idx, None)
                # 回收控件前保留临近视口的有效图片工作，转为独立数据预取。
                if getattr(card, "_thumb_loaded", False) and self._prefetch_bounds is not None:
                    if self._prefetch_bounds[0] <= idx <= self._prefetch_bounds[1]:
                        self._request_prefetch(idx)
                self._reflow_targets.pop(card, None)
                if self._card_binder is not None and type(card) is GbModCard:
                    card.cancel_thumbnail()
                    self._card_pool.release(card)
                else:
                    self._dispose_card(card)
        # 2) 创建 / 摆放可视卡片
        reveal = self._pending_reveal and not self._scrolling
        viewport = self.scroll_area.viewport().rect().translated(0, self.scroll_area.verticalScrollBar().value())
        for i in range(first_idx, last_idx + 1):
            card = self._cards.get(i)
            is_new = card is None
            if card is None:
                card = self._card_pool.take(GbModCard) if self._card_binder is not None else None
                try:
                    if card is None:
                        card = self._card_factory(self._records[i])
                        card.setParent(self._content)
                    else:
                        self._card_binder(card, self._records[i])
                except Exception:
                    if card is not None:
                        self._dispose_card(card)
                    continue
                self._cards[i] = card
            # 重排动画中的卡片由动画驱动，避免直接定位使动画提前结束。
            if card not in self._reflow_targets:
                x, y = self._cell_pos(i)
                if card.x() != x or card.y() != y:
                    card.move(x, y)
            if reveal and is_new and card.geometry().intersects(viewport):
                self._card_reveal.add(card, i)
            if card.isHidden():
                card.show()
        self._visible_window = window if len(self._cards) == last_idx - first_idx + 1 else None
        self._pending_reveal = False
        self._scroll_edge_fade.refresh()
        # 4) 同时判断是否需要预拉下一页
        self._maybe_emit_next()
        self._rebuild_prefetch_queue()
        self._schedule_images()

    def _schedule_images(self) -> None:
        """网格可见时安排图片加载批次，保留已经启动的定时截止时间。"""
        if not self._img_timer.isActive() and self.isVisible():
            self._img_timer.start()

    def _visible_image_range(self) -> tuple[int, int]:
        """根据滚动位置和视口高度返回覆盖可见行的记录下标闭区间。"""
        y = self.scroll_area.verticalScrollBar().value()
        height = self.scroll_area.viewport().height() or 1
        first = y // self._row_h * self._cols
        last = ((y + height - 1) // self._row_h + 1) * self._cols - 1
        return first, min(len(self._records) - 1, last)

    def _sync_prefetch_window(self) -> bool:
        """更新视口周围的预取范围并取消移出范围的请求，返回范围是否发生变化。"""
        if self._image_prefetcher is None:
            return False
        y = self.scroll_area.verticalScrollBar().value()
        height = self.scroll_area.viewport().height() or 1
        padding = height * self.IMAGE_PRELOAD_VIEWPORTS
        first = max(0, (y - padding) // self._row_h) * self._cols
        last = min(len(self._records) - 1, ((y + height + padding) // self._row_h + 1) * self._cols - 1)
        bounds = first, last
        if bounds == self._prefetch_bounds:
            return False
        self._prefetch_bounds = bounds
        for index in list(self._prefetch_requested):
            if not first <= index <= last:
                record = self._prefetch_requested.pop(index)
                if self._image_prefetch_canceller is not None:
                    self._image_prefetch_canceller(record)
        return True

    def _rebuild_prefetch_queue(self) -> None:
        """将预取范围内尚无卡片及请求的记录排队，优先处理靠近可见区的图片。"""
        if self._prefetch_bounds is None or self._image_prefetcher is None:
            self._prefetch_queue = []
            return
        first, last = self._prefetch_bounds
        visible_first, visible_last = self._visible_image_range()
        self._prefetch_queue = sorted(
            (
                index
                for index in range(first, last + 1)
                if index not in self._cards and index not in self._prefetch_requested
            ),
            key=lambda index: min(abs(index - visible_first), abs(index - visible_last)),
        )

    def _request_prefetch(self, index: int) -> bool:
        """提交尚未登记的图片预取，回调明确拒绝时返回 False 并保留重试机会。"""
        if index in self._prefetch_requested or self._image_prefetcher is None:
            return True
        record = self._records[index]
        if self._image_prefetcher(record) is False:
            return False
        self._prefetch_requested[index] = record
        return True

    def _load_visible_images(self) -> None:
        """小批提交当前可见卡，再准备邻近记录；滚动中保持原定截止时间。"""
        if not self._cards or self._total_rows <= 0 or not self.isVisible():
            return
        first, last = self._visible_image_range()
        candidates = sorted(
            self._cards,
            key=lambda index: (
                not first <= index <= last,
                index if first <= index <= last else min(abs(index - first), abs(index - last)),
            ),
        )
        remaining = self.IMG_BATCH_SIZE
        pending = False
        for idx in candidates:
            card = self._cards[idx]
            if self._image_requested.get(idx) is card or getattr(card, "_thumb_loaded", False):
                continue
            load = getattr(card, "load_thumbnail", None)
            if callable(load):
                if remaining == 0:
                    pending = True
                    break
                try:
                    load()
                except RuntimeError:
                    pass
                remaining -= 1
            self._image_requested[idx] = card
        if not pending:
            while remaining and self._prefetch_queue:
                index = self._prefetch_queue[0]
                if not self._request_prefetch(index):
                    break
                self._prefetch_queue.pop(0)
                remaining -= 1
        if pending or self._prefetch_queue:
            self._schedule_images()

    def _reposition_cached(self) -> None:
        """立即将卡片放置到当前格位，供首次布局及禁用动画时使用。"""
        self._stop_reflow()
        for idx, card in self._cards.items():
            x, y = self._cell_pos(idx)
            card.move(x, y)

    def _animate_reposition(self) -> None:
        """列数变化后将可见卡片缓动至目标格位。"""
        targets: dict = {}
        for idx, card in self._cards.items():
            x, y = self._cell_pos(idx)
            try:
                cur = card.pos()
            except RuntimeError:
                continue
            if cur.x() == x and cur.y() == y:
                continue
            targets[card] = QPoint(x, y)
        if not targets:
            return
        self._reflow_targets = targets
        if not self._reflow_timer.isActive():
            self._reflow_timer.start()

    def _reflow_tick(self) -> None:
        """推进一帧重排动画，全部卡片到达目标后停止。"""
        if not self._reflow_targets:
            self._reflow_timer.stop()
            return
        alpha = self.REFLOW_ALPHA
        snap = self.REFLOW_SNAP_PX
        done = []
        for card, tgt in self._reflow_targets.items():
            try:
                cur = card.pos()
            except RuntimeError:
                done.append(card)
                continue
            dx = tgt.x() - cur.x()
            dy = tgt.y() - cur.y()
            if abs(dx) <= snap and abs(dy) <= snap:
                card.move(tgt)
                done.append(card)
                continue
            card.move(cur.x() + round(dx * alpha), cur.y() + round(dy * alpha))
        for c in done:
            self._reflow_targets.pop(c, None)
        if not self._reflow_targets:
            self._reflow_timer.stop()

    def _stop_reflow(self) -> None:
        """停止卡片重排定时器并丢弃剩余位置目标。"""
        if self._reflow_timer.isActive():
            self._reflow_timer.stop()
        self._reflow_targets = {}

    # 事件
    def eventFilter(self, obj, event):
        """视口宽度变化时重算列数并调整卡片位置，尺寸变化后延后检查可见区及分页回填。"""
        if obj is self.scroll_area.viewport() and event.type() == QEvent.Type.Resize:
            w = event.size().width()
            if w != self._last_viewport_width:
                first = self._last_viewport_width < 0
                self._last_viewport_width = w
                self._relayout()
                # 首次获得有效宽度时直接布局，后续宽度变化再使用重排动画。
                if first or not anim.is_enabled() or not self._cards:
                    self._reposition_cached()
                else:
                    self._animate_reposition()
            # 高度变化后延迟检查回填，使滚动范围先完成更新。
            call_later(0, self, self._update_visible)
            call_later(0, self, self._maybe_fill_viewport)
        return super().eventFilter(obj, event)

    def showEvent(self, event):
        """页面重新显示时按当前视口恢复布局和图片请求，并补查隐藏期间未执行的回填。"""
        # QStackedWidget 切到可见时按当前 viewport 宽重排（切换期间可能没发生 resize 事件）。
        super().showEvent(event)
        if self._records:
            self._scrolling = False
            self._scroll_idle.stop()
            # 重新显示页面属于恢复，仅处理隐藏期间新产生的显示请求。
            self._visible_window = None
            self._updating_items = True
            try:
                self._relayout()
                self._reposition_cached()
                self._update_visible()
            finally:
                self._updating_items = False
            self._schedule_images()
            # 隐藏期间不进行视口回填，重新显示时须再检查一次。
            call_later(0, self, self._maybe_fill_viewport)

    def hideEvent(self, event):
        """隐藏网格时取消卡片入场揭示，保留数据供返回页面时恢复。"""
        self._card_reveal.cancel()
        super().hideEvent(event)

    # 数据
    def set_items(
        self,
        records: list[GbModRecord],
        *,
        has_more: bool,
        card_factory: Callable,
        card_binder: Callable | None = None,
        image_prefetcher: Callable | None = None,
        image_prefetch_canceller: Callable | None = None,
        append: bool = False,
    ) -> None:
        """替换或追加资源记录及绑定回调，结束加载状态、更新可见卡片并安排视口回填检查。"""
        if not append:
            self._destroy_all_cards()
            self._records = list(records)
            self._scrolling = False
            self._scroll_idle.stop()
        else:
            self._records.extend(records)
        self._card_factory = card_factory
        self._card_binder = card_binder
        if not append or image_prefetcher is not None:
            self._image_prefetcher = image_prefetcher
            self._image_prefetch_canceller = image_prefetch_canceller
            self._prefetch_bounds = None
        # 仅为非滚动状态下新增的可视卡片安排入场动画，返回页面时不重播。
        self._pending_reveal = not self._scrolling
        self._has_more = has_more
        self._loading = False
        # 数据到达后收起分页加载条。
        self._updating_items = True
        try:
            self._emit_loading_more(False)
            self._relayout()
            self._update_visible()
        finally:
            self._updating_items = False
        # 本批布局完成后检查内容是否覆盖视口，未覆盖时再请求一页。
        call_later(0, self, self._maybe_fill_viewport)

    def append_items(
        self,
        records,
        *,
        has_more,
        card_factory,
        card_binder=None,
        image_prefetcher=None,
        image_prefetch_canceller=None,
    ) -> None:
        """沿用统一数据提交入口追加一页记录，保留已有卡片和数据。"""
        self.set_items(
            records,
            has_more=has_more,
            card_factory=card_factory,
            card_binder=card_binder,
            image_prefetcher=image_prefetcher,
            image_prefetch_canceller=image_prefetch_canceller,
            append=True,
        )

    @staticmethod
    def _dispose_card(card: QWidget) -> None:
        """取消卡片可选的缩略图请求，暂停其交互及动画后解除父对象并延后销毁。"""
        cancel = getattr(card, "cancel_thumbnail", None)
        if callable(cancel):
            cancel()
        suspend_card(card)
        card.setParent(None)
        card.deleteLater()
