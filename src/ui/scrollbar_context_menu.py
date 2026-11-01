"""统一禁用滚动条的原生上下文菜单。"""

from __future__ import annotations

import shiboken6
from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QScrollBar

from src.ui.application_event_filters import install_application_event_filter


class ScrollbarContextMenuFilter(QObject):
    """拦截滚动条上下文菜单，保留其余控件的菜单和滚动交互。"""

    def eventFilter(self, obj, event):
        """消费滚动条菜单事件，其他事件继续传递。"""
        if event.type() == QEvent.Type.ContextMenu and isinstance(obj, QScrollBar):
            event.accept()
            return True
        return False


def install(app: QApplication) -> ScrollbarContextMenuFilter:
    """覆盖已有及后续创建的滚动条，过滤器随 QApplication 存活。"""
    menu_filter = getattr(app, "_scrollbar_context_menu_filter", None)
    if menu_filter is None or (not shiboken6.isValid(menu_filter)):
        menu_filter = ScrollbarContextMenuFilter(app)
        app._scrollbar_context_menu_filter = menu_filter
    install_application_event_filter(app, menu_filter, (QEvent.Type.ContextMenu,))
    return menu_filter
