"""显示导入、纳管和导出的批次进度。

独立模式定位于父内容区右下角，不参与布局尺寸分配；嵌入模式由宿主布局管理。
调用方推送批次和单项状态，面板不持有工作线程。折叠时显示总进度及操作按钮，
展开后显示可滚动的逐项明细。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QCursor, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui import anim, icons, theme
from src.ui.widgets.ring_progress import RingProgressButton
from src.utils.i18n import N_
from src.utils.i18n import t as tr

# 常量

PANEL_WIDTH = 340
ITEM_DOT_PX = 7  # 状态点直径
SLIDE_MS = 200
EXPAND_MS = 190  # 明细区展开/收起动画时长
LIST_MAX_HEIGHT = 200  # 明细区高度上限；超出即出滚动条
RESULT_HOLD_MS = 3500  # 成功后自动收起的等待时间。
# 任务浮层不占键盘焦点，按鼠标是否进入过选择终态自动收起的等待时间。
DISMISS_AFTER_LEAVE_MS = 800
DISMISS_UNTOUCHED_MS = 3500

# 供调用方传递单项状态的字符串常量。
ST_QUEUED = "queued"
ST_RUNNING = "running"
ST_DONE = "done"
ST_FAILED = "failed"
ST_ROLLED_BACK = "rolled_back"

_DOT_COLORS = {
    ST_QUEUED: "text.disabled",
    ST_RUNNING: "accent",
    ST_DONE: "success",
    ST_FAILED: "danger",
    ST_ROLLED_BACK: "warning",
}


# 省略号标签


class _ElideLabel(QLabel):
    """按可用宽度省略尾部文本的单行标签。

    保留完整文本，尺寸变化时重新计算显示内容，避免固定宽度面板直接裁切长名称。
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        """保存完整文字并允许标签缩小，按当前可用宽度生成显示文本。"""
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self._sync()

    def setText(self, text: str) -> None:
        """更新完整文字缓存，并重新计算尾部省略结果。"""
        self._full = text or ""
        self._sync()

    def full_text(self) -> str:
        """返回未经省略的原始文本，供提示或状态处理使用。"""
        return self._full

    def _sync(self) -> None:
        """按字体度量和当前宽度更新显示文本，尚无可用宽度时保留完整内容。"""
        avail = max(0, self.width())
        if avail <= 0:
            super().setText(self._full)
            return
        fm = QFontMetrics(self.font())
        super().setText(fm.elidedText(self._full, Qt.TextElideMode.ElideRight, avail))

    def resizeEvent(self, event) -> None:
        """尺寸变化后重新计算单行文字的省略位置。"""
        super().resizeEvent(event)
        self._sync()


# 逐项行


