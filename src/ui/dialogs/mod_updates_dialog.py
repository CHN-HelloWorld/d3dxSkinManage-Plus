"""以可展开列表显示 Mod 更新，底部固定显示批次操作。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from time import monotonic

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    QRectF,
    QSequentialAnimationGroup,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)

from src.services.gamebanana.update_checker import UpdateInfo
from src.ui import anim, icons, theme
from src.ui.dialog_chrome import DialogTitleBar
from src.ui.dialog_chrome import ResizeHandle as _ResizeHandle
from src.ui.dialogs.mod_update_check_panel import UpdateCheckPanel, refresh_icon
from src.ui.dialogs.mod_update_row import ElidedLabel, UpdateRow, apply_icon, format_size, icon_label, label
from src.ui.dialogs.mod_update_task_progress import ACTIVE_PHASES, PAUSABLE_PHASES, TaskProgressBar, finite_number
from src.ui.dialogs.mod_updates_style import TEXT_LINE_HEIGHT, blend, color, stylesheet
from src.utils.i18n import N_
from src.utils.i18n import t as tr

_SUCCESS_HOLD_MS = 2000


class _SuccessRetirement(QObject):
    """短暂保留成功结果，再淡出并收起布局空间。"""

    finished = Signal(str)

    def __init__(self, row: UpdateRow, parent: QObject, *, hold_ms: int = _SUCCESS_HOLD_MS) -> None:
        """持有成功条目并启动一次性阅读停留计时器，计时结束后再移出列表。"""
        super().__init__(parent)
        self.row = row
        self._sequence: QSequentialAnimationGroup | None = None
        self._effect: QGraphicsOpacityEffect | None = None
        self._original_height: tuple[int, int, QLayout.SizeConstraint] | None = None
        self._collapsing = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._begin_exit)
        # 结果停留时间用于阅读，减少动画模式下同样保留。
        self._timer.start(hold_ms)

    def _begin_exit(self) -> None:
        """条目可见且允许动画时依次淡出和收缩高度，否则直接发送移除请求。"""
        row = self.row
        if not anim.is_enabled() or not row.isVisible():
            self.finished.emit(row.info.local_mod_id)
            return
        self._original_height = (row.minimumHeight(), row.maximumHeight(), row.layout().sizeConstraint())
        self._effect = QGraphicsOpacityEffect(row)
        self._effect.setOpacity(1.0)
        row.setGraphicsEffect(self._effect)
        sequence = QSequentialAnimationGroup(self)
        self._sequence = sequence
        fade = QPropertyAnimation(self._effect, b"opacity", sequence)
        fade.setDuration(220)
        fade.setStartValue(1.0)
        fade.setEndValue(0.0)
        fade.setEasingCurve(QEasingCurve.Type.InQuad)
        collapse = QVariantAnimation(sequence)
        collapse.setDuration(360)
        collapse.setStartValue(row.height())
        collapse.setEndValue(0)
        collapse.setEasingCurve(QEasingCurve.Type.OutCubic)

        def prepare_collapse() -> None:
            """淡出完成后解除内部布局尺寸约束，并从条目实时高度开始收缩。"""
            self._collapsing = True
            # 固定高度子控件不能限制条目的收起动画。
            row.layout().setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
            collapse.setStartValue(row.height())

        def set_height(value) -> None:
            """仅在收缩阶段把动画值应用为条目固定高度，避免初始化动画时压缩布局。"""
            if self._collapsing:
                row.setFixedHeight(int(value))

        fade.finished.connect(prepare_collapse)
        collapse.valueChanged.connect(set_height)
        sequence.addAnimation(fade)
        sequence.addAnimation(collapse)
        sequence.finished.connect(lambda: self.finished.emit(row.info.local_mod_id))
        sequence.start()

    def cancel(self) -> None:
        """收起期间出现更新内容时重新显示条目。"""
        self._timer.stop()
        if self._sequence is not None:
            self._sequence.stop()
        if self._effect is not None and self.row.graphicsEffect() is self._effect:
            self.row.setGraphicsEffect(None)
        self._effect = None
        if self._original_height is not None:
            minimum, maximum, constraint = self._original_height
            self.row.setMinimumHeight(minimum)
            self.row.setMaximumHeight(maximum)
            self.row.layout().setSizeConstraint(constraint)
            self.row.updateGeometry()
        self.deleteLater()


class _BulkUpdateButton(QPushButton):
    """展示图标、文字、分隔线及独立更新数量。"""

    def __init__(self) -> None:
        """创建独立的图标、标题和数量子控件，使文字布局不依赖按钮默认绘制。"""
        super().__init__()
        self.setObjectName("updateAll")
        self.setAutoDefault(False)
        self._minimum_width = 132
        self._text_color = ""
        self.setFixedSize(132, 36)
        self.setIconSize(QSize(14, 14))
        apply_icon(self, "download", "on_accent")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(13, 0, 13, 0)
        layout.setSpacing(7)
        self._icon = icon_label("download", 14, "on_accent")
        self._title = label(tr("全部更新"))
        self._divider = QWidget()
        self._divider.setObjectName("updateAllDivider")
        self._divider.setFixedSize(1, 17)
        self._count = label("0")
        self._count.setObjectName("updateAllCount")
        self._count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for child in (self._icon, self._title, self._divider, self._count):
            layout.addWidget(child)
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_count(self, count: int, busy: bool, status: str = N_("正在更新…"), *, complete: bool = False) -> None:
        """按待更新数、忙碌或完成状态更新标题与图标，并同步可访问名称、禁用颜色及最小宽度。"""
        self._title.setText(tr("完成") if complete else status if busy else tr("全部更新"))
        self._count.setText(str(count))
        self._count.setVisible(not (busy or complete))
        self._divider.setVisible(not (busy or complete))
        svg = "check" if complete else "refresh" if busy else "download"
        apply_icon(self, svg, "on_accent")
        text_color = color("on_accent") if self.isEnabled() else blend("on_accent", "layer", 0.48)
        if self._text_color != text_color:
            self._text_color = text_color
            for widget in (self._title, self._count):
                widget.setStyleSheet(f"color: {text_color};")
            self._divider.setStyleSheet(f"background: {text_color};")
        self._icon.setPixmap(icons.colored_pixmap_from(f"mod-updates/{svg}", 14, text_color))
        self.setFixedWidth(max(self._minimum_width, self.layout().sizeHint().width()))
        self.setAccessibleName(
            tr("完成") if complete else status if busy else tr("全部更新，{count} 个 Mod", count=count)
        )

    def paintEvent(self, event) -> None:
        """仅绘制按钮背景和边框，将图标及文字绘制交给内部子控件。"""
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text, option.icon = "", QIcon()
        QStylePainter(self).drawControl(QStyle.ControlElement.CE_PushButton, option)


class ModUpdatesDialog(QDialog):
    """展示更新候选和任务快照，维护成功条目退场及搜索视图，实际任务和关闭决策交由宿主处理。"""

    recheck_requested = Signal()
    cancel_check_requested = Signal()
    retry_failed_requested = Signal()
    open_gb_requested = Signal(str)
    ignore_version_requested = Signal(str)
    ignore_mod_requested = Signal(str)
    update_requested = Signal(str)
    update_all_requested = Signal()
    pause_update_requested = Signal(str)
    resume_update_requested = Signal(str)
    stop_update_requested = Signal(str)
    pause_all_requested = Signal()
    resume_all_requested = Signal()
    stop_all_requested = Signal()

    def __init__(
        self,
        infos: list[UpdateInfo],
        *,
        failures: dict[str, str] | None = None,
        enable_actions: bool = False,
        config=None,
        parent=None,
    ) -> None:
        """初始化候选、任务和检查状态，恢复可用窗口尺寸并构建自定义边框；是否允许操作由宿主指定。"""
        super().__init__(parent)
        self._config = config
        self.actions_enabled = enable_actions
        self._rows_by_id: dict[str, UpdateRow] = {}
        self._task_snapshots: dict[str, dict] = {}
        self._completed_files: dict[str, tuple[int, int, int, str]] = {}
        self._row_retirements: dict[str, _SuccessRetirement] = {}
        self._close_handler: Callable[[], bool] | None = None
        self._requesting_close = False
        self._force_closing = False
        self._dismissed = False
        self._check_hidden_by_tasks = False
        self._failures: dict[str, str] = {}
        self._checking = False
        self._check_done = 0
        self._check_total = 0
        self._check_snapshot: dict = {"phase": "idle", "checking": False}
        self._pending_check_updates: tuple[list[UpdateInfo], dict[str, str] | None] | None = None
        self._density: tuple[bool, bool] | None = None
        self._theme = theme.current_theme()
        self._undo: Callable[[], bool | None] | None = None
        self._sized_for_screen = False
        self.setObjectName("modUpdatesDialog")
        self.setProperty("customDialogFrame", True)
        self.setProperty("opaqueDialogSurface", True)
        self.setWindowTitle(tr("更新 Mod"))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        # 拖动缩小时分层表面可能暂时丢失，保持客户区不透明，由系统提供圆角。
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setModal(True)
        self.setMinimumSize(420, 340)
        remembered = [848, 626]
        if config is not None:
            for index, key in enumerate(("width", "height")):
                value = config.get(f"ui.mod_updates.{key}", 0)
                if type(value) is int and value > 0:
                    remembered[index] = min(value, 16777215)
        self.resize(*remembered)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        outer.addWidget(self._build_titlebar())
        outer.addWidget(self._build_header())
        outer.addWidget(self._build_body(), 1)
        outer.addWidget(self._build_footer())
        self._build_message()
        self._resize_handles = {
            edges: _ResizeHandle(self, edges, cursor)
            for edges, cursor in (
                (Qt.Edge.LeftEdge, Qt.CursorShape.SizeHorCursor),
                (Qt.Edge.RightEdge, Qt.CursorShape.SizeHorCursor),
                (Qt.Edge.TopEdge, Qt.CursorShape.SizeVerCursor),
                (Qt.Edge.BottomEdge, Qt.CursorShape.SizeVerCursor),
                (Qt.Edge.LeftEdge | Qt.Edge.TopEdge, Qt.CursorShape.SizeFDiagCursor),
                (Qt.Edge.RightEdge | Qt.Edge.TopEdge, Qt.CursorShape.SizeBDiagCursor),
                (Qt.Edge.LeftEdge | Qt.Edge.BottomEdge, Qt.CursorShape.SizeBDiagCursor),
                (Qt.Edge.RightEdge | Qt.Edge.BottomEdge, Qt.CursorShape.SizeFDiagCursor),
            )
        }
        self.setStyleSheet(stylesheet())
        self._apply_density()
        self.set_updates(infos, failures=failures or {})
        QWidget.setTabOrder(self._search, self._recheck_btn)
        QWidget.setTabOrder(self._recheck_btn, self._update_all_btn)

    def _button(self, text: str, svg: str = "", *, quiet: bool = False) -> QPushButton:
        """创建固定高度的更新窗口按钮，按需设置主题图标和轻量样式。"""
        button = QPushButton(text)
        button.setAutoDefault(False)
        button.setFixedHeight(34)
        button.setProperty("quiet", quiet)
        button.setIconSize(QSize(15, 15))
        if svg:
            apply_icon(button, svg)
        return button

    def _build_titlebar(self) -> QWidget:
        """创建共享可拖动标题栏，并保存关闭按钮供布局密度调整。"""
        bar = DialogTitleBar(self)
        self._titlebar = bar
        self._close_btn = bar.close_button
        self._close_btn.setObjectName("updateClose")
        return bar

    def _build_header(self) -> QWidget:
        """构建更新数量标题和重新检查入口，检查动作通过信号交给宿主。"""
        header = QWidget()
        self._header = header
        header.setObjectName("updateHeader")
        header.setFixedHeight(84)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(26, 15, 26, 19)
        layout.setSpacing(13)
        badge = icon_label("download", 23, "accent")
        self._header_badge = badge
        badge.setObjectName("updateHeaderIcon")
        badge.setFixedSize(43, 43)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(badge)
        copy = QVBoxLayout()
        self._heading_layout = copy
        copy.setSpacing(4)
        title = label(tr("可更新的 Mod"), "updateTitle")
        self._heading_title = title
        title.setFixedHeight(31)
        copy.addWidget(title)
        self._subtitle = label(name="updateSubtitle")
        self._subtitle.setFixedHeight(TEXT_LINE_HEIGHT)
        copy.addWidget(self._subtitle)
        layout.addLayout(copy, 1)
        self._recheck_btn = self._button(tr("检测更新"), "refresh", quiet=True)
        self._recheck_btn.setObjectName("updateRecheck")
        self._recheck_btn.setFixedWidth(98)
        self._recheck_btn.clicked.connect(self.recheck_requested.emit)
        layout.addWidget(self._recheck_btn)
        return header

    def _build_body(self) -> QWidget:
        """组合检查反馈、批次控制、搜索和滚动列表，并转发检查取消与重试信号。"""
        body = QWidget()
        body.setObjectName("updateBody")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 1, 0, 0)
        layout.setSpacing(0)
        self._check_panel = UpdateCheckPanel()
        self._check_panel.cancel_requested.connect(self.cancel_check_requested.emit)
        self._check_panel.retry_requested.connect(self.retry_failed_requested.emit)
        self._check_panel.spin_angle_changed.connect(self._spin_check_icon)
        layout.addWidget(self._check_panel)
        layout.addWidget(self._build_batch_panel())
        collection = QWidget()
        self._collection = collection
        collection.setFixedHeight(48)
        collection_layout = QHBoxLayout(collection)
        collection_layout.setContentsMargins(26, 12, 26, 7)
        collection_layout.setSpacing(6)
        self._collection_label = label(tr("更新列表"), "updateCollectionLabel")
        collection_layout.addWidget(self._collection_label)
        self._visible_count = label("0", "updateVisibleCount")
        self._visible_count.setMinimumWidth(21)
        self._visible_count.setFixedHeight(TEXT_LINE_HEIGHT)
        self._visible_count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        collection_layout.addWidget(self._visible_count)
        collection_layout.addStretch()
        self._search = QLineEdit()
        self._search.setObjectName("updateSearch")
        self._search.setFixedSize(185, 28)
        self._search.setPlaceholderText(tr("搜索 Mod 名称"))
        self._search.setAccessibleName(tr("搜索 Mod 名称"))
        search_icon = self._search.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition)
        apply_icon(search_icon, "search", "muted")
        self._search.textChanged.connect(self._filter_rows)
        collection_layout.addWidget(self._search)
        layout.addWidget(collection)
        self._stack = QStackedWidget()
        self._scroll = QScrollArea()
        self._scroll.setObjectName("updatesScroll")
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.viewport().setAutoFillBackground(False)
        self._list = QWidget()
        self._list.setMinimumWidth(0)
        self._list_layout = QVBoxLayout(self._list)
        self._list_layout.setContentsMargins(22, 0, 22, 14)
        self._list_layout.setSpacing(0)
        self._list_layout.addStretch()
        self._scroll.setWidget(self._list)
        self._stack.addWidget(self._scroll)
        self._stack.addWidget(self._build_empty())
        layout.addWidget(self._stack, 1)
        return body

    def _build_batch_panel(self) -> QWidget:
        """创建覆盖当前游戏全部任务的批次控制和下载汇总，搜索隐藏的任务仍属于控制范围。"""
        self._batch_panel = QWidget()
        self._batch_panel.setObjectName("updateBatchPanel")
        self._batch_panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        outer = QVBoxLayout(self._batch_panel)
        outer.setSpacing(0)
        self._batch_surface = QWidget()
        self._batch_surface.setObjectName("updateBatchSurface")
        self._batch_surface.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        content = QVBoxLayout(self._batch_surface)
        content.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(5)
        self._batch_icon = icon_label("download", 14, "accent")
        top.addWidget(self._batch_icon)
        self._batch_title = ElidedLabel(name="updateBatchTitle")
        top.addWidget(self._batch_title, 1)
        controls = (
            ("pause", tr("全部暂停"), self.pause_all_requested),
            ("play", tr("全部继续"), self.resume_all_requested),
            ("stop", tr("全部终止"), self.stop_all_requested),
        )
        buttons = []
        for action, text, signal in controls:
            button = QPushButton(text)
            button.setObjectName(f"updateBatch{action.title()}")
            button.setProperty("batchControl", True)
            button.setAutoDefault(False)
            button.setFixedHeight(24)
            button.setIconSize(QSize(12, 12))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAccessibleName(tr("{text}当前游戏的更新任务", text=text))
            button.setToolTip(
                tr("{text}当前游戏所有未结束的更新（包括搜索隐藏的任务）", text=text)
                + ("…" if action == "stop" else "")
            )
            icons.apply_icon(button, f"actions/{action}", "mod_updates.secondary")
            button.clicked.connect(signal.emit)
            top.addWidget(button)
            buttons.append(button)
        self._pause_all_btn, self._resume_all_btn, self._stop_all_btn = buttons
        content.addLayout(top)
        self._batch_counts = ElidedLabel(name="updateBatchCounts")
        content.addWidget(self._batch_counts)
        self._batch_transfer = ElidedLabel(name="updateBatchTransfer")
        content.addWidget(self._batch_transfer)
        self._batch_progress = TaskProgressBar()
        self._batch_progress.setObjectName("updateBatchTrack")
        self._batch_progress.setAccessibleName(tr("全部更新任务的下载进度"))
        content.addWidget(self._batch_progress)
        outer.addWidget(self._batch_surface)
        self._batch_panel.hide()
        return self._batch_panel

    def _build_empty(self) -> QWidget:
        """构建可滚动的空列表提示页，按状态提供重新检查或清除搜索操作。"""
        empty = QWidget()
        empty.setMinimumHeight(220)
        layout = QVBoxLayout(empty)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(0)
        layout.addStretch()
        badge = icon_label("check", 29, "accent")
        badge.setObjectName("updateEmptyIcon")
        badge.setFixedSize(64, 64)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_icon = badge
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(17)
        self._empty_title = label(name="updateEmptyTitle")
        self._empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_title.setWordWrap(True)
        layout.addWidget(self._empty_title)
        layout.addSpacing(9)
        self._empty_description = label(name="updateEmptyDescription")
        self._empty_description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_description.setWordWrap(True)
        layout.addWidget(self._empty_description)
        layout.addSpacing(19)
        self._empty_action = self._button(tr("重新检查"), "refresh")
        self._empty_action.setObjectName("updateEmptyAction")
        self._empty_action.setFixedWidth(100)
        self._empty_action.clicked.connect(self._empty_action_clicked)
        layout.addWidget(self._empty_action, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setObjectName("updateEmptyScroll")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(empty)
        return scroll

    def _build_footer(self) -> QWidget:
        """构建固定底栏，展示待更新数量、预计容量及全部更新或完成操作。"""
        footer = QWidget()
        self._footer = footer
        footer.setObjectName("updateFooter")
        footer.setFixedHeight(75)
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(25, 15, 25, 15)
        layout.setSpacing(10)
        self._footer_icon = icon_label("download", 16, "accent")
        layout.addWidget(self._footer_icon)
        copy = QVBoxLayout()
        copy.setSpacing(1)
        self._footer_count = label(name="updateFooterCount")
        self._footer_count.setTextFormat(Qt.TextFormat.RichText)
        self._footer_count.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._download_size = label(name="updateDownloadSize")
        copy.addWidget(self._footer_count)
        copy.addWidget(self._download_size)
        layout.addLayout(copy, 1)
        self._later = self._button(tr("稍后"), quiet=True)
        self._later.setFixedWidth(48)
        self._later.clicked.connect(self.reject)
        layout.addWidget(self._later)
        self._update_all_btn = _BulkUpdateButton()
        self._update_all_btn.clicked.connect(self._update_all_clicked)
        layout.addWidget(self._update_all_btn)
        return footer

    def _build_message(self) -> None:
        """创建窗口持有的临时提示层和可选撤销按钮，并配置一次性自动隐藏定时器。"""
        self._message = QWidget(self)
        self._message.setObjectName("updateMessage")
        layout = QHBoxLayout(self._message)
        layout.setContentsMargins(17, 12, 17, 12)
        layout.setSpacing(10)
        layout.addWidget(icon_label("info", 16, "accent"))
        self._message_text = label(name="updateMessageText")
        self._message_text.setWordWrap(True)
        layout.addWidget(self._message_text, 1)
        self._undo_btn = self._button(tr("撤销"))
        self._undo_btn.setObjectName("updateUndo")
        self._undo_btn.setFixedWidth(36)
        self._undo_btn.clicked.connect(self._undo_clicked)
        layout.addWidget(self._undo_btn)
        self._message.hide()
        self._message_timer = QTimer(self)
        self._message_timer.setSingleShot(True)
        self._message_timer.timeout.connect(self._dismiss_message)

    def row_count(self) -> int:
        """返回仍由窗口持有的条目数，包含搜索隐藏及等待成功退场的条目。"""
        return len(self._rows_by_id)

    def pending_ids(self) -> list[str]:
        """返回尚未完成且未进入退场流程的 Mod 标识；搜索只改变可见性，不改变全部更新范围。"""
        return [
            mid for mid, row in self._rows_by_id.items() if row.state != "done" and mid not in self._row_retirements
        ]

    def is_batch_complete(self) -> bool:
        """仅在有任务历史、检查已结束、无待处理行且全部任务成功时返回真；成功行移出后仍使用历史判断。"""
        return (
            bool(self._task_snapshots)
            and not self._checking
            and not self.pending_ids()
            and all(snapshot.get("phase") == "done" for snapshot in self._task_snapshots.values())
        )

    def set_close_handler(self, callback: Callable[[], bool]) -> None:
        """用户关闭前先由所有者结算活动工作。"""
        self._close_handler = callback

    def prepare_for_open(self, infos: list[UpdateInfo], *, failures: dict[str, str]) -> None:
        """复用静态界面并恢复服务当前状态。"""
        self._message_timer.stop()
        self._dismiss_message()
        self._search.clear()
        for mid in list(self._row_retirements):
            self._cancel_retirement(mid)
        self._task_snapshots.clear()
        self._completed_files.clear()
        self._pending_check_updates = None
        self._checking = False
        self._check_done = self._check_total = 0
        self._check_snapshot = {"phase": "idle", "checking": False}
        self._dismissed = False
        self.setResult(QDialog.DialogCode.Rejected)
        for row in self._rows_by_id.values():
            row.set_state("ready")
        self.set_updates(infos, failures=failures)
        self._scroll.verticalScrollBar().setValue(0)

    def force_close(self) -> None:
        """游戏切换和应用退出直接执行生命周期清理，不再次询问普通关闭操作。"""
        self._force_closing = True
        try:
            self.done(QDialog.DialogCode.Rejected)
        finally:
            self._force_closing = False

    def _update_all_clicked(self) -> None:
        """批次已全部完成时接受窗口，否则向宿主请求启动全部待更新项目。"""
        if self.is_batch_complete():
            self.accept()
        else:
            self.update_all_requested.emit()

    @staticmethod
    def _file_identity(info: UpdateInfo) -> tuple[int, int, int, str]:
        """提取来源、文件标识、发布时间和大小写无关校验和，作为已完成文件的比较身份。"""
        file = info.latest_file
        return info.gb_mod_id, file.file_id, file.date_added_ts, (file.md5_checksum or "").casefold()

    def _is_newer_update(self, info: UpdateInfo, completed: tuple[int, int, int, str]) -> bool:
        """来源变化视为新候选；同来源必须时间更新，且可比较校验和或文件标识发生变化才重新显示。"""
        source, file_id, timestamp, checksum = self._file_identity(info)
        old_source, old_id, old_timestamp, old_checksum = completed
        if source != old_source:
            return True
        if timestamp <= old_timestamp:
            return False
        return checksum != old_checksum if checksum and old_checksum else file_id != old_id

    def _schedule_retirement(self, mid: str, row: UpdateRow) -> None:
        """记录完成文件身份并按剩余阅读时间安排退场，已超过停留期的后台结果立即移除。"""
        self._completed_files.setdefault(mid, self._file_identity(row.info))
        if mid not in self._row_retirements:
            completed_at = finite_number(self._task_snapshots.get(mid, {}).get("completed_at"))
            elapsed_ms = max(0, int((monotonic() - completed_at) * 1000)) if completed_at > 0 else 0
            remaining_ms = _SUCCESS_HOLD_MS - elapsed_ms
            if remaining_ms <= 0:
                # 重新打开时不重放已经在后台收起的结果提示。
                self._retire_success_row(mid)
                return
            retirement = _SuccessRetirement(row, self, hold_ms=remaining_ms)
            self._row_retirements[mid] = retirement
            retirement.finished.connect(self._retire_success_row)
