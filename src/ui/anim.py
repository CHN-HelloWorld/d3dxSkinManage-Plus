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
