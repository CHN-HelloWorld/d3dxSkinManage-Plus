"""验证真实对话框动画在重复显示、提前关闭和用户移动后保持正确几何。"""

from __future__ import annotations

import pytest
import shiboken6
from PySide6.QtCore import QAbstractAnimation, QEvent, QPoint, Qt
from PySide6.QtWidgets import QDialog

from src.ui import acrylic, anim, dialog_chrome


@pytest.fixture
def animated_dialogs(qapp):
    """启用真实动画和公共装饰入口，测试结束前停止并销毁所属窗口及回调。"""
    previous = anim.is_enabled()
    native = qapp.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
    anim.set_enabled(True)
    manager = dialog_chrome.install(qapp)
    dialogs = []

    def create(*, prepared=True):
        """分别覆盖构造时准备和首次显示时自动装饰的公共入口。"""
        dialog = QDialog()
        dialogs.append(dialog)
        if prepared:
            acrylic.apply_dialog_acrylic(dialog)
        dialog.ensurePolished()
        dialog.setGeometry(120, 140, 320, 240)
        return dialog

    yield create

    for dialog in dialogs:
        if shiboken6.isValid(dialog):
            for animation in dialog.findChildren(QAbstractAnimation):
                animation.stop()
            dialog.hide()
            dialog.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    qapp.removeEventFilter(manager)
    manager.deleteLater()
    qapp.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, native)
    anim.set_enabled(previous)


def _wait_for_animation(dialog, qtbot):
    """等待当前真实动画停止并处理尾部窗口事件，避免仅观察完成信号中的临时几何。"""

    def stopped():
        """已销毁或处于停止状态的动画均表示本次位移结束。"""
        animation = getattr(dialog, "_slide_anim", None)
        return (
            animation is None
            or not shiboken6.isValid(animation)
            or animation.state() == QAbstractAnimation.State.Stopped
        )

    qtbot.waitUntil(stopped)
    qtbot.wait(20)


@pytest.mark.parametrize("prepared", [True, False])
@pytest.mark.parametrize("finish", ["accept", "reject", "close", "done"])
def test_reopening_animated_dialog_preserves_resting_geometry(animated_dialogs, qtbot, prepared, finish):
    """确认、取消、标题栏关闭与显式完成都不得让退出位移累积到下一次显示。"""
    dialog = animated_dialogs(prepared=prepared)
    expected = dialog.geometry()
    results = []
    dialog.finished.connect(results.append)
    for _ in range(3):
        dialog.show()
        _wait_for_animation(dialog, qtbot)
        assert dialog.geometry() == expected
        if finish == "done":
            dialog.done(7)
        else:
            getattr(dialog, finish)()
        qtbot.waitUntil(lambda: not dialog.isVisible())
        _wait_for_animation(dialog, qtbot)
    assert results == [1 if finish == "accept" else 7 if finish == "done" else 0] * 3


@pytest.mark.parametrize("finish", ["hide", "reject", "repeat_close"])
def test_interrupted_dialog_animation_cannot_move_a_reopened_window(animated_dialogs, qtbot, finish):
    """入场尚未完成就隐藏或重复关闭时，旧动画不得继续修改新一轮窗口位置。"""
    dialog = animated_dialogs()
    expected = dialog.geometry()
    dialog.show()
    if finish == "hide":
        dialog.hide()
    else:
        dialog.reject()
        if finish == "repeat_close":
            dialog.reject()
    qtbot.waitUntil(lambda: not dialog.isVisible())
    dialog.show()
    _wait_for_animation(dialog, qtbot)
    assert dialog.isVisible()
    assert dialog.geometry() == expected


def test_dialog_close_preserves_position_chosen_by_user(animated_dialogs, qtbot):
    """关闭仅撤销退出动画，不能把用户已移动的窗口重置到本轮打开位置。"""
    dialog = animated_dialogs()
    dialog.show()
    _wait_for_animation(dialog, qtbot)
    dialog.move(dialog.pos() + QPoint(30, 20))
    expected = dialog.geometry()
    dialog.reject()
    qtbot.waitUntil(lambda: not dialog.isVisible())
    _wait_for_animation(dialog, qtbot)
    dialog.show()
    _wait_for_animation(dialog, qtbot)
    assert dialog.geometry() == expected
