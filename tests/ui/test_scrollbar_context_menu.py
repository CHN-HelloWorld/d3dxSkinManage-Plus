"""验证滚动条菜单策略的全局覆盖范围和交互边界。"""

from contextlib import contextmanager

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QContextMenuEvent, QWheelEvent
from PySide6.QtWidgets import QScrollArea, QScrollBar, QWidget

from src.ui import scrollbar_context_menu
from src.ui.application_event_filters import group_application_event_filters, remove_application_event_filter


@contextmanager
def _installed_policy(qapp):
    """模拟主程序的分组安装，并在用例结束后清理应用过滤器。"""
    with group_application_event_filters(qapp):
        handler = scrollbar_context_menu.install(qapp)
    try:
        yield
    finally:
        remove_application_event_filter(qapp, handler)
        handler.deleteLater()


def _context_menu(qapp, widget, reason):
    """发送真实菜单事件；用自定义菜单信号记录控件是否收到请求。"""
    received = []
    widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    widget.customContextMenuRequested.connect(received.append)
    pos = widget.rect().center()
    event = QContextMenuEvent(reason, pos, widget.mapToGlobal(pos))
    qapp.sendEvent(widget, event)
    widget.customContextMenuRequested.disconnect(received.append)
    return received


def test_existing_and_later_scrollbars_do_not_receive_context_menus(qapp, qtbot):
    existing = QScrollArea()
    qtbot.addWidget(existing)
    with _installed_policy(qapp):
        later = QScrollArea()
        qtbot.addWidget(later)
        for area in (existing, later):
            for scrollbar in (area.horizontalScrollBar(), area.verticalScrollBar()):
                for reason in (QContextMenuEvent.Reason.Mouse, QContextMenuEvent.Reason.Keyboard):
                    assert _context_menu(qapp, scrollbar, reason) == []


def test_other_context_menus_and_scrollbar_wheel_still_work(qapp, qtbot):
    widget = QWidget()
    scrollbar = QScrollBar(Qt.Orientation.Vertical)
    qtbot.addWidget(widget)
    qtbot.addWidget(scrollbar)
    scrollbar.setRange(0, 100)
    scrollbar.setValue(50)
    with _installed_policy(qapp):
        assert len(_context_menu(qapp, widget, QContextMenuEvent.Reason.Mouse)) == 1
        pos = scrollbar.rect().center()
        event = QWheelEvent(
            QPointF(pos),
            QPointF(scrollbar.mapToGlobal(pos)),
            QPoint(),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        qapp.sendEvent(scrollbar, event)
        assert scrollbar.value() > 50
