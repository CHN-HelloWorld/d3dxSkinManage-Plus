import gc
import weakref

import pytest
import shiboken6
from PySide6.QtCore import QObject, Qt

from src.utils.qt_timers import call_later


class Receiver(QObject):
    def __init__(self, calls):
        super().__init__()
        self.calls = calls

    def run(self):
        self.calls.append(True)


@pytest.mark.parametrize("delay", [0, 30, 2500])
@pytest.mark.parametrize("callback_kind", ["method", "closure"])
def test_destroyed_context_releases_deferred_method_and_closure(qapp, delay, callback_kind):
    calls = []
    owner = Receiver(calls)
    reference = weakref.ref(owner)
    callback = owner.run if callback_kind == "method" else lambda owner=owner: owner.run()
    timer = call_later(delay, owner, callback)
    del callback
    shiboken6.delete(owner)
    del owner
    qapp.processEvents()
    gc.collect()
    assert reference() is None
    assert not shiboken6.isValid(timer)
    assert calls == []


@pytest.mark.parametrize("delay", [0, 20])
def test_deferred_call_runs_once_then_releases_its_timer(qtbot, delay):
    calls = []
    owner = Receiver(calls)
    timer = call_later(delay, owner, owner.run)
    assert calls == []
    qtbot.waitUntil(lambda: calls == [True])
    qtbot.waitUntil(lambda: not shiboken6.isValid(timer))
    assert owner.children() == []


def test_deferred_callback_can_destroy_its_own_context(qtbot):
    calls = []
    owner = QObject()

    def close():
        calls.append(True)
        shiboken6.delete(owner)

    timer = call_later(0, owner, close)
    qtbot.waitUntil(lambda: calls == [True])
    assert not shiboken6.isValid(timer)


def test_callback_exception_does_not_leave_a_timer_behind(qtbot):
    owner = QObject()

    def fail():
        raise ValueError("deferred failure")

    with qtbot.captureExceptions() as exceptions:
        timer = call_later(0, owner, fail)
        qtbot.waitUntil(lambda: bool(exceptions))
        qtbot.waitUntil(lambda: not shiboken6.isValid(timer))
    assert len(exceptions) == 1
    assert str(exceptions[0][1]) == "deferred failure"
    assert owner.children() == []


@pytest.mark.parametrize(
    "delay,kind",
    [
        (16, Qt.TimerType.PreciseTimer),
        (1999, Qt.TimerType.PreciseTimer),
        (2000, Qt.TimerType.CoarseTimer),
    ],
)
def test_frame_callbacks_retain_single_shot_clock_precision(qapp, delay, kind):
    owner = QObject()
    timer = call_later(delay, owner, lambda: None)
    assert timer.interval() == delay
    assert timer.timerType() == kind
    shiboken6.delete(owner)
