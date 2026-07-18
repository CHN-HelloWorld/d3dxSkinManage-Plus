"""提供旋转指示器及图片加载占位。

图片占位共用动画驱动，仅在有可见目标时运行；动画禁用时显示静态底色。
"""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QColor,
    QGradient,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from src.ui import anim, theme
from src.utils.i18n import N_, t

# Spinner（转圈）


class Spinner(QWidget):
    """旋转加载指示器（固定尺寸，连续旋转一段弧）。"""

    def __init__(self, parent: QWidget | None = None, *, size: int = 28, line_width: int = 3) -> None:
        """创建固定尺寸的旋转指示器，并配置线性无限循环的角度属性动画。"""
        super().__init__(parent)
        self._size = size
        self._lw = line_width
        self._angle = 0
        self.setFixedSize(size, size)
        self._anim = QPropertyAnimation(self, b"angle", self)
        self._anim.setStartValue(0)
        self._anim.setEndValue(360)
        self._anim.setDuration(900)
        self._anim.setLoopCount(-1)
        self._anim.setEasingCurve(QEasingCurve.Type.Linear)

    def _get_angle(self) -> int:
        """返回当前进度弧的旋转角度。"""
        return self._angle

    def _set_angle(self, value: int) -> None:
        """保存动画推进的角度并请求重绘。"""
        self._angle = value
        self.update()

    angle = Property(int, _get_angle, _set_angle)

    def start(self) -> None:
        """在界面动画启用且当前未运行时启动旋转动画。"""
        if anim.is_enabled() and self._anim.state() != QPropertyAnimation.State.Running:
            self._anim.start()

    def stop(self) -> None:
        """停止旋转属性动画。"""
        self._anim.stop()

    def showEvent(self, event) -> None:
        """控件显示后按全局动画设置启动旋转。"""
        super().showEvent(event)
        self.start()

    def hideEvent(self, event) -> None:
        """控件隐藏时停止旋转，避免不可见动画占用刷新资源。"""
        self.stop()
        super().hideEvent(event)

    def sizeHint(self) -> QSize:
        """返回旋转指示器配置的正方形尺寸。"""
        return QSize(self._size, self._size)

    def paintEvent(self, _event) -> None:
        """绘制主题色圆形轨道及随角度变化的一百角度进度弧。"""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        inset = self._lw
        rect = QRectF(inset, inset, self._size - 2 * inset, self._size - 2 * inset)
        track = QPen(QColor(theme.color("stroke.control")))
        track.setWidth(self._lw)
        track.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(track)
        p.drawEllipse(rect)
        arc = QPen(QColor(theme.color("accent")))
        arc.setWidth(self._lw)
        arc.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(arc)
        p.drawArc(rect, int(-self._angle * 16), int(100 * 16))
        p.end()


