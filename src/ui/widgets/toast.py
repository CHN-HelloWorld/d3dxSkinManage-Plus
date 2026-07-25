"""显示应用内操作进度与完成提示。

通知实例可复用，显示动画完成后再启动外观变更等耗时操作；
连续请求复用当前通知，不重复播放入场动画。
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPropertyAnimation, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel

from src.ui import anim as ui_anim
from src.ui import icons, theme
from src.ui.widgets.spinner import Spinner


class Toast(QFrame):
    """右上角滑入的轻量通知条。"""

    MARGIN = 18
    SLIDE_MS = 220

    def __init__(self, parent) -> None:
        """创建穿透鼠标的通知浮层、状态图标及文字，并初始化入场完成和自动隐藏计时器。"""
        super().__init__(parent)
        self.setObjectName("appToast")
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 16, 10)
        lay.setSpacing(10)
        self._spinner = Spinner(size=16, line_width=2)
        lay.addWidget(self._spinner)
        self._icon = QLabel()
        self._icon.setFixedSize(18, 18)
        self._icon.hide()
        lay.addWidget(self._icon)
        self._label = QLabel("")
        lay.addWidget(self._label)

        self._anim: QPropertyAnimation | None = None
        self._entering = False
        self._shown_callbacks = []
        self._ready_timer = QTimer(self)
        self._ready_timer.setSingleShot(True)
        self._ready_timer.timeout.connect(self._notify_shown)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._slide_out)
        self._apply_style()
        self.hide()

    # 样式（随当前主题取色）
    def _apply_style(self) -> None:
        """将通知文字同步到当前主题，仅在样式内容变化时重新设置样式表。"""
        css = (
            "#appToast { background: transparent; border: none; }"
            f"#appToast QLabel {{ color: {theme.color('text.primary')};"
            f" font-size: 12px; background: transparent; }}"
        )
        if self.styleSheet() != css:
            self.setStyleSheet(css)

    def changeEvent(self, event) -> None:
        """响应样式变化并重绘通知背景，在控件构造完成后同步主题文字样式。"""
        super().changeEvent(event)
        if event.type() == QEvent.Type.StyleChange and hasattr(self, "_label"):
            self._apply_style()
            self.update()

    def paintEvent(self, event) -> None:
        # 完整绘制圆角表面并清空外侧像素，避免样式更新留下方形底色。
        """使用源合成模式绘制圆角通知卡片及边框，避免透明背景叠加残影。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.setPen(QPen(QColor(theme.color("stroke.card")), 1))
        painter.setBrush(QColor(theme.color("bg.card")))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)

    # 对外 API
    def busy(self, text: str, on_shown=None) -> None:
        """显示「处理中」态（转圈 + 文案），从右侧滑入。

        ``on_shown`` 在滑入动画结束后调用，供同步换肤等耗时操作使用，避免其阻塞动画绘制。
        """
        was_visible = self.isVisible()
        self._hide_timer.stop()
        self._apply_style()
        self._label.setText(text)
        self._icon.hide()
        self._spinner.show()
        self._spinner.start()
        self.adjustSize()
        if on_shown is not None:
            self._shown_callbacks.append(on_shown)
        if self._entering:
            if self._anim is not None:
                self._anim.setEndValue(self._target_pos())
        elif was_visible:
            self._stop_anim()
            self._reposition(visible=True)
            self.raise_()
            self._ready_timer.start(0)
        else:
            self._entering = True
            self._slide_in(on_done=self._entry_finished)

    def _entry_finished(self) -> None:
        """结束入场状态、校正可见位置，并排队通知等待浮层显示完成的回调。"""
        self._entering = False
        self._reposition(visible=True)
        # 最后动画帧结束后再排队处理，不按固定墙钟时间猜测完成状态。
        self._ready_timer.start(0)

    def _notify_shown(self) -> None:
        """取走并清空显示完成回调队列，然后依次执行一次。"""
        callbacks, self._shown_callbacks = self._shown_callbacks, []
        for callback in callbacks:
            callback()

    def done(self, text: str, *, ok: bool = True, hold_ms: int = 1300) -> None:
        """切到「完成」态（✓ 图标 + 文案），停留 ``hold_ms`` 后滑出。"""
        self._stop_anim()
        self._entering = False
        if self._shown_callbacks:
            self._ready_timer.start(0)
        self._apply_style()
        self._spinner.stop()
        self._spinner.hide()
        token = "success" if ok else "danger"
        name = "status/installed" if ok else "status/warning"
        try:
            icons.apply_pixmap(self._icon, name, 16, token)
            self._icon.show()
        except Exception:
            self._icon.hide()
        self._label.setText(text)
        self.adjustSize()
        # 若当前不可见（极少：busy 没来得及显示），先滑入再排程滑出
        if not self.isVisible():
            self._slide_in()
        else:
            self._reposition(visible=True)
        self._hide_timer.start(hold_ms)

    # 几何 / 动画
    def _target_pos(self) -> QPoint:
        """计算通知位于父控件右上角且保留边距的目标位置。"""
        p = self.parent()
        pw = p.width() if p is not None else 0
        x = max(self.MARGIN, pw - self.width() - self.MARGIN)
        return QPoint(x, self.MARGIN)

    def _offscreen_pos(self) -> QPoint:
        """返回父控件右边界之外的退场位置。"""
        p = self.parent()
        pw = p.width() if p is not None else 0
        return QPoint(pw, self.MARGIN)

    def _reposition(self, *, visible: bool) -> None:
        """根据可见状态将通知立即移动到显示位置或屏幕外位置。"""
        self.move(self._target_pos() if visible else self._offscreen_pos())

    def _stop_anim(self) -> None:
        """停止并延迟销毁当前位移动画，容忍动画的 Qt 对象已经销毁。"""
        if self._anim is not None:
            try:
                self._anim.stop()
                self._anim.deleteLater()
            except RuntimeError:
                pass
            self._anim = None

    def _start_anim(self, start: QPoint, end: QPoint, on_done=None) -> None:
        """显示并置顶通知，在指定起止位置间执行缓出动画，结束时调用可选回调。

        禁用界面动画时直接移到终点并同步执行回调。
        """
        self._stop_anim()
        self.move(start)
        self.raise_()
        self.show()
        if not ui_anim.is_enabled():
            self.move(end)
            if on_done is not None:
                on_done()
            return
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setDuration(self.SLIDE_MS)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        if on_done is not None:
            anim.finished.connect(on_done)
        anim.start()
        self._anim = anim

    def _slide_in(self, on_done=None) -> None:
        """按内容调整浮层尺寸，并从右侧屏幕外滑入目标位置。"""
        self.adjustSize()
        self._start_anim(self._offscreen_pos(), self._target_pos(), on_done)

    def _slide_out(self) -> None:
        """将通知滑出父控件右边界，并在动画完成后隐藏控件。"""

        def _hide() -> None:
            """在退场动画结束时隐藏通知，忽略控件已经销毁的情况。"""
            try:
                self.hide()
            except RuntimeError:
                pass

        self._start_anim(self._target_pos(), self._offscreen_pos(), on_done=_hide)
