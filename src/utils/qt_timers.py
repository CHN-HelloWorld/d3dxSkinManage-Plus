"""随所有者销毁而释放回调的单次 Qt 定时器。"""

from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, Qt, QThread, QTimer


class _ContextTimer(QTimer):
    """等待期间归上下文所有，执行或取消时解除可能重入析构的父子关系。"""

    def event(self, event: QEvent) -> bool:
        """在回调或延迟销毁释放上下文最后引用前，将自身移出上下文的子对象列表。"""
        kind = event.type()
        if kind == QEvent.Type.DeferredDelete or (
            kind == QEvent.Type.Timer and self.isActive() and event.timerId() == self.timerId()
        ):
            self.setParent(None)
        return super().event(event)


def call_later(delay_ms: int, context: QObject, callback: Callable[[], object]) -> QTimer:
    """在上下文线程调度回调，并在上下文销毁时取消和释放。

    调用必须发生在同一 Qt 线程。PySide 的上下文版 singleShot 取消投递后
    仍可能持有 Python 回调；子定时器会随所有者释放连接，并在投递后删除自身，
    包括回调抛出异常或销毁所有者的情况。"""
    if QThread.currentThread() != context.thread():
        raise RuntimeError("Deferred work must be scheduled on its context thread")
    timer = _ContextTimer(context)
    timer.setSingleShot(True)
    # 保持 QTimer.singleShot 的精度策略，短延迟回调不得提前触发。
    timer.setTimerType(Qt.TimerType.PreciseTimer if delay_ms < 2000 else Qt.TimerType.CoarseTimer)
    timer.setInterval(delay_ms)
    timer.timeout.connect(callback)
    timer.timeout.connect(timer.deleteLater)
    timer.start()
    return timer