class _ItemRow(QWidget):
    """显示单项任务名称、状态标记及状态文案。"""

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        """创建初始排队状态的任务行，使名称宽度让位于状态文案。"""
        super().__init__(parent)
        self._status = ST_QUEUED
        self._dot_color = "text.disabled"
        self._icon_name = ""
        self._icon_color = ""

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(7)

        self._dot = QLabel()
        self._dot.setFixedSize(ITEM_DOT_PX, ITEM_DOT_PX)
        lay.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)

        # 保持水平尺寸策略为 Ignored，使完整文本宽度不参与最小宽度计算，
        # 从而让标签按面板实际分配的宽度省略文本。
        self._name = _ElideLabel(name)
        lay.addWidget(self._name, 1)

        self._state_label = QLabel(tr("排队中"))
        self._state_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self._state_label, 0)

        self._apply_style()

    def set_status(self, status: str, text: str = "", *, done_text: str = N_("已完成")) -> None:
        """记录任务状态，选择默认或指定文案，并同步状态标记与文字颜色。"""
        self._status = status
        self._dot_color = _DOT_COLORS.get(status, "text.disabled")
        label = text or {
            ST_QUEUED: tr("排队中"),
            ST_DONE: tr(done_text),
            ST_FAILED: tr("失败"),
            ST_ROLLED_BACK: tr("已回滚"),
        }.get(status, "")
        self._state_label.setText(label)
        self._apply_style()

    def _apply_style(self) -> None:
        """按任务状态及可选自定义图标更新状态标记、名称和文案样式。"""
        if self._icon_name:
            self._dot.setFixedSize(14, 14)
            self._dot.setStyleSheet("background:transparent;")
            self._dot.setPixmap(icons.pixmap(self._icon_name, 14, self._icon_color or self._dot_color))
        else:
            self._dot.setFixedSize(ITEM_DOT_PX, ITEM_DOT_PX)
            self._dot.clear()
            dot_hex = theme.color(self._dot_color)
            self._dot.setStyleSheet(f"background:{dot_hex}; border-radius:{ITEM_DOT_PX // 2}px;")
        name_hex = theme.color("text.primary" if self._status == ST_RUNNING else "text.secondary")
        st_hex = theme.color(_DOT_COLORS.get(self._status, "text.tertiary"))
        self._name.setStyleSheet(f"color:{name_hex}; font-size:12px; background:transparent;")
        self._state_label.setStyleSheet(f"color:{st_hex}; font-size:11px; background:transparent;")

    def retheme(self) -> None:
        """使用当前主题重新应用任务行的状态样式。"""
        self._apply_style()

    def set_icon(self, name: str, color_token: str = "") -> None:
        """设置任务行的自定义状态图标及可选色标，并立即刷新显示。"""
        self._icon_name, self._icon_color = name, color_token
        self._apply_style()


# 面板主体


class TaskCenterStack:
    """同一父容器内多张任务浮层的右下角堆叠排版器。

    面板按进入顺序由下向上排列；一个面板收起后，其上方面板下移补位。
    仅管理纵向偏移，保留面板层级和内容。面板通过 ``note_shown / note_hidden``
    报告显隐状态，通过 ``offset_for`` 查询位置偏移。``_order`` 索引 0 对应最早进入的底部面板。
    """

    _GAP = 10  # 相邻两张浮层的垂直间距

    def __init__(self) -> None:
        """初始化面板显示顺序及明细高度协调的重入保护状态。"""
        self._order: list[TaskCenterPanel] = []
        self._fitting_details = False

    def note_shown(self, panel: TaskCenterPanel) -> None:
        """显示面板时加入堆叠序列末尾。"""
        if panel not in self._order:
            self._order.append(panel)
        self.fit_details(entering=panel)

    def note_hidden(self, panel: TaskCenterPanel) -> None:
        """隐藏面板后移出序列，并更新其他面板的位置。"""
        if panel in self._order:
            self._order.remove(panel)
            # 先收起空槽位，避免列表扩展与位置动画同时修改几何。
            self.relayout(fit=False)
            moving = False
            for other in self._order:
                move = getattr(other, "_move_anim", None)
                try:
                    if move is not None and move.state() != QPropertyAnimation.State.Stopped:
                        move.finished.connect(self._finish_reflow)
                        moving = True
                except RuntimeError:
                    pass
            if not moving:
                self.fit_details()

    def _finish_reflow(self) -> None:
        """等待所有面板位移动画结束后重新分配明细区域高度。"""
        for panel in self._order:
            move = getattr(panel, "_move_anim", None)
            try:
                if move is not None and move.state() != QPropertyAnimation.State.Stopped:
                    return
            except RuntimeError:
                pass
        self.fit_details()

    def offset_for(self, panel: TaskCenterPanel) -> int:
        """返回当前面板下方可见面板占用的总高度。"""
        total = 0
        for other in self._order:
            if other is panel:
                break
            try:
                if other.isVisible():
                    total += other.height() + self._GAP
            except RuntimeError:
                continue  # 面板已被销毁，忽略
        return total

    def relayout(self, *, animate: bool = True, fit: bool = True) -> None:
        """按当前顺序重新定位全部面板。

        逐帧跟随展开或收起动画时直接设置位置，避免连续创建位移动画相互中断。
        """
        if fit:
            self.fit_details(animate=animate)
        if any(panel._list_anim is not None for panel in self._order):
            animate = False
        for panel in list(self._order):
            try:
                panel.settle_position(animate=animate)
            except RuntimeError:
                self._order.remove(panel)

    def fit_details(self, *, animate: bool = True, entering=None) -> None:
        """共享可用详情空间，避免多个展开卡片重叠。"""
        if self._fitting_details:
            return
        self._fitting_details = True
        try:
            panels = []
            for panel in list(self._order):
                try:
                    if panel.isVisible() or panel is entering:
                        panels.append(panel)
                except RuntimeError:
                    self._order.remove(panel)
            if not panels:
                return
            parent = panels[0].parentWidget()
            if parent is None:
                return
            fixed = 0
            wanted = {}
            for panel in panels:
                panel.adjustSize()
                visible_list = 0 if panel._list_scroll.isHidden() else panel._list_scroll.height()
                fixed += panel.height() - visible_list
                if panel._is_expanded and panel._divider.isHidden():
                    fixed += panel._divider.height()
                wanted[panel] = min(panel._content_height(), LIST_MAX_HEIGHT) if panel._is_expanded else 0
            room = max(0, parent.height() - 2 * panels[0]._MARGIN - fixed - self._GAP * (len(panels) - 1))
            targets = dict.fromkeys(panels, 0)
            pending = [panel for panel in panels if wanted[panel]]
            while pending:
                share, extra = divmod(room, len(pending))
                small = [panel for panel in pending if wanted[panel] <= share]
                if not small:
                    for index, panel in enumerate(pending):
                        targets[panel] = share + (index < extra)
                    break
                for panel in small:
                    targets[panel] = wanted[panel]
                    room -= wanted[panel]
                    pending.remove(panel)
            for panel, target in targets.items():
                if target == panel._list_target and (
                    panel._list_anim is not None or panel._list_scroll.maximumHeight() == target
                ):
                    continue
                panel._animate_list_height(target, animate=animate)
        finally:
            self._fitting_details = False

    def raise_all(self) -> None:
        """提升可见面板的层级，避免被页面切换或覆盖层遮挡。"""
        for panel in list(self._order):
            try:
                if panel.isVisible():
                    panel.raise_()
            except RuntimeError:
                self._order.remove(panel)


