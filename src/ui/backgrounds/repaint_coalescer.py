"""通过一个定时器合并已捕获前景窗口的绘制同步。"""

from __future__ import annotations

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QTimer


class WindowRepaintCoalescer(QObject):
    """保留 Qt 脏区域，仅重新提交当前顶层控件的同步请求。"""

    def __init__(self, window, root, effect):
        """停止窗口旧合并器，接管更新请求并绑定捕获对象的销毁清理。"""
        super().__init__(window)
        previous = getattr(window, "_native_video_repaint_coalescer", None)
        if previous is not None and shiboken6.isValid(previous):
            previous.stop()
        self._window, self._root, self._effect = window, root, effect
        self._active = True
        self._pending = False
        self._flushing = False
        self._replay = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._flush)
        window._native_video_repaint_coalescer = self
        window.destroyed.connect(self._window_destroyed)
        root.destroyed.connect(self.stop)
        effect.destroyed.connect(self.stop)
        window.installEventFilter(self)

    def _owns_capture(self):
        """确认窗口、根控件及效果仍有效且捕获关系仍由本对象持有。"""
        return (
            self._active
            and shiboken6.isValid(self._window)
            and shiboken6.isValid(self._root)
            and shiboken6.isValid(self._effect)
            and self._root.graphicsEffect() is self._effect
            and self._root.window() is self._window
        )

    def eventFilter(self, watched, event):
        """截留目标窗口的更新请求并合并计时，放行本对象重放的同步事件。"""
        if watched is not self._window or event.type() != QEvent.Type.UpdateRequest:
            return False
        if not self._owns_capture():
            self.stop()
            return False
        if event is self._replay:
            return False
        self._pending = True
        if not self._flushing and not self._timer.isActive():
            self._timer.start()
        return True

    def _flush(self):
        """同步重放一次更新请求，避免重入；绘制期间新增请求留到下一次计时。"""
        if not self._pending or self._flushing:
            return
        if not self._owns_capture():
            self.stop()
            return
        self._pending = False
        self._flushing = True
        self._replay = QEvent(QEvent.Type.UpdateRequest)
        try:
            QCoreApplication.sendEvent(self._window, self._replay)
        except RuntimeError:
            self._pending = True
            self.stop()
        finally:
            self._replay = None
            self._flushing = False
            # 绘制中可能进入嵌套事件循环，当前绘制栈返回后再启动下一定时器。
            if self._active and self._pending:
                self._timer.start()

    def stop(self, *_):
        """停止合并并移除事件过滤器，将未处理更新交还 Qt 后延迟删除自身。"""
        if not self._active:
            return
        self._active = False
        self._timer.stop()
        window = self._window
        pending, self._pending = self._pending, False
        if window is not None and shiboken6.isValid(window):
            window.removeEventFilter(self)
            if getattr(window, "_native_video_repaint_coalescer", None) is self:
                window._native_video_repaint_coalescer = None
            if pending:
                # 已有脏区域时 update 可能不投递新事件，移除过滤器后转交实际同步请求。
                QCoreApplication.postEvent(
                    window, QEvent(QEvent.Type.UpdateRequest), Qt.EventPriority.LowEventPriority.value
                )
        self.deleteLater()

    def _window_destroyed(self, *_):
        """窗口销毁时停止计时并清除捕获引用，避免继续访问已删除的 Qt 对象。"""
        self._active = False
        self._pending = False
        self._timer.stop()
        self._window = self._root = self._effect = None
