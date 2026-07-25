"""随所有者销毁而释放回调的单次 Qt 定时器。"""

from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, QThread, QTimer


def call_later(delay_ms: int, context: QObject, callback: Callable[[], object]) -> QTimer:
    """在上下文线程调度回调，并在上下文销毁时取消和释放。

    调用必须发生在同一 Qt 线程。PySide 的上下文版 singleShot 取消投递后
    仍可能持有 Python 回调；子定时器会随所有者释放连接，并在投递后删除自身，
    包括回调抛出异常或销毁所有者的情况。"""
    if QThread.currentThread() != context.thread():
        raise RuntimeError("Deferred work must be scheduled on its context thread")
    timer = QTimer(context)
    timer.setSingleShot(True)
    # 保持 QTimer.singleShot 的精度策略，短延迟回调不得提前触发。
    timer.setTimerType(Qt.TimerType.PreciseTimer if delay_ms < 2000 else Qt.TimerType.CoarseTimer)
    timer.setInterval(delay_ms)
    timer.timeout.connect(callback)
    timer.timeout.connect(timer.deleteLater)
    timer.start()
    return timer