class TaskCenterPanel(QFrame):
    """非模态批次任务面板。

    start_batch 开始批次，update_item 和 update_progress 更新状态，finish 完成批次。
    通过信号向宿主请求停止、暂停或继续，具体任务调度由宿主处理。
    """

    stop_requested = Signal()
    pause_requested = Signal()
    resume_requested = Signal()

    _MARGIN = 14  # 距父容器右边 / 下边的间距
    _SHADOW_BLUR = 20  # 估算阴影占用高度（用于避遮挡）

    def __init__(
        self,
        parent: QWidget,
        *,
        embedded: bool = False,
        stack: TaskCenterStack | None = None,
    ) -> None:
        """配置嵌入模式及面板堆叠关系。

        嵌入模式由宿主管理外观、布局和显隐；独立模式支持右下角定位及显示动画。
        stack 提供多面板位置管理，未传入时单独定位。
        """
        super().__init__(parent)
        self.setObjectName("taskCenterPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._embedded = bool(embedded)
        self._stack = None if self._embedded else stack
        if not self._embedded:
            self.setFixedWidth(PANEL_WIDTH)
            # 独立于父布局定位。
            self.hide()

        self._has_pause = False
        self._paused = False
        self._is_expanded = False
        self._total_items = 0
        self._items: dict[int, _ItemRow] = {}  # 索引映射到明细行控件。
        self._failed = False  # 是否有失败项（失败时不自动消失）
        # 每批任务通过 start_batch 设置业务文案，默认用于纳管和导入。
        self._stop_tooltip = tr("停止纳管并恢复原文件")
        self._cancelled_title = tr("已停止纳管")
        self._done_text = tr("已纳管")
        self._pause_tooltip = tr("暂停")
        self._resume_tooltip = tr("继续")

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._slide_out)

        # 终止状态按鼠标交互决定收起时间，与成功状态的固定延迟分别管理。
        self._dismiss_armed = False
        self._dismiss_touched = False  # 本批终止后鼠标是否进过浮层
        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self._on_dismiss_timeout)

        self._anim: QPropertyAnimation | None = None
        self._list_anim: QPropertyAnimation | None = None
        self._list_target = 0

        self._build_ui()
        self._apply_style()

    # 构建 UI

    def _build_ui(self) -> None:
        """构建总进度、暂停停止操作和默认折叠的明细列表，向宿主转发任务控制请求。"""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 头部行。
        head = QWidget()
        head.setObjectName("tcHead")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(10, 9, 8, 9)
        hl.setSpacing(10)

        self._ring = RingProgressButton(size=34, line_width=3, parent=head)
        self._ring.set_icon("actions/pause", "text.on_accent")
        self._ring.set_indeterminate(True)
        self._ring.set_progress(-1)
        self._ring.setToolTip(tr("暂停"))
        self._ring.clicked.connect(self._on_ring_clicked)
        hl.addWidget(self._ring, 0, Qt.AlignmentFlag.AlignVCenter)

        txt_col = QVBoxLayout()
        txt_col.setSpacing(1)
        txt_col.setContentsMargins(0, 0, 0, 0)
        self._title = _ElideLabel(tr("正在处理…"))
        self._title.setObjectName("tcTitle")
        self._detail = _ElideLabel("")
        self._detail.setObjectName("tcDetail")
        txt_col.addWidget(self._title)
        txt_col.addWidget(self._detail)
        hl.addLayout(txt_col, 1)

        self._stop_btn = _IconButton("actions/stop", "danger", parent=head)
        self._stop_btn.setToolTip(self._stop_tooltip)
        self._stop_btn.clicked.connect(self.stop_requested)
        hl.addWidget(self._stop_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._expand_btn = _IconButton("actions/chevron-down", "text.secondary", parent=head)
        self._expand_btn.setToolTip(tr("展开明细"))
        self._expand_btn.clicked.connect(self._toggle_expand)
        hl.addWidget(self._expand_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        outer.addWidget(head)

        # 仅在明细展开时显示的分隔线。
        self._divider = QFrame()
        self._divider.setObjectName("tcDivider")
        self._divider.setFixedHeight(1)
        self._divider.hide()
        outer.addWidget(self._divider)

        # 明细列表。
        self._list_scroll = QScrollArea()
        self._list_scroll.setObjectName("tcListScroll")
        self._list_scroll.setWidgetResizable(True)
        self._list_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._list_scroll.setFrameShape(QFrame.Shape.NoFrame)
        # 起始收起态：上限 0，它同时是首次展开动画的起点（见 _animate_list_height）。
        self._list_scroll.setMaximumHeight(0)
        self._list_scroll.hide()

        self._list_container = QWidget()
        self._list_container.setObjectName("tcListContainer")
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 4, 0, 4)
        self._list_layout.setSpacing(0)
        self._list_scroll.setWidget(self._list_container)

        outer.addWidget(self._list_scroll)

    # 样式

    def _apply_style(self) -> None:
        """按嵌入模式决定是否绘制面板外壳，并同步头部、明细行及滚动条主题。"""
        bg = theme.color("bg.card")
        stroke = theme.color("stroke.card")
        divider = theme.color("stroke.divider")
        r = theme.radius("large")
        primary = theme.color("text.primary")
        tertiary = theme.color("text.tertiary")
        # 嵌入模式：不画卡片外壳（宿主对话框已有背景与描边），只留内容层。
        shell = (
            "#taskCenterPanel { background:transparent; border:none; }"
            if self._embedded
            else f"#taskCenterPanel {{ background:{bg}; border:1px solid {stroke}; border-radius:{r}px; }}"
        )
        self.setStyleSheet(
            shell + f"#tcHead {{ background:transparent; }}"
            f"#tcTitle {{ color:{primary}; font-size:12px; font-weight:600;"
            f" background:transparent; }}"
            f"#tcDetail {{ color:{tertiary}; font-size:11px; background:transparent; }}"
            f"#tcDivider {{ background:{divider}; border:none; }}"
            f"#tcListContainer {{ background:transparent; }}"
            f"#tcListScroll {{ background:transparent; }}"
            # 使用较宽且对比明显的滚动条，使溢出内容的操作入口清晰可见。
            f"#tcListScroll QScrollBar:vertical {{ width:8px; border:none;"
            f" background:transparent; margin:2px 1px 2px 0; }}"
            f"#tcListScroll QScrollBar::handle:vertical {{"
            f" background:{theme.color('scrollbar.handle')};"
            f" border-radius:3px; min-height:24px; }}"
            f"#tcListScroll QScrollBar::handle:vertical:hover {{"
            f" background:{theme.color('scrollbar.handle.hover')}; }}"
            f"#tcListScroll QScrollBar::add-line:vertical,"
            f"#tcListScroll QScrollBar::sub-line:vertical {{ height:0; }}"
            f"#tcListScroll QScrollBar::add-page:vertical,"
            f"#tcListScroll QScrollBar::sub-page:vertical {{ background:transparent; }}"
        )
        # 每行子控件的颜色由 _ItemRow._apply_style 独立管理。
        for row in self._items.values():
            row.retheme()

    # 对外 API

    def start_batch(
        self,
        total: int,
        title: str,
        *,
        has_pause: bool = False,
        stop_tooltip: str = N_("停止纳管并恢复原文件"),
        cancelled_title: str = N_("已停止纳管"),
        done_text: str = N_("已纳管"),
        pause_tooltip: str = N_("暂停"),
        resume_tooltip: str = N_("继续"),
    ) -> None:
        """开始一批作业，total 为零时使用不确定进度。

        文案参数用于区分纳管、导入和导出，各调用方提供符合实际操作的终止提示。
        """
        self._hide_timer.stop()
        # 开始新批次前取消上一批的自动收起任务。
        self._disarm_focus_dismiss()
        self._dismiss_touched = False
        self._failed = False
        self._has_pause = has_pause
        self._paused = False
        self._total_items = total
        self._stop_tooltip = tr(stop_tooltip)
        self._cancelled_title = tr(cancelled_title)
        self._done_text = tr(done_text)
        self._pause_tooltip = tr(pause_tooltip)
        self._resume_tooltip = tr(resume_tooltip)
        self._stop_btn.setToolTip(self._stop_tooltip)
        self._stop_btn.setAccessibleName(self._stop_tooltip)
        self._items.clear()
        # 清空列表控件
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        # 环态：无确定进度时走不确定旋转
        self._ring.set_progress(-1)
        self._ring.set_ring_color("accent")
        if total > 0:
            self._ring.set_indeterminate(False)
            self._ring.set_progress(0)
        else:
            self._ring.set_indeterminate(True)

        # has_pause 决定环心图标
        if has_pause:
            self._ring.set_icon("actions/pause", "text.on_accent")
            self._ring.setEnabled(True)
            self.set_paused(False)
        else:
            # 仅进度展示，环不可点
            self._ring.set_icon("", "")
            self._ring.setEnabled(False)

        self._title.setText(title)
        self._detail.setText("")
        self._stop_btn.setEnabled(True)
        self._stop_btn.show()
        self._expand_btn.show()
        self._restore_expand_btn()
        self._reset_detail_area()

        self._apply_style()
        self._reposition()
        self._slide_in()

    def _restore_expand_btn(self) -> None:
        """将复用面板的关闭按钮恢复为展开按钮，并重置信号连接。

        关闭操作仅在失败或终止批次后连接，受 `_dismiss_wired` 控制。
        明细区需在每批开始时复位，独立由 `_reset_detail_area` 处理，避免继承上一批展开状态。
        """
        if not getattr(self, "_dismiss_wired", False):
            return
        self._dismiss_wired = False
        try:
            self._expand_btn.clicked.disconnect()
        except TypeError:
            pass
        self._expand_btn.clicked.connect(self._toggle_expand)
