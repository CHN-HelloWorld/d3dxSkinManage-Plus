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

    def set_density(self, *, compact: bool, short: bool) -> None:
        """根据紧凑和低高度布局调整内外边距，保留详情行高并刷新主题。"""
        self._compact, self._short = compact, short
        self._outer.setContentsMargins(
            12 if compact else 24, 4 if short else 7 if compact else 10, 12 if compact else 24, 0
        )
        self._content.setContentsMargins(
            8 if short else 9 if compact else 12,
            6 if short else 8 if compact else 10,
            8 if short else 9 if compact else 12,
            6 if short else 8 if compact else 10,
        )
        self._progress_layout.setContentsMargins(0, 5 if short else 8, 0, 0)
        self._progress_layout.setSpacing(0)
        self.detail.setMinimumHeight(TEXT_LINE_HEIGHT)
        self.hint.setMinimumHeight(TEXT_LINE_HEIGHT)
        self.retheme()

    def retheme(self) -> None:
        """使颜色和图标缓存失效，再按当前检查快照重新绘制反馈。"""
        self._tone = ""
        self._symbol = ""
        self._tick()

    def _style(self, tone: str) -> None:
        """按阶段语义色更新反馈面板、操作按钮和进度条样式，同色时复用已生成样式。"""
        if self._tone == tone:
            return
        self._tone = tone
        background, border = {
            "accent": ("accent_soft", "accent_border"),
            "warning": ("warning_soft", "warning_border"),
            "green": ("green_soft", "green"),
            "secondary": ("layer", "line"),
        }[tone]
        border_color = color(border)
        if tone == "green":
            green, line = QColor(color("green")), QColor(color("line"))
            border_color = QColor(
                *(
                    round(a * 0.28 + b * 0.72)
                    for a, b in zip((green.red(), green.green(), green.blue()), (line.red(), line.green(), line.blue()))
                )
            ).name()
        self.setStyleSheet(f"""
            QFrame#updateCheckFeedback {{
                background: {color(background)}; border: 1px solid {border_color}; border-radius: 6px;
            }}
            QLabel#updateCheckTitle {{
                font-size: {BODY_FONT_SIZE}px; font-weight: 600; color: {color(tone)};
            }}
            QLabel#updateCheckDetail, QLabel#updateCheckElapsed, QLabel#updateCheckHint {{
                font-size: {BODY_FONT_SIZE}px; color: {color("secondary")};
            }}
            QLabel#updateCheckHint {{ padding-top: 3px; }}
            QPushButton#updateCheckAction {{
                font-size: {BODY_FONT_SIZE}px; color: {color(tone)}; background: transparent; border: 0;
                text-decoration: underline; padding: 0 1px; min-height: 22px; max-height: 22px;
            }}
            QPushButton#updateCheckAction:focus {{ border: 1px solid {color(tone)}; }}
        """)
        self.progress.tone = tone
        self.hint.setText(tr("可继续等待或取消检测。"))

    def _tick(self) -> None:
        """由当前快照和单调时钟计算阶段、耗时及三秒无进展提示，并更新真实进度和可选动画。"""
        state = self._snapshot
        phase = state.get("phase", "idle")
        active = state.get("checking", False) and phase != "idle"
        now = monotonic()
        started = state.get("started_at", 0.0)
        end = now if active else state.get("finished_at", 0.0)
        elapsed = max(0, end - started) if started and end else 0
        total, done = state.get("total", 0), state.get("done", 0)
        failed, found = state.get("failed", 0), state.get("found", 0)
        retry = state.get("retry_only", False)
        waiting = active and now - state.get("last_progress_at", now) >= 3
        tone, symbol = "accent", "refresh"
        action = tr("取消检测")
        if phase == "preparing":
            title = tr("正在准备更新检测…")
            detail = tr("正在读取本地更新信息，已保留上次检测结果。")
            self.progress.setRange(0, 0)
            self.progress.setAccessibleName(tr("正在准备更新检测"))
        elif phase == "connecting":
            title = tr("正在重新连接 · 仅重试失败项") if retry else tr("正在连接 GameBanana…")
            detail = tr("正在获取新版本信息，已保留上次检测结果。")
            if waiting:
                title = tr("连接响应较慢，仍在等待")
                detail = tr("正在等待 GameBanana 响应，可以取消后稍后重试。")
            self.progress.setRange(0, 0)
            self.progress.setAccessibleName(tr("正在连接更新服务"))
        elif phase == "checking":
            title = tr(
                "{value} · {done} / {total} 个 Mod",
                value=tr("正在重试") if retry else tr("正在检测"),
                done=done,
                total=total,
            )
            detail = (
                tr("网络响应较慢，正在等待后续结果。") if waiting else tr("已发现 {found} 个可用更新。", found=found)
            ) + tr("检测完成前保留上次列表。")
            self.progress.setRange(0, max(1, total))
            self.progress.setValue(done)
            self.progress.setAccessibleName(tr("已完成检测 {done} / {total} 个 Mod", done=done, total=total))
        elif phase == "partial":
            title = (
                tr(
                    "已检测 {value} / {total} 个 Mod · {failed} 项失败",
                    value=max(0, total - failed),
                    total=total,
                    failed=failed,
                )
                if total
                else tr("{failed} 项检测失败", failed=failed)
            )
            detail = tr(
                "已发现 {available} 个可用更新；{failed} 项检测失败，可单独重试。",
                available=self._available,
                failed=failed,
            )
            tone, symbol, action = "warning", "warning", tr("重试 {failed} 项", failed=failed)
        elif phase == "done":
            title = (
                tr("检测完成 · {available} 个可用更新", available=self._available)
                if self._available
                else tr("检测完成 · 暂无可用更新")
                if found
                else tr("检测完成 · 全部已是最新")
                if total
                else tr("检测完成 · 暂无检测项")
            )
            detail = (
                (tr("失败项重试成功 · ") if retry else "")
                + tr("已检测 {total} / {total_2} 个 Mod · 刚刚", total=total, total_2=total)
                if total
                else tr("未找到可检测的 Mod，请确认更新来源或检测设置。")
            )
            tone, symbol, action = "green", "check-circle", tr("收起")
        elif phase == "failed":
            title = tr("检查准备失败")
            detail = state.get("error") or tr("无法读取本地更新信息，请稍后重新检测。")
            tone, symbol, action = "warning", "warning", tr("收起")
        elif phase == "cancelled":
            title, detail = tr("已取消检测"), tr("上次检测结果已保留，可以稍后重新检测。")
            tone, symbol, action = "secondary", "info", tr("收起")
        else:
            title = detail = ""
        if waiting:
            tone = "warning"
        self._style(tone)
        self.title.setText(title)
        self.detail.setText(detail)
        self.elapsed.setText(
            tr("{value} {value_2} 秒", value=tr("已用时") if active else tr("用时"), value_2=int(elapsed))
        )
        self.elapsed.setVisible(bool(started))
        self.action.setText(action)
        self._progress_wrap.setVisible(active)
        self.progress.setVisible(active)
        self.hint.setVisible(waiting and not self._short)
        if active and anim.is_enabled():
            angle = elapsed * 360 % 360
            self._icon.setPixmap(refresh_icon(angle, tone).pixmap(14, 14))
            self.spin_angle_changed.emit(angle)
            self.progress.sweep = self._sweep_ease.valueForProgress((elapsed % 1.5) / 1.5)
            self.progress.update()
        elif self._symbol != (symbol, tone):
            icons.apply_pixmap(self._icon, f"mod-updates/{symbol}", 14, f"mod_updates.{tone}")
        self._symbol = (symbol, tone)

    def _activate(self) -> None:
        """检查期间发出取消请求，部分失败时发出重试请求，其余终态仅收起当前提示。"""
        if self._snapshot.get("checking"):
            self.cancel_requested.emit()
        elif self._snapshot.get("phase") == "partial":
            self.retry_requested.emit()
        else:
            self._dismissed = True
            self.hide()

    def _sync_timer(self) -> None:
        """仅在面板可见且检查运行时刷新，启用动画用四十毫秒间隔，否则用二百毫秒间隔。"""
        if self.isVisible() and self._snapshot.get("checking"):
            self._timer.start(40 if anim.is_enabled() else 200)
        else:
            self._timer.stop()

    def showEvent(self, event) -> None:
        """重新显示时立即刷新检查反馈，并按任务状态恢复定时器。"""
        super().showEvent(event)
        self._tick()
        self._sync_timer()

    def hideEvent(self, event) -> None:
        """隐藏时停止定时刷新，避免不可见反馈持续占用界面线程。"""
        self._timer.stop()
        super().hideEvent(event)
