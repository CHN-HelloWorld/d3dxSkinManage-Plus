"""提供界面动画的统一时长、缓动与生命周期管理。

调用方先设置逻辑状态，再播放视觉过渡。动画结束后移除临时图形效果，
避免影响子控件及原生窗口合成。动画禁用或控件不可见时直接完成状态更新，
需要收尾的入口仍执行完成回调。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import (
    Property,
    QAbstractAnimation,
    QEasingCurve,
    QElapsedTimer,
    QEvent,
    QObject,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QRect,
    Qt,
    QTimer,
)
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QGraphicsOpacityEffect, QStackedWidget, QWidget

from src.utils.qt_timers import call_later

# 动画时长以毫秒计，调用方按用途复用。
DURATION = {"fast": 120, "normal": 160, "slow": 220}

# 进入和位移动画使用先快后慢的 OutCubic 缓动。
_EASE = QEasingCurve.Type.OutCubic
# 关闭动画使用加速位移，避免依赖会影响亚克力合成的窗口透明度。
_EASE_OUT = QEasingCurve.Type.InQuad

_enabled = True


def set_enabled(on: bool) -> None:
    """设置全局动画开关，关闭后各入口直接应用最终状态。"""
    global _enabled
    _enabled = bool(on)


def is_enabled() -> bool:
    """返回全局界面动画开关状态。"""
    return _enabled


# 淡入（QGraphicsOpacityEffect，结束后移除）


def _cancel_fade(widget: QWidget) -> None:
    """取消尚未完成的淡入并移除临时图形效果。"""
    anim = getattr(widget, "_fade_anim", None)
    if anim is not None:
        try:
            anim.stop()
        except RuntimeError:
            pass
        try:
            widget._fade_anim = None  # type: ignore[attr-defined]
        except (AttributeError, RuntimeError):
            pass
    try:
        if widget.graphicsEffect() is not None:
            widget.setGraphicsEffect(None)
    except RuntimeError:
        pass


def fade_in(
    widget: QWidget | None,
    duration: int | None = None,
    on_finished: Callable[[], None] | None = None,
) -> QPropertyAnimation | None:
    """将控件从透明过渡为不透明，结束后移除图形效果。

    动画未启用或控件不可用时直接执行完成回调并返回 None，否则返回动画对象。
    """
    if not _enabled or widget is None or not widget.isVisible():
        if on_finished:
            on_finished()
        return None

    _cancel_fade(widget)

    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(0.0)
    widget.setGraphicsEffect(effect)

    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(duration or DURATION["normal"])
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(_EASE)

    def _cleanup() -> None:
        # 仅移除当前动画持有的效果，保留期间新创建的替代效果。
        """移除本次淡入使用的透明度效果和保活引用，再调用完成回调。"""
        try:
            if widget.graphicsEffect() is effect:
                widget.setGraphicsEffect(None)
        except RuntimeError:
            pass
        try:
            widget._fade_anim = None  # type: ignore[attr-defined]
        except (AttributeError, RuntimeError):
            pass
        if on_finished:
            on_finished()

    anim.finished.connect(_cleanup)
    widget._fade_anim = anim  # type: ignore[attr-defined]
    anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    return anim


def fade_out(
    widget: QWidget | None,
    duration: int | None = None,
    on_finished: Callable[[], None] | None = None,
) -> QPropertyAnimation | None:
    """淡出控件后隐藏并移除图形效果。

    可用于图片上方加载占位的交叉过渡；无需动画时直接隐藏并执行完成回调。
    """
    if not _enabled or widget is None or not widget.isVisible():
        if widget is not None:
            try:
                widget.hide()
            except RuntimeError:
                pass
        if on_finished:
            on_finished()
        return None

    _cancel_fade(widget)
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(1.0)
    widget.setGraphicsEffect(effect)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(duration or DURATION["fast"])
    anim.setStartValue(1.0)
    anim.setEndValue(0.0)
    anim.setEasingCurve(_EASE)

    def _cleanup() -> None:
        """隐藏淡出控件并清理本次透明度效果，最后通知动画完成。"""
        try:
            widget.hide()  # 隐藏后 hideEvent 会退出 shimmer 驱动
            if widget.graphicsEffect() is effect:
                widget.setGraphicsEffect(None)
        except RuntimeError:
            pass
        try:
            widget._fade_anim = None  # type: ignore[attr-defined]
        except (AttributeError, RuntimeError):
            pass
        if on_finished:
            on_finished()

    anim.finished.connect(_cleanup)
    widget._fade_anim = anim  # type: ignore[attr-defined]
    anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    return anim


# 滑入（几何位移，适合下拉浮层；与亚克力 / 原生窗口兼容，不动 effect）


def slide_in(
    widget: QWidget | None,
    dy: int = 8,
    duration: int | None = None,
) -> QPropertyAnimation | None:
    """从最终位置上方滑入，调用前须完成目标几何设置。

    使用位移而不叠加图形透明效果，以保留浮层原生模糊背景。
    """
    if not _enabled or widget is None:
        return None
    end = widget.geometry()
    start = QRect(end.x(), end.y() - dy, end.width(), end.height())
    anim = QPropertyAnimation(widget, b"geometry", widget)
    anim.setDuration(duration or DURATION["fast"])
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.setEasingCurve(_EASE)
    anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    widget._slide_anim = anim  # type: ignore[attr-defined]  # 保活引用
    return anim


def slide_out(
    widget: QWidget | None,
    dy: int = 40,
    duration: int | None = None,
    on_finished: Callable[[], None] | None = None,
    easing: QEasingCurve.Type | None = None,
) -> QPropertyAnimation | None:
    """从当前位置向上滑出，并执行一次完成回调。

    几何动画保留窗口模糊合成；禁用动画、控件不可用或启动失败时立即回调。
    调用方可据此在动画结束后关闭对话框，并保持模态返回结果。
    """
    if not _enabled or widget is None:
        if on_finished is not None:
            on_finished()
        return None
    # 先停止未完成的入场动画，避免同时修改几何属性。
    prev = getattr(widget, "_slide_anim", None)
    if prev is not None:
        try:
            prev.stop()
        except RuntimeError:
            pass
    start = widget.geometry()
    end = QRect(start.x(), start.y() - dy, start.width(), start.height())
    anim = QPropertyAnimation(widget, b"geometry", widget)
    anim.setDuration(duration or DURATION["fast"])
    anim.setStartValue(start)
    anim.setEndValue(end)
    anim.setEasingCurve(easing or _EASE_OUT)
    if on_finished is not None:
        anim.finished.connect(on_finished)
    anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    widget._slide_anim = anim  # type: ignore[attr-defined]  # 保活引用
    return anim


def animate_fixed_width(
    widget: QWidget | None,
    start: int,
    end: int,
    duration: int | None = None,
    on_finished: Callable[[], None] | None = None,
    on_frame: Callable[[int], None] | None = None,
) -> QParallelAnimationGroup | None:
    """同步调整最小和最大宽度，使控件从起始宽度过渡到目标宽度。

    每帧的宽度约束一致，适用于分隔布局中的展开和收起。
    无需动画时直接设置目标宽度并回调；调用方在结束后按需恢复可拖动的尺寸约束。
    """
    # 停止现有宽度动画，避免快速反向切换时同时修改尺寸约束。
    prev = getattr(widget, "_width_anim", None)
    if prev is not None:
        try:
            prev.stop()
        except RuntimeError:
            pass

    # 首帧前反向切换可能使起止值相同，仍须停止旧动画，避免后续帧覆盖已恢复宽度。
    if not _enabled or widget is None or start == end:
        if widget is not None:
            widget.setFixedWidth(end)
        if on_frame:
            on_frame(end)
        if on_finished:
            on_finished()
        return None

    # 首帧前设置起始宽度，避免布局值跳变。
    widget.setMinimumWidth(start)
    widget.setMaximumWidth(start)

    group = QParallelAnimationGroup(widget)
    last_anim = None
    for prop in (b"minimumWidth", b"maximumWidth"):
        a = QPropertyAnimation(widget, prop, group)
        a.setDuration(duration or DURATION["normal"])
        a.setStartValue(start)
        a.setEndValue(end)
        a.setEasingCurve(_EASE)
        group.addAnimation(a)
        last_anim = a

    # 只从一个属性动画转发逐帧通知，避免同一帧重复触发相邻区域重排。
    if on_frame and last_anim is not None:
        last_anim.valueChanged.connect(lambda v: on_frame(int(v)))

    if on_finished:
        group.finished.connect(on_finished)
    group.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    widget._width_anim = group  # type: ignore[attr-defined]  # 保活引用
    return group


# 顶级窗口淡入（windowOpacity，原生 alpha，不用 QGraphicsEffect）

# 适用的顶层窗口使用原生透明度淡入，避免整棵控件树重复进行离屏合成。
# 原生亚克力窗口使用独立的位移动画入口。


def fade_in_window(
    window: QWidget | None,
    duration: int | None = None,
) -> QPropertyAnimation | None:
    """对顶层窗口应用整体透明度淡入。

    显示事件在首帧前设置动画起点，避免先完整显示再变透明。
    """
    if not _enabled or window is None:
        return None
    anim = QPropertyAnimation(window, b"windowOpacity", window)
    anim.setDuration(duration or DURATION["normal"])
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(_EASE)
    # 结束时恢复完全不透明，避免窗口保留中间状态。
    anim.finished.connect(lambda: _safe_set_window_opacity(window, 1.0))
    anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    window._win_fade_anim = anim  # type: ignore[attr-defined]  # 保活引用
    return anim


def _safe_set_window_opacity(window: QWidget, value: float) -> None:
    """设置窗口透明度，忽略动画期间窗口已经销毁的情况。"""
    try:
        window.setWindowOpacity(value)
    except RuntimeError:  # 窗口已析构
        pass


class _IntroFadeFilter(QObject):
    """首次显示时播放一次窗口淡入，随后移除事件过滤器。"""

    def __init__(self, target: QWidget, duration: int | None) -> None:
        """保存目标窗口和动画时长，并初始化首次显示标记。"""
        super().__init__(target)
        self._target = target
        self._duration = duration
        self._done = False

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        """仅在目标窗口首次显示时启动窗口淡入动画。"""
        if not self._done and obj is self._target and event.type() == QEvent.Type.Show:
            self._done = True
            fade_in_window(self._target, self._duration)
        return False


def install_intro_fade(
    window: QWidget | None,
    duration: int | None = None,
) -> None:
    """安装首次显示淡入过滤器，无需修改窗口基类；动画禁用时不安装。"""
    if not _enabled or window is None:
        return
    filt = _IntroFadeFilter(window, duration)
    window.installEventFilter(filt)
    window._intro_fade_filter = filt  # type: ignore[attr-defined]  # 保活引用


# 页面 / 分区进入：从下方上滑到位（纯几何位移，不用 effect）

# 页面包含 QScrollArea/QAbstractScrollArea，QGraphicsOpacityEffect 对其视口的合成不可靠。
# 位移动画可避开该限制，同时省去离屏 pixmap 渲染。


def _safe_move(widget: QWidget, pos: QPoint) -> None:
    """移动仍有效的控件，忽略动画期间对象销毁导致的异常。"""
    try:
        widget.move(pos)
    except RuntimeError:
        pass


# 切页交叉淡出：旧页快照盖在上面淡出，露出新页（= 淡出旧 → 淡入新）

# 快照自己绘制透明度，避免 QGraphicsOpacityEffect 的 drawSource 快捷路径在
# QWidget.render 的前景捕获中重复应用容器偏移。新页的滚动区与卡片 effect 不变。


class _PageSnapshot(QWidget):
    """以旧页面快照覆盖新页面完成淡出，超时、切页或尺寸变化时自动释放。"""

    def __init__(self, container: QWidget, snapshot: QPixmap, duration: int):
        """保存页面快照并启动到期清理，同时订阅容器尺寸、隐藏和切页事件。"""
        super().__init__(container)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._snapshot = snapshot
        self._opacity = 1.0
        self._duration = duration
        self._elapsed = QElapsedTimer()
        self._elapsed.start()
        self._finished = False
        self._expiry = QTimer(self)
        self._expiry.setSingleShot(True)
        self._expiry.setTimerType(Qt.TimerType.PreciseTimer)
        self._expiry.timeout.connect(self.finish)
        self._expiry.start(duration)
        container.installEventFilter(self)
        if isinstance(container, QStackedWidget):
            container.currentChanged.connect(self.finish)

    @Property(float)
    def opacity(self):
        """返回当前快照叠加层的不透明度。"""
        return self._opacity

    @opacity.setter
    def opacity(self, value):
        """更新快照不透明度并请求重绘，供属性动画驱动。"""
        self._opacity = value
        self.update()

    def paintEvent(self, event):
        # 界面任务可能延后首个动画帧，不能在清理定时器等待期间重放已过期页面。
        """在叠加层有效期内按当前透明度绘制旧页面快照。"""
        if self._finished or self._elapsed.elapsed() >= self._duration:
            return
        painter = QPainter(self)
        painter.setOpacity(self._opacity)
        painter.drawPixmap(0, 0, self._snapshot)

    def eventFilter(self, watched, event):
        """容器缩放或隐藏时立即结束快照过渡，避免使用过期几何。"""
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Hide):
            self.finish()
        return False

    def finish(self):
        """幂等结束过渡，撤销过滤器与保活引用，停止动画并延迟删除叠加层。"""
        if self._finished:
            return
        self._finished = True
        self._expiry.stop()
        # 立即隐藏旧快照，避免延迟删除前仍被前景捕获或嵌套事件循环显示。
        self.hide()
        container = self.parentWidget()
        container.removeEventFilter(self)
        if getattr(container, "_cross_fade_overlay", None) is self:
            container._cross_fade_overlay = None
        animation = getattr(self, "_cross_fade_anim", None)
        if animation is not None:
            animation.stop()
        self.deleteLater()


def cross_fade(container: QWidget | None, switch_fn, duration: int | None = None):
    """截取切换前内容，执行 switch_fn 后将旧快照覆盖在新内容上淡出。

    动画禁用、容器不可见或尺寸无效时直接切换；前次淡出未结束时也直接切换，
    避免快速切页反复截取整页图像。
    """
    prev = getattr(container, "_cross_fade_overlay", None)
    if prev is not None:
        # 上一轮淡出还在进行（快速连切）→ 清掉旧快照、本次直接切换不再 grab/动画
        try:
            prev.finish()
        except RuntimeError:
            pass
        container._cross_fade_overlay = None  # type: ignore[attr-defined]
        if switch_fn is not None:
            switch_fn()
        return None
    if (
        not _enabled
        or container is None
        or not container.isVisible()
        or container.width() <= 0
        or container.height() <= 0
    ):
        if switch_fn is not None:
            switch_fn()
        return None
    try:
        snapshot = container.grab()  # 旧内容快照（切换前）
    except RuntimeError:
        snapshot = QPixmap()
    if switch_fn is not None:
        switch_fn()
    if snapshot.isNull():
        return None
    duration = duration or DURATION["normal"]
    overlay = _PageSnapshot(container, snapshot, duration)
    overlay.setGeometry(0, 0, container.width(), container.height())
    overlay.show()
    overlay.raise_()
    container._cross_fade_overlay = overlay  # type: ignore[attr-defined]
    anim = QPropertyAnimation(overlay, b"opacity", overlay)
    anim.setDuration(duration)
    anim.setStartValue(1.0)
    anim.setEndValue(0.0)
    anim.setEasingCurve(_EASE)

    anim.finished.connect(overlay.finish)
    anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    overlay._cross_fade_anim = anim  # type: ignore[attr-defined]
    return anim


# 几何位移到目标（用于卡片重排：旧位 → 新位 平滑过渡）


def animate_move(
    widget: QWidget | None,
    target: QPoint,
    duration: int | None = None,
) -> QPropertyAnimation | None:
    """将控件平滑移动到目标位置，无需动画时直接定位。

    用于卡片重排，短时长可减少与滚动回收和直接定位的交叠。
    """
    if not _enabled or widget is None or not widget.isVisible():
        if widget is not None:
            _safe_move(widget, target)
        return None
    start = widget.pos()
    if start == target:
        return None
    prev = getattr(widget, "_move_anim", None)
    if prev is not None:
        try:
            prev.stop()
        except RuntimeError:
            pass
    anim = QPropertyAnimation(widget, b"pos", widget)
    anim.setDuration(duration or DURATION["fast"])
    anim.setStartValue(start)
    anim.setEndValue(target)
    anim.setEasingCurve(_EASE)
    anim.finished.connect(lambda: _safe_move(widget, target))
    anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    widget._move_anim = anim  # type: ignore[attr-defined]
    return anim


def animate_geometry(
    widget: QWidget | None,
    target: QRect,
    duration: int | None = None,
    on_finished: Callable[[], None] | None = None,
) -> QPropertyAnimation | None:
    """将控件几何过渡到目标状态。

    无需动画时直接设置几何；两种路径均调用完成回调，供隐藏浮层等收尾逻辑使用。
    """
    if not _enabled or widget is None or not widget.isVisible():
        if widget is not None:
            try:
                widget.setGeometry(target)
            except RuntimeError:
                pass
        if on_finished:
            on_finished()
        return None
    start = widget.geometry()
    if start == target:
        if on_finished:
            on_finished()
        return None
    prev = getattr(widget, "_geom_anim", None)
    if prev is not None:
        try:
            prev.stop()
        except RuntimeError:
            pass
    anim = QPropertyAnimation(widget, b"geometry", widget)
    anim.setDuration(duration or DURATION["fast"])
    anim.setStartValue(start)
    anim.setEndValue(target)
    anim.setEasingCurve(_EASE)
    if on_finished:
        anim.finished.connect(on_finished)
    anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    widget._geom_anim = anim  # type: ignore[attr-defined]
    return anim


# 交错淡入（列表/卡片视图「逐项显示」）


def stagger_reveal(
    cards: list,
    step: int = 26,
    duration: int | None = None,
    max_total: int = 320,
) -> None:
    """按间隔逐项淡入一组控件，并限制总等待时间。

    先同步设置透明状态，再依次显示，避免首帧闪烁。
    延迟期间控件可能被回收，访问前需检查对象及效果是否仍有效。
    """
    if not _enabled or not cards:
        return
    dur = duration or DURATION["normal"]
    # 先同步设置透明状态，避免首帧完整显示。
    # 新加入的卡片可能尚未完成可见性传播，因此不以即时可见性跳过整批动画。
    prepared: list = []
    for card in cards:
        try:
            if card is None:
                continue
            eff = QGraphicsOpacityEffect(card)
            eff.setOpacity(0.0)
            card.setGraphicsEffect(eff)
            prepared.append((card, eff))
        except RuntimeError:
            continue
    for i, (card, eff) in enumerate(prepared):
        delay = min(i * step, max_total)
        call_later(delay, card, lambda c=card, e=eff, d=dur: _run_staggered_fade(c, e, d))


def _run_staggered_fade(card: QWidget, eff: QGraphicsOpacityEffect, dur: int) -> None:
    """为仍持有预期透明度效果的卡片启动淡入，并保存动画引用至结束。"""
    try:
        # 延迟期间控件或效果已销毁时跳过。
        if card.graphicsEffect() is not eff:
            return  # 效果已被替换或清除时结束当前动画。
        anim = QPropertyAnimation(eff, b"opacity", card)
        anim.setDuration(dur)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(_EASE)

        def _cleanup() -> None:
            """仅在卡片仍使用本次效果时移除效果，避免覆盖后续动画。"""
            try:
                if card.graphicsEffect() is eff:
                    card.setGraphicsEffect(None)
            except RuntimeError:
                pass

        anim.finished.connect(_cleanup)
        anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        card._stagger_anim = anim  # type: ignore[attr-defined]
    except RuntimeError:
        pass
