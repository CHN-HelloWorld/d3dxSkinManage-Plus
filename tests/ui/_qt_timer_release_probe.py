"""在独立进程验证单次定时器释放上下文最后引用时的原生析构顺序。"""

from __future__ import annotations

import gc
import json
import os
import sys
import weakref
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer
from PySide6.QtWidgets import QApplication, QWidget

from src.utils.qt_timers import call_later


def run(owner_kind: str, outcome: str) -> None:
    """移除上下文的外部引用，完成真实定时事件或取消后检查原生对象均只销毁一次。"""
    app = QApplication([])
    owner = QWidget() if owner_kind == "widget" else QObject()
    reference = weakref.ref(owner)
    calls, errors, destroyed = [], [], []
    owner.destroyed.connect(lambda *_args: destroyed.append(True))

    def callback(context=owner):
        """故意持有上下文的最后引用，覆盖返回、抛错及同步销毁上下文。"""
        calls.append(True)
        if outcome == "raise":
            raise ValueError("deferred failure")
        if outcome == "destroy_context":
            shiboken6.delete(context)

    def capture_error(_kind, error, _traceback):
        """仅保留错误文本，清除解释器交互回溯对上下文的额外引用。"""
        errors.append(str(error))
        for name in ("last_exc", "last_type", "last_value", "last_traceback"):
            if hasattr(sys, name):
                delattr(sys, name)

    previous_hook = sys.excepthook
    sys.excepthook = capture_error
    try:
        delay = 60_000 if outcome.startswith("cancel_") else 0
        timers = [call_later(delay, owner, callback) for _ in range(3)]
        assert all(isinstance(timer, QTimer) and timer.parent() is owner for timer in timers)
        if outcome == "cancel_timer":
            for timer in timers:
                timer.deleteLater()
        elif outcome == "cancel_context":
            owner.deleteLater()
        del callback, owner

        app.processEvents()
        # 定时事件安排删除；显式处理 DeferredDelete，以覆盖子对象释放最后父引用的时刻。
        for _ in range(3):
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
        gc.collect()
        assert all(not shiboken6.isValid(timer) for timer in timers)
        assert reference() is None
        assert destroyed == [True]
        expected_calls = 0 if outcome.startswith("cancel_") else (1 if outcome == "destroy_context" else 3)
        assert calls == [True] * expected_calls
        assert errors == (["deferred failure"] * 3 if outcome == "raise" else [])
        print(json.dumps({"owner": owner_kind, "outcome": outcome, "calls": len(calls), "destroyed": len(destroyed)}))
    finally:
        sys.excepthook = previous_hook


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
