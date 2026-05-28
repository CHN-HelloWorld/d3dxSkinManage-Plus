"""更新条目和批次状态栏共用的真实进度与控制组件。"""

from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QProgressBar

from src.ui import anim
from src.ui.dialogs.mod_updates_style import color
from src.ui.widgets.ring_progress import RingProgressButton
from src.utils.i18n import N_
from src.utils.i18n import t as tr

PAUSABLE_PHASES = frozenset({"queued", "preparing", "downloading", "verifying", "extracting", "installing"})
ACTIVE_PHASES = PAUSABLE_PHASES | {"paused", "stopping"}
FILE_PHASES = frozenset({"verifying", "extracting", "installing"})
PHASE_LABELS = {
    "queued": N_("排队中"),
    "preparing": N_("准备下载"),
    "downloading": N_("正在下载"),
    "verifying": N_("正在校验"),
    "extracting": N_("正在解压"),
    "installing": N_("正在安装"),
    "paused": N_("已暂停"),
    "stopping": N_("正在终止"),
    "canceled": N_("已终止"),
    "done": N_("已更新"),
    "failed": N_("更新失败"),
    "deferred": N_("等待继续"),
}
PHASE_ICONS = {
    "queued": "actions/list-view",
    "preparing": "mod-updates/refresh",
    "downloading": "mod-updates/download",
    "verifying": "mod-updates/check-circle",
    "extracting": "actions/folder-open",
    "installing": "mod-updates/box",
    "paused": "actions/pause",
    "stopping": "actions/stop",
    "canceled": "actions/stop",
    "done": "mod-updates/check",
    "failed": "mod-updates/warning",
    "deferred": "actions/play",
}


def finite_number(value, default: float = 0.0) -> float:
    """接受非布尔的有限整数或浮点数并转为浮点值，其他输入返回指定默认值。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return default


def progress_phase(snapshot: dict) -> str:
    """为暂停、失败、取消或延期快照返回恢复阶段，其余返回当前阶段以解释对应进度数据。"""
    phase = snapshot.get("phase", "preparing")
    if phase in {"paused", "failed", "canceled", "deferred"}:
        return snapshot.get("resume_phase", "")
    return phase


def progress_fraction(snapshot: dict) -> float | None:
    """按实际处理阶段返回零到一的进度，未知时返回空值；下载字节比例不用于表示后续文件处理进度。"""
    phase = progress_phase(snapshot)
    if phase == "downloading":
        total = finite_number(snapshot.get("total_bytes"))
        if total > 0:
            return min(1.0, max(0.0, finite_number(snapshot.get("downloaded_bytes")) / total))
    elif phase in FILE_PHASES:
        value = finite_number(snapshot.get("stage_progress"), -1.0)
        if value >= 0:
            return min(1.0, value)
    return None


def format_eta(value) -> str:
    """将有效非负秒数向上取整后格式化为预计剩余时间，无效值或负值返回空字符串。"""
    seconds = finite_number(value, -1.0)
    if seconds < 0:
        return ""
    seconds = math.ceil(seconds)
    if seconds < 60:
        return tr("约 {seconds} 秒", seconds=seconds) if seconds else tr("不足 1 秒")
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return tr("约 {minutes} 分 {seconds:02d} 秒", minutes=minutes, seconds=seconds)
    hours, minutes = divmod(minutes, 60)
    return tr("约 {hours} 小时 {minutes} 分", hours=hours, minutes=minutes)


class TaskProgressBar(QProgressBar):
    """保留辅助功能的细进度条，未知进度仅在运行时播放动画。"""

    def __init__(self, parent=None) -> None:
        """创建三像素高的进度条及所属动画定时器，初始不运行扫动动画。"""
        super().__init__(parent)
        self.setFixedHeight(3)
        self.setMinimumWidth(0)
        self.setTextVisible(False)
        self.tone = "accent"
        self._running = False
        self._sweep = 0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._advance)

    def set_measurement(self, fraction: float | None, *, running: bool, tone: str = "accent") -> None:
        """接收真实比例或未知值并更新量程、运行状态和颜色，只有未知且运行中的进度允许动画。"""
        self.tone = tone
        self._running = running
        self.setRange(0, 0 if fraction is None else 1000)
        if fraction is not None:
            self.setValue(round(min(1.0, max(0.0, fraction)) * 1000))
        self._sync_timer()
        self.update()

    def is_animating(self) -> bool:
        """返回内部扫动定时器是否活动，供绘制区分动画和静态未知进度。"""
        return self._timer.isActive()

    def _sync_timer(self) -> None:
        """仅在任务运行、进度未知、控件可见且全局动画开启时启动扫动定时器。"""
        if self._running and self.maximum() == 0 and self.isVisible() and anim.is_enabled():
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()

    def _advance(self) -> None:
        """循环推进扫动帧索引并请求重绘，不修改任务的真实进度。"""
        self._sweep = (self._sweep + 1) % 38
        self.update()

    def showEvent(self, event) -> None:
        """控件显示后按当前任务和动画条件恢复扫动定时器。"""
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event) -> None:
        """控件隐藏时停止扫动定时器，保留任务进度状态供再次显示。"""
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:
        """绘制主题轨道与真实比例填充，未知进度根据定时器状态显示移动或静止短条。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = QColor(color(self.tone))
        track = QColor(fill)
        track.setAlphaF(0.14)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        rect = QRectF(self.rect())
        painter.drawRoundedRect(rect, 1.5, 1.5)
        painter.setClipRect(rect)
        painter.setBrush(fill)
        if self.maximum() == 0:
            width = self.width() * 0.36
            left = (self.width() + width) * self._sweep / 38 - width if self.is_animating() else 0
            rect = QRectF(left, 0, width, self.height())
        else:
            rect.setWidth(self.width() * max(0, self.value()) / self.maximum())
        painter.drawRoundedRect(rect, 1.5, 1.5)


class TaskRingButton(RingProgressButton):
    """支持键盘焦点的环形下载控件，隐藏时停止动画。"""

    def __init__(self) -> None:
        """创建固定尺寸的环形进度按钮，并启用键盘焦点。"""
        super().__init__(size=32, line_width=2)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_indeterminate(self, on: bool) -> None:
        """切换基类不确定进度状态，隐藏或全局禁用动画时立即停止旋转定时器。"""
        super().set_indeterminate(on)
        if self._spin_timer is not None and (not self.isVisible() or not anim.is_enabled()):
            self._spin_timer.stop()

    def showEvent(self, event) -> None:
        """显示时仅为启用动画的不确定进度恢复旋转。"""
        super().showEvent(event)
        if self._indeterminate and self._spin_timer is not None and anim.is_enabled():
            self._spin_timer.start()

    def hideEvent(self, event) -> None:
        """隐藏环形按钮时停止旋转定时器，避免后台重绘。"""
        if self._spin_timer is not None:
            self._spin_timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:
        """先绘制基类进度环，拥有键盘焦点时叠加主题焦点轮廓。"""
        super().paintEvent(event)
        if self.hasFocus():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(color("accent")), 1))
            painter.drawEllipse(QRectF(self.rect()).adjusted(1, 1, -1, -1))
