"""验证对话框装饰在隐藏、循环垃圾回收和 Qt 子对象析构期间的资源清理。"""

import gc
import weakref

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QDialog

from src.ui.dialog_chrome import decorate_dialog


@pytest.mark.parametrize("control_kind", ["titlebar", "resize"])
def test_visible_dialog_collection_handles_cleared_control_state(qapp, qtbot, control_kind):
    """用合法的父子循环引用使 Python 状态先于窗口析构释放，再验证真实隐藏回调及定时器回收。"""
    timeouts = []

    def abandon_dialog():
        """不保留窗口强引用，让垃圾回收执行 Qt 析构，而非提前调用隐藏或删除。"""
        dialog = QDialog()
        frame = decorate_dialog(dialog)
        control = frame.titlebar if control_kind == "titlebar" else frame.handles[0]
        # 控制器到所属窗口的引用使子控件状态可能先被清理，Qt 随后仍须投递隐藏事件。
        control._test_owner = dialog
        dialog.show()
        qapp.processEvents()
        timers = control.findChildren(QTimer)
        assert timers
        for timer in timers:
            timer.timeout.connect(lambda: timeouts.append(True))
            timer.start(0)
        return tuple(weakref.ref(obj) for obj in (dialog, frame, control, *timers))

    with qtbot.captureExceptions() as exceptions:
        references = abandon_dialog()
        gc.collect()
        qapp.processEvents()

    assert exceptions == []
    assert all(reference() is None for reference in references)
    assert timeouts == []


def test_hidden_titlebar_discards_pending_move_and_can_be_reused(qapp, qtbot):
    """隐藏期间取消已排队的移动，重新显示后仍能合并处理新的拖动位置。"""
    dialog = QDialog()
    qtbot.addWidget(dialog)
    bar = decorate_dialog(dialog).titlebar
    dialog.setGeometry(100, 100, 320, 200)
    dialog.show()
    qapp.processEvents()
    origin = dialog.pos()

    def queue_move(delta: QPoint):
        """设置一次拖动基准并投递真实鼠标移动，使窗口移动经过所属定时器。"""
        start = bar.mapToGlobal(QPoint(20, 15))
        bar._drag_origin = start
        bar._window_origin = dialog.pos()
        bar._coalesced_move = True
        QApplication.sendEvent(
            bar,
            QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(20, 15),
                QPointF(start + delta),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )

    queue_move(QPoint(60, 40))
    assert bar._move_timer.isActive()
    dialog.hide()
    assert not bar._move_timer.isActive()
    qapp.processEvents()
    dialog.show()
    qapp.processEvents()
    assert dialog.pos() == origin

    queue_move(QPoint(30, 20))
    qtbot.waitUntil(lambda: dialog.pos() == origin + QPoint(30, 20))
