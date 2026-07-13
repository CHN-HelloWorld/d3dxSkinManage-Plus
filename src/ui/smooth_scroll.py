"""为滚轮滚动提供指数缓动，连续滚动累加目标。

关闭动画、触摸板像素滚动、缩放手势及不可滚动区域继续使用 Qt 原生处理。
键盘、拖动滚动条和程序定位会同步调整缓动目标。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QAbstractScrollArea

from src.ui import anim


class SmoothScroller(QObject):
    """把某个 ``QAbstractScrollArea`` 的垂直滚轮变为平滑缓动（单实例随 area 生命周期）。"""

    FPS_MS = 16  # ~60fps 驱动
    ALPHA = 0.20  # 每帧向目标推进的比例：越小越「柔」、滑得越久
    STEP_PX = 100  # 一个标准滚轮 notch（angleDelta=120）对应的滚动像素量
    SNAP_PX = 1  # 距目标 ≤ 此像素即吸附收尾，停驱动

    def __init__(self, area: QAbstractScrollArea) -> None:
        """接管滚动区域视口的滚轮事件，并同步滚动条外部变化与缓动目标。"""
        super().__init__(area)
        self._area = area
        self._bar = area.verticalScrollBar()
        # 目标位（float 累加，避免逐帧取整误差堆积）；初值=当前。
        self._target: float = float(self._bar.value())
        self._driving = False  # 缓动自身在改 value 时置 True，用以区分「外部改动」
        self._timer = QTimer(self)
        self._timer.setInterval(self.FPS_MS)
        self._timer.timeout.connect(self._tick)
        # 滚轮事件经 viewport 路由（子控件忽略 wheel 会冒泡到此），装过滤器接管。
        area.viewport().installEventFilter(self)
        # 外部（拖滚动条 / 键盘 / 程序滚动）改了 value → 同步 target，避免缓动把它拽回去。
        self._bar.valueChanged.connect(self._on_bar_changed)

    @classmethod
    def attach(cls, area: QAbstractScrollArea) -> SmoothScroller:
        """给 area 装上平滑滚动并返回该实例（实例 parent=area，无需自行持有）。"""
        return cls(area)

    def cancel(self) -> None:
        """撤销旧视图的滚轮目标，包括恢复到相同数值的情况。"""
        self._timer.stop()
        self._target = float(self._bar.value())

    # 事件接管
    def eventFilter(self, obj, event) -> bool:
        """将普通垂直滚轮转换为累加缓动目标；触摸板、缩放手势及不可滚动区域交回 Qt。"""
        if event.type() != QEvent.Type.Wheel or obj is not self._area.viewport():
            return False
        if not anim.is_enabled():
            return False  # 动画关 → 原生即时滚动，零接管
        if not isinstance(event, QWheelEvent):
            return False
        # 触摸板逐像素滚动本就平滑；Ctrl+滚轮多为缩放——都交回原生。
        if not event.pixelDelta().isNull():
            return False
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            return False
        dy = event.angleDelta().y()
        if dy == 0:
            return False
        lo, hi = self._bar.minimum(), self._bar.maximum()
        if hi <= lo:
            return False  # 不可滚动 → 交回原生（让事件继续冒泡，如父级可滚）
        # 累加目标（向下滚 dy<0 → value 增大），clamp 到可滚范围。
        self._target = max(lo, min(hi, self._target - dy / 120.0 * self.STEP_PX))
        if not self._timer.isActive():
            self._timer.start()
        return True  # 消耗原生「瞬跳」

    def _on_bar_changed(self, value: int) -> None:
        # 非缓动驱动的 value 变化（拖滚动条 / 键盘 / 内容增减触发的 clamp）→ 跟随，
        # 让下一帧 _tick 看到 diff≈0 自然停，不会把用户刚拖到的位置又缓回旧 target。
        """外部改变滚动位置时同步缓动目标，避免继续滚向旧位置。"""
        if not self._driving:
            self._target = float(value)

    # 缓动驱动
    def _tick(self) -> None:
        """按比例逼近目标并重新限制滚动范围，接近终点时对齐整数位置并停止定时器。"""
        cur = self._bar.value()
        lo, hi = self._bar.minimum(), self._bar.maximum()
        # 内容在滚动期间可能增减（无限滚动追加）→ 每帧重新 clamp 目标。
        self._target = max(float(lo), min(float(hi), self._target))
        diff = self._target - cur
        if abs(diff) <= self.SNAP_PX:
            self._driving = True
            self._bar.setValue(int(round(self._target)))
            self._driving = False
            self._timer.stop()
            return
        step = diff * self.ALPHA
        if -1.0 < step < 1.0:
            step = 1.0 if diff > 0 else -1.0  # 保底每帧至少挪 1px，避免亚像素停滞
        self._driving = True
        self._bar.setValue(cur + int(step))
        self._driving = False
