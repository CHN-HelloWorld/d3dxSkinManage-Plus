"""串行应用外观变化，并合并重复等待的选择。"""

from __future__ import annotations

from collections.abc import Callable

import shiboken6
from PySide6.QtCore import QObject, QTimer

from src.utils.i18n import t
from src.utils.logger import logger
from src.utils.qt_timers import call_later


class AppearanceQueue(QObject):
    """每个操作通过回调报告完成，界面工作不重叠执行。"""

    def __init__(self, parent, toast) -> None:
        """保存提示控件和待执行操作，并创建单次定时器串行推进外观更新。"""
        super().__init__(parent)
        self._toast = toast
        self._pending: dict[str, tuple[str, str, Callable]] = {}
        self._active = False
        self._disposed = False
        self._current = None
        self._next = QTimer(self)
        self._next.setSingleShot(True)
        self._next.timeout.connect(self._show_next)

    def submit(self, key: str, busy_text: str, done_text: str, operation: Callable) -> None:
        """以操作键合并尚未开始的外观请求；队列空闲时立即显示忙碌提示。"""
        if self._disposed:
            return
        self._pending[key] = (busy_text, done_text, operation)
        if not self._active:
            self._active = True
            self._show_next()

    def _show_next(self) -> None:
        """在提示显示后执行下一项；提示不可用时通过事件循环继续操作。"""
        if (not shiboken6.isValid(self)) or self._disposed or not self._pending:
            return
        busy_text = next(iter(self._pending.values()))[0]
        try:
            self._toast.busy(busy_text, on_shown=self._run_next)
        except Exception:
            call_later(0, self, self._run_next)

    def _run_next(self) -> None:
        """取出最早的请求并传入完成回调；捕获执行异常并将其转为失败提示。"""
        if (not shiboken6.isValid(self)) or self._disposed or self._current is not None or not self._pending:
            return
        key = next(iter(self._pending))
        _, done_text, operation = self._pending.pop(key)
        token = object()
        self._current = token
        try:
            operation(lambda: self._complete(token, done_text))
        except Exception:
            logger.exception("应用外观设置失败: %s", key)
            self._complete(token, t("外观设置应用失败，请重试"), ok=False)

    def _complete(self, token, text: str, *, ok: bool = True) -> None:
        """仅接受当前操作令牌的回调，继续等待队列或显示最终结果。"""
        if (not shiboken6.isValid(self)) or self._disposed or self._current is not token:
            return
        self._current = None
        if self._pending:
            self._next.start(0)
        else:
            self._active = False
            self._toast.done(text, ok=ok)

    def dispose(self) -> None:
        """停止后续调度并丢弃待执行操作，使已发出的完成回调失效。"""
        self._disposed = True
        self._next.stop()
        self._pending.clear()
        self._current = None
