"""根据服务检查快照显示 Mod 更新状态。"""

from __future__ import annotations

from time import monotonic

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui import anim, icons
from src.ui.dialogs.mod_updates_style import BODY_FONT_SIZE, TEXT_LINE_HEIGHT, color
from src.utils.i18n import t as tr


def refresh_icon(angle: float, tone: str = "secondary") -> QIcon:
    """在固定画布上旋转图标，不改变布局尺寸。"""
    pixmap = QPixmap(28, 28)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.translate(14, 14)
    painter.rotate(angle)
    painter.drawPixmap(-14, -14, icons.pixmap("mod-updates/refresh", 28, f"mod_updates.{tone}"))
    painter.end()
    pixmap.setDevicePixelRatio(2)
    icon = QIcon(pixmap)
    icon.addPixmap(pixmap, QIcon.Mode.Disabled)
    return icon


class _CheckProgress(QProgressBar):
    """保留进度条的辅助功能，并使用细轨道绘制。"""

    def __init__(self) -> None:
        """创建三像素高的检查进度条，初始化主题色、扫动位置及数值插值状态。"""
        super().__init__()
        self.setObjectName("updateCheckProgress")
        self.setFixedHeight(3)
        self.setTextVisible(False)
        self.tone = "accent"
        self.sweep = 0.0
        self._from = self._target = 0.0
        self._changed_at = 0.0

    def _fraction(self) -> float:
        """计算当前一百八十毫秒线性插值的绘制比例，动画关闭时直接返回目标比例。"""
        if not anim.is_enabled():
            return self._target
        fraction = min(1, max(0, (monotonic() - self._changed_at) / 0.18))
        return self._from + (self._target - self._from) * fraction

    def setValue(self, value: int) -> None:
        """更新真实进度值并从当前绘制比例建立新的过渡，保留 QProgressBar 的可访问数值。"""
        target = max(0, value) / max(1, self.maximum())
        if target != self._target:
            self._from = self._fraction() if self.value() >= 0 and self._changed_at else target
            self._target, self._changed_at = target, monotonic()
        super().setValue(value)

    def paintEvent(self, event) -> None:
        """绘制细圆角轨道，未知进度使用扫动条，确定进度使用插值比例填充。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        fill = QColor(color(self.tone))
        track = QColor(fill)
        track.setAlphaF(0.14)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(rect, 1.5, 1.5)
        painter.setClipRect(rect)
        painter.setBrush(fill)
        if self.maximum() == 0:
            width = self.width() * (0.36 if anim.is_enabled() else 0.45)
            left = width * (-1.15 + 5 * self.sweep) if anim.is_enabled() else 0
            painter.drawRoundedRect(QRectF(left, 0, width, 3), 1.5, 1.5)
        else:
            rect.setWidth(self.width() * self._fraction())
            painter.drawRoundedRect(rect, 1.5, 1.5)


class UpdateCheckPanel(QWidget):
    """根据检查快照展示阶段、耗时和失败重试入口，可独立收起而不修改服务结果。"""

    cancel_requested = Signal()
    retry_requested = Signal()
    spin_angle_changed = Signal(float)

    def __init__(self) -> None:
        """创建检查反馈控件及所属定时器，初始为空闲隐藏状态，并配置不确定进度的缓动曲线。"""
        super().__init__()
        self.setObjectName("updateCheckPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._snapshot: dict = {"phase": "idle", "checking": False}
        self._available = 0
        self._dismissed = False
        self._compact = False
        self._short = False
        self._tone = ""
        self._symbol = ""
        self._outer = QVBoxLayout(self)
        self._outer.setSpacing(0)
        self._surface = QFrame()
        self._surface.setObjectName("updateCheckFeedback")
        self._content = QVBoxLayout(self._surface)
        self._content.setSpacing(0)
        top = QHBoxLayout()
        top.setSpacing(7)
        self._icon = QLabel()
        self._icon.setFixedSize(14, 14)
        top.addWidget(self._icon)
        self.title = self._label("updateCheckTitle")
        self.title.setWordWrap(True)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        top.addWidget(self.title, 1)
        self.elapsed = self._label("updateCheckElapsed")
        top.addWidget(self.elapsed)
        self.action = QPushButton()
        self.action.setObjectName("updateCheckAction")
        self.action.setAutoDefault(False)
        self.action.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action.clicked.connect(self._activate)
        top.addWidget(self.action)
        self._content.addLayout(top)
        self._content.addSpacing(3)
        self.detail = self._label("updateCheckDetail")
        self.detail.setWordWrap(True)
        self.detail.setMinimumWidth(0)
        self._content.addWidget(self.detail)
        self._progress_wrap = QWidget()
        self._progress_layout = QVBoxLayout(self._progress_wrap)
        self.progress = _CheckProgress()
        self._progress_layout.addWidget(self.progress)
        self._content.addWidget(self._progress_wrap)
        self.hint = self._label("updateCheckHint")
        self.hint.setTextFormat(Qt.TextFormat.RichText)
        self.hint.setWordWrap(True)
        self._content.addWidget(self.hint)
        self._outer.addWidget(self._surface)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._sweep_ease = QEasingCurve(QEasingCurve.Type.BezierSpline)
        self._sweep_ease.addCubicBezierSegment(QPointF(0.42, 0), QPointF(0.58, 1), QPointF(1, 1))
        self.set_density(compact=False, short=False)
        self.hide()

    @staticmethod
    def _label(name: str) -> QLabel:
        """创建具名纯文本标签，供局部样式区分标题、耗时和详情。"""
        widget = QLabel()
        widget.setObjectName(name)
        widget.setTextFormat(Qt.TextFormat.PlainText)
        return widget

    def set_snapshot(self, snapshot: dict, *, available: int) -> None:
        """复制服务快照并更新可用数量，新一轮检查重置收起状态，然后同步显示和定时刷新。"""
        if (
            snapshot.get("started_at") != self._snapshot.get("started_at")
            or snapshot.get("checking")
            and not self._snapshot.get("checking")
        ):
            self._dismissed = False
        self._snapshot = dict(snapshot)
        self._available = available
        self.setVisible(snapshot.get("phase", "idle") != "idle" and not self._dismissed)
        self._tick()
        self._sync_timer()
