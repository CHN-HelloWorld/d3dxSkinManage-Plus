from __future__ import annotations

import sys
from weakref import ref

import shiboken6
from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMenu, QWidget


def _dismiss(widget):
    if widget is None or not shiboken6.isValid(widget) or not widget.isVisible():
        return
    if isinstance(widget, QDialog):
        # 覆盖 reject 或 closeEvent 的对话框也必须能够清理。
        QDialog.done(widget, QDialog.DialogCode.Rejected)
    else:
        QWidget.hide(widget)


class interact(QObject):
    """在下一个匹配的对话框或菜单事件循环内执行回调。

    菜单回调可返回 QAction，以真实回车选择；其他回调自行点击、接受或拒绝控件。
    最终清理未关闭的弹层，并在 Qt 外重新抛出回调异常，供 pytest 展示调用栈。
    """

    def __init__(self, widget_type, callback, *, timeout=2000):
        super().__init__()
        self._app = QApplication.instance()
        if self._app is None:
            raise RuntimeError("interact requires a QApplication")
        self._widget_type = widget_type
        self._callback = callback
        self._timeout = timeout
        self._target = None
        self._handled = False
        self._error = None
        self._existing = set(self._app.topLevelWidgets())
        self._invoke = QTimer(self)
        self._invoke.setSingleShot(True)
        self._invoke.timeout.connect(self._respond)
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._timed_out)

    def __enter__(self):
        self._app.installEventFilter(self)
        self._watchdog.start(self._timeout)
        return self

    def eventFilter(self, widget, event):
        if self._target is None and event.type() == QEvent.Type.Show and isinstance(widget, self._widget_type):
            self._target = ref(widget)
            self._invoke.start(0)
        return False

    def _respond(self):
        widget = self._target() if self._target is not None else None
        if widget is None or not shiboken6.isValid(widget):
            return
        self._handled = True
        try:
            action = self._callback(widget)
            if isinstance(widget, QMenu) and isinstance(action, QAction):
                assert action in widget.actions() and action.isEnabled(), "Menu choice is unavailable"
                widget.setActiveAction(action)
                QTest.keyClick(widget, Qt.Key.Key_Return)
        except BaseException:
            self._error = sys.exc_info()
        finally:
            _dismiss(widget)

    def _timed_out(self):
        if self._error is None:
            error = AssertionError(f"{self._widget_type.__name__} interaction did not finish within {self._timeout} ms")
            self._error = (AssertionError, error, None)
        # 意外模态窗口不得将测试阻塞在嵌套事件循环。
        targets = [self._app.activeModalWidget(), self._app.activePopupWidget()]
        targets.extend(
            widget
            for widget in self._app.topLevelWidgets()
            if widget not in self._existing and isinstance(widget, (QDialog, QMenu))
        )
        for widget in targets:
            _dismiss(widget)

    def __exit__(self, exception_type, exception, traceback):
        self._invoke.stop()
        self._watchdog.stop()
        self._app.removeEventFilter(self)
        _dismiss(self._target() if self._target is not None else None)
        self.deleteLater()
        if exception_type is not None:
            return False
        if self._error is not None:
            _, error, original_traceback = self._error
            raise error.with_traceback(original_traceback)
        assert self._handled, f"Expected a {self._widget_type.__name__} to open"
        return False
