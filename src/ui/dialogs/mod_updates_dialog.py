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
