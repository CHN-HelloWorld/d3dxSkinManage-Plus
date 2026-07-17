"""在操作按钮外圈显示下载进度。

中心图标表示暂停、继续、打开或重试，负进度值隐藏进度环。
绘制时读取当前主题颜色，主题变化后通过 retheme 更新缓存图标。
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QAbstractButton

from src.ui import icons, theme


class RingProgressButton(QAbstractButton):
    """环形进度 + 中心图标的可点击按钮。"""

    #: 不确定态旋转：每帧步进角度 / 帧间隔（约 60fps）
    _SPIN_STEP = 6
    _SPIN_MS = 16

    def __init__(self, parent=None, *, size: int = 32, line_width: int = 3) -> None:
        """初始化固定尺寸的图标按钮、进度环颜色和不确定进度的旋转状态。"""
        super().__init__(parent)
        self._size = size
        self._lw = line_width
        self._progress: float = -1.0  # <0：不画进度环（纯图标按钮）
        self._icon_name: str = ""
        self._icon_color: str = "text.primary"
        self._ring_color: str = "accent"
        self._pix: QPixmap | None = None
        self._indeterminate = False
        self._spin_deg = 0
        self._spin_timer: QTimer | None = None
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # 打标：中心图标 pixmap 是自绘的（走 icons.pixmap），不是 setIcon/apply_icon，
        # 全局 icons.retheme_icons 的属性遍历扫不到它。给它挂个标记，让 retheme_icons
        # 见到就调 .retheme 重建图标——否则换主题后环形按钮的图标颜色会残留旧色。
        self.setProperty("_ring_progress_button", True)

    def sizeHint(self) -> QSize:
        """返回进度按钮配置的正方形逻辑尺寸。"""
        return QSize(self._size, self._size)

    # 配置
    def set_progress(self, value: float) -> None:
        """设置进度（0~100）；value<0 表示隐藏进度环。"""
        v = float(value)
        if v != self._progress:
            self._progress = v
            self.update()

    def set_ring_color(self, token: str) -> None:
        """在主题颜色标记变化时更新进度弧颜色并请求重绘。"""
        if token != self._ring_color:
            self._ring_color = token
            self.update()

    def set_indeterminate(self, on: bool) -> None:
        """切换用于未知总量的旋转进度。

        通过计时器驱动自绘圆弧，仅在不确定状态运行；确定进度时停止计时器。
        """
        on = bool(on)
        if on == self._indeterminate:
            return
        self._indeterminate = on
        if on:
            if self._spin_timer is None:
                self._spin_timer = QTimer(self)
                self._spin_timer.setInterval(self._SPIN_MS)
                self._spin_timer.timeout.connect(self._advance_spin)
            self._spin_timer.start()
        elif self._spin_timer is not None:
            self._spin_timer.stop()
        self.update()

    def _advance_spin(self) -> None:
        # 不可见时不重绘：面板滑出后计时器可能还在跑，避免无谓的合成开销。
        """仅在按钮可见时推进不确定进度的旋转角度并请求重绘。"""
        if not self.isVisible():
            return
        self._spin_deg = (self._spin_deg + self._SPIN_STEP) % 360
        self.update()

    def set_icon(self, name: str, color_token: str = "text.primary") -> None:
        """更新中心图标及其主题颜色，参数未变且已有缓存时保留原像素图。"""
        if name == self._icon_name and color_token == self._icon_color and self._pix is not None:
            return
        self._icon_name = name
        self._icon_color = color_token
        self._rebuild_pixmap()
        self.update()

    def retheme(self) -> None:
        """换主题后重建中心图标 pixmap（环色由 paintEvent 现取，无需缓存）。"""
        self._rebuild_pixmap()
        self.update()

    def _icon_px_size(self) -> int:
        """返回按钮尺寸一半且至少十二像素的中心图标尺寸。"""
        return max(12, int(self._size * 0.5))

    def _rebuild_pixmap(self) -> None:
        """按当前图标名称和颜色重新生成像素图，空名称或加载异常时清空图标。"""
        if not self._icon_name:
            self._pix = None
            return
        try:
            self._pix = icons.pixmap(self._icon_name, self._icon_px_size(), self._icon_color)
        except Exception:
            self._pix = None

    # 绘制
    def paintEvent(self, _e) -> None:
        """绘制悬停底色、确定或旋转进度弧及中心图标，并在禁用时降低图标透明度。"""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # hover 淡圆底（仅可用时）
        if self.underMouse() and self.isEnabled():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.color("bg.hover")))
            p.drawEllipse(QRectF(0.5, 0.5, self._size - 1, self._size - 1))
            p.setBrush(Qt.BrushStyle.NoBrush)

        # 进度环（progress<0 且非不确定态则不画）
        if self._indeterminate or self._progress >= 0:
            inset = self._lw + 1
            rect = QRectF(inset, inset, self._size - 2 * inset, self._size - 2 * inset)
            track = QPen(QColor(theme.color("stroke.control")))
            track.setWidth(self._lw)
            track.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(track)
            p.drawEllipse(rect)
            arc_pen = QPen(QColor(theme.color(self._ring_color)))
            arc_pen.setWidth(self._lw)
            arc_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            if self._indeterminate:
                # 固定 1/4 圈弧长，起点随 _spin_deg 顺时针跑。
                p.setPen(arc_pen)
                start = (90 - self._spin_deg) * 16
                p.drawArc(rect, int(start), int(-90 * 16))
            elif self._progress > 0:
                p.setPen(arc_pen)
                # Qt 角度单位 = 1/16 度；从 12 点(90°)起、顺时针(span 取负)。
                span = int(-min(100.0, self._progress) / 100.0 * 360 * 16)
                p.drawArc(rect, 90 * 16, span)

        # 中心图标
        if self._pix is not None and not self._pix.isNull():
            isz = self._icon_px_size()
            x = (self._size - isz) // 2
            y = (self._size - isz) // 2
            p.setOpacity(1.0 if self.isEnabled() else 0.4)
            p.drawPixmap(QRect(x, y, isz, isz), self._pix)
            p.setOpacity(1.0)
        p.end()

    def enterEvent(self, e) -> None:
        """保留默认鼠标进入处理并刷新悬停背景。"""
        super().enterEvent(e)
        self.update()

    def leaveEvent(self, e) -> None:
        """保留默认鼠标离开处理并清除悬停背景。"""
        super().leaveEvent(e)
        self.update()