class LoadingIndicator(QWidget):
    """居中的「旋转指示器 + 文案」加载占位（整块区域用）。"""

    def __init__(self, text: str = N_("正在加载…"), parent: QWidget | None = None) -> None:
        """创建垂直居中的旋转指示器和可翻译的加载提示文字。"""
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)  # 左撑
        self.spinner = Spinner(size=32, line_width=3)
        row.addWidget(self.spinner)
        row.addStretch(1)  # 两侧伸缩项共同使内容居中。
        outer.addLayout(row)
        self._label = QLabel(t(text), alignment=Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet(f"color: {theme.color('text.secondary')};")
        outer.addSpacing(10)
        outer.addWidget(self._label)
        outer.addStretch(1)

    def set_text(self, text: str) -> None:
        """翻译并替换加载提示文字。"""
        self._label.setText(t(text))


# Shimmer（骨架微光）—— 共享驱动 + 占位控件 + 自管 QLabel


class _ShimmerDriver:
    """共用的加载占位相位驱动，以约每秒三十帧更新可见控件。

    没有可见目标时停止计时；动画禁用时保留静态占位。
    """

    def __init__(self) -> None:
        """初始化共享微光相位、活动控件集合和统一刷新定时器。"""
        self.phase = 0.0
        self._widgets: set = set()
        self._timer = QTimer()
        self._timer.setInterval(20)  # ~50fps，丝滑且开销可控
        self._timer.timeout.connect(self._tick)

    def _tick(self) -> None:
        # 相位 0→1 线性推进；配合 paintEvent 的 RepeatSpread「平移整整一个周期」，
        # phase 从 0.999…回卷到 0 时图案与起点完全重合 → 无缝循环（无首尾跳变）。
        """推进共享微光相位并刷新已注册控件，同时移除已销毁的 Qt 对象。"""
        self.phase = (self.phase + 0.013) % 1.0  # ≈1.5s 一个循环
        for w in list(self._widgets):
            try:
                w.update()
            except RuntimeError:  # C++ 对象已销毁
                self._widgets.discard(w)

    def register(self, widget) -> None:
        """将可见骨架控件加入共享刷新集合，并在允许动画时启动统一计时器。"""
        self._widgets.add(widget)
        if anim.is_enabled() and not self._timer.isActive():
            self._timer.start()

    def unregister(self, widget) -> None:
        """移除骨架控件，在没有活动控件时停止共享刷新计时器。"""
        self._widgets.discard(widget)
        if not self._widgets and self._timer.isActive():
            self._timer.stop()


_driver: _ShimmerDriver | None = None


def _get_driver() -> _ShimmerDriver:
    # 懒建：避免在 QApplication 之前于模块导入期建 QTimer。
    """延迟创建并返回全局共享的骨架微光动画驱动器。"""
    global _driver
    if _driver is None:
        _driver = _ShimmerDriver()
    return _driver


class ShimmerPlaceholder(QWidget):
    """骨架微光占位块：圆角灰底 + 一条高光带匀速划过（skeleton screen）。

    可见时自动加入共享驱动、隐藏时退出。动画关闭时只画静态灰底（无扫光、无定时器）。
    """

    def __init__(self, parent: QWidget | None = None, *, radius: int = 4) -> None:
        """创建指定圆角半径的骨架占位层，并使其不阻挡鼠标事件。"""
        super().__init__(parent)
        self._radius = radius
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def start(self) -> None:
        """新绑定不受旧图像淡出动画影响。"""
        anim._cancel_fade(self)
        self.show()
        self.raise_()
        if self.isVisible():
            _get_driver().register(self)

    def showEvent(self, event) -> None:
        """显示时注册到共享微光刷新驱动器。"""
        super().showEvent(event)
        _get_driver().register(self)

    def hideEvent(self, event) -> None:
        """隐藏时退出共享刷新集合，避免无效绘制请求。"""
        if _driver is not None:
            _driver.unregister(self)
        super().hideEvent(event)

    def reveal(self) -> None:
        """图片到达后结束加载占位。

        可见时暂停扫光并淡出，不可见时直接隐藏，避免后续显示时遮挡已完成图片。
        """
        if self.isHidden():
            return
        if _driver is not None:
            _driver.unregister(self)
        if self.isVisible():
            anim.fade_out(self)
        else:
            self.hide()

    def paintEvent(self, _event) -> None:
        """绘制适应明暗主题的圆角底色，并在允许动画时叠加随共享相位移动的渐变高光。"""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(rect, self._radius, self._radius)
        # 配色：base = 骨架底；hi = 更亮的扫光高光（亮色用白、暗色用更浅灰；勿用 bg.hover）。
        dark = theme.current_theme() == "dark"
        base = QColor(theme.color("bg.subtle"))
        if dark:
            hi = base.lighter(142)
        else:
            base = base.darker(106)
            hi = QColor("#fafafa")  # 使用柔和的浅色高光。
        p.fillPath(path, base)
        if anim.is_enabled() and rect.width() > 1:
            p.setClipPath(path)
            w = rect.width()
            # 使用周期渐变并按完整周期移动，使循环首尾图案一致。
            # 周期大于卡片宽度，减少同时出现的高光条数量。
            period = w * 1.5
            x0 = (self._driver_phase() % 1.0) * period
            grad = QLinearGradient(x0, 0.0, x0 + period, 0.0)
            grad.setSpread(QGradient.Spread.RepeatSpread)
            grad.setColorAt(0.0, base)
            grad.setColorAt(0.38, base)
            grad.setColorAt(0.5, hi)
            grad.setColorAt(0.62, base)
            grad.setColorAt(1.0, base)
            p.fillRect(rect, grad)
        p.end()

    @staticmethod
    def _driver_phase() -> float:
        """返回共享骨架动画当前的归一化相位。"""
        return _get_driver().phase


class ShimmerLabel(QLabel):
    """带动态加载占位的图片标签。

    start_loading 显示占位，设置有效图像或文本时自动隐藏，统一管理加载结束状态。
    """

    def __init__(self, parent: QWidget | None = None, *, radius: int = 4) -> None:
        """在图片标签内创建默认隐藏的骨架占位层。"""
        super().__init__(parent)
        self._shimmer = ShimmerPlaceholder(self, radius=radius)
        self._shimmer.hide()

    def start_loading(self) -> None:
        """清空原有文字和图片，使骨架层覆盖标签并开始加载动画。"""
        super().setText("")
        super().setPixmap(QPixmap())  # 清空旧图
        self._shimmer.setGeometry(self.rect())
        self._shimmer.start()

    def _stop_loading(self) -> None:
        """隐藏骨架占位层以结束加载反馈。"""
        self._shimmer.hide()

    def setPixmap(self, pixmap) -> None:
        """设置图片，并在有效图像到达时触发骨架层的揭示动画。"""
        if pixmap is not None and not pixmap.isNull():
            super().setPixmap(pixmap)  # 图先铺底
            self._shimmer.reveal()  # 设置有效内容后结束加载占位。
            return
        super().setPixmap(pixmap)

    def setText(self, text) -> None:
        """停止骨架反馈后显示指定文字。"""
        self._stop_loading()
        super().setText(text)

    def resizeEvent(self, event) -> None:
        """标签尺寸变化时同步仍可见的骨架层几何区域。"""
        super().resizeEvent(event)
        if not self._shimmer.isHidden():
            self._shimmer.setGeometry(self.rect())
