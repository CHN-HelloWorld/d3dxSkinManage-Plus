"""按事件类型分发显式连续安装的应用过滤器。

仅合并启动时声明的批次，独立过滤器安装前结束批次；其他安装保持 Qt 原有顺序。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from weakref import ReferenceType, ref

import shiboken6
from PySide6.QtCore import QObject


@dataclass(eq=False)
class _Registration:
    """保存事件处理器弱引用、订阅的事件类型及其销毁连接。"""

    handler: ReferenceType
    kinds: frozenset
    active: bool = True
    destroyed_slot: object = None


class _FilterGroup(QObject):
    """以一个应用级过滤器分发多个处理器的事件订阅，避免重复遍历无关事件。"""

    def __init__(self, app):
        """创建按事件类型索引的订阅表；首次注册时才安装应用级过滤器。"""
        super().__init__(app)
        self._app = app
        self._entries = []
        self._routes = {}
        self._installed = False
        self._closed = False

    def add(self, handler, kinds):
        """注册处理器并按后注册先调用的顺序分发；对象销毁时自动注销。"""
        entry = _Registration(ref(handler), frozenset(kinds))
        group_ref = ref(self)

        def destroyed(*_):
            """在 Python 包装对象或 Qt 对象销毁时移除对应订阅。"""
            group = group_ref()
            if group is not None:
                group._remove(entry)

        entry.handler = ref(handler, destroyed)
        entry.destroyed_slot = destroyed
        handler.destroyed.connect(destroyed)
        handler._application_filter_group = group_ref
        self._entries.append(entry)
        for kind in entry.kinds:
            self._routes[kind] = (entry, *self._routes.get(kind, ()))
        if not self._installed:
            self._app.installEventFilter(self)
            self._installed = True

    def remove(self, handler):
        """按处理器身份查找并移除其第一项有效订阅。"""
        for entry in self._entries:
            if entry.handler() is handler:
                self._remove(entry)
                break

    def _remove(self, entry):
        """使订阅立即失效，断开销毁连接，并从所有事件路由中移除。"""
        if not entry.active:
            return
        entry.active = False
        self._entries.remove(entry)
        handler = entry.handler()
        if handler is not None and shiboken6.isValid(handler):
            handler._application_filter_group = None
            handler.destroyed.disconnect(entry.destroyed_slot)
        for kind in entry.kinds:
            remaining = tuple(item for item in self._routes[kind] if item is not entry)
            if remaining:
                self._routes[kind] = remaining
            else:
                del self._routes[kind]
        self._retire_if_empty()

    def _retire_if_empty(self):
        """无订阅时卸载应用级过滤器；已关闭的组随后交由 Qt 删除。"""
        if self._entries or (not shiboken6.isValid(self)) or (not shiboken6.isValid(self._app)):
            return
        if self._installed:
            self._app.removeEventFilter(self)
            self._installed = False
        if self._closed:
            self.deleteLater()

    def eventFilter(self, watched, event):
        # 当前投递使用稳定列表，移除项立即失效，嵌套事件读取最新注册表。
        """按订阅顺序转发事件，遇到已消费事件或已销毁目标立即停止。"""
        for entry in self._routes.get(event.type(), ()):
            handler = entry.handler()
            if not entry.active or handler is None or (not shiboken6.isValid(handler)):
                continue
            consumed = handler.eventFilter(watched, event)
            if consumed or (not shiboken6.isValid(watched)):
                return True
        return False


@contextmanager
def group_application_event_filters(app):
    """合并一个明确连续的安装批次，不接管开放式过滤链。"""
    if getattr(app, "_application_filter_batch", None) is not None:
        raise RuntimeError("Application event-filter batches cannot be nested")
    group = _FilterGroup(app)
    app._application_filter_batch = group
    try:
        yield
    finally:
        app._application_filter_batch = None
        group._closed = True
        group._retire_if_empty()


def install_application_event_filter(app, handler, kinds):
    """立即安装，仅显式批次启用按类型分发。"""
    remove_application_event_filter(app, handler)
    group = getattr(app, "_application_filter_batch", None)
    if group is None:
        app.installEventFilter(handler)
    else:
        group.add(handler, kinds)


def remove_application_event_filter(app, handler):
    """注销分组中的处理器，同时移除可能直接安装的 Qt 过滤器。"""
    group_ref = getattr(handler, "_application_filter_group", None)
    group = group_ref() if group_ref is not None else None
    if group is not None and shiboken6.isValid(group):
        group.remove(handler)
    if shiboken6.isValid(handler) and shiboken6.isValid(app):
        app.removeEventFilter(handler)
