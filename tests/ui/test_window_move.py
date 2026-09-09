"""验证 Win10 标题栏移动合并过期指针事件，并正确提交或取消尾部更新。"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QCursor, QMouseEvent
from PySide6.QtWidgets import QApplication, QDialog

from src.ui import native_resize, win_effects
from src.ui.dialog_chrome import decorate_dialog


class NativePointerEvent(QMouseEvent):
    """在离屏测试中保留真实系统指针事件的来源标志。"""

    def spontaneous(self) -> bool:
        """标识该事件应按系统输入处理。"""
        return True


def test_titlebar_coalesces_native_moves_and_commits_release(qapp, qtbot, monkeypatch):
    """队列中的旧坐标不能移动窗口，实际绘制使用最新光标，释放后不再追随光标。"""
    monkeypatch.setattr(QApplication, "platformName", lambda: "windows")
    monkeypatch.setattr(win_effects, "needs_popup_paint_before_blur", lambda: True)
    monkeypatch.setattr(native_resize, "begin_native_resize", lambda window: None)
    dialog = QDialog()
    qtbot.addWidget(dialog)
    frame = decorate_dialog(dialog)
    dialog.setGeometry(100, 120, 400, 280)
    dialog.show()
    qapp.processEvents()
    caption = frame.titlebar
    origin = dialog.pos()
    local = QPointF(80, 15)
    start = caption.mapToGlobal(local.toPoint())

    def send(kind, offset, button, buttons):
        """向真实标题栏投递带全局坐标的鼠标消息。"""
        event = NativePointerEvent(
            kind, local, QPointF(start + offset), button, buttons, Qt.KeyboardModifier.NoModifier
        )
        QApplication.sendEvent(caption, event)

    with monkeypatch.context() as moving:
        moving.setattr(dialog.windowHandle(), "startSystemMove", lambda: False)
        moving.setattr(QCursor, "pos", lambda: start + QPoint(55, 24))
        send(QEvent.Type.MouseButtonPress, QPoint(), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
        for offset in (QPoint(10, 4), QPoint(30, 12), QPoint(45, 18)):
            send(QEvent.Type.MouseMove, offset, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
        assert dialog.pos() == origin
        qapp.processEvents()
        assert dialog.pos() == origin + QPoint(55, 24)
        send(QEvent.Type.MouseMove, QPoint(63, 30), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
        send(QEvent.Type.MouseButtonRelease, QPoint(70, 35), Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
        qapp.processEvents()
        assert dialog.pos() == origin + QPoint(70, 35)
    dialog.close()
