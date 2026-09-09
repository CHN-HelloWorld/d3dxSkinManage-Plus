import pytest
import shiboken6
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtWidgets import QMenu, QVBoxLayout, QWidget

from src.ui import acrylic


def test_menu_polish_does_not_create_native_ancestors(qtbot, qapp, monkeypatch):
    from src.ui import win_effects

    native_calls = []
    monkeypatch.setattr(win_effects, "disable_open_close_transition", lambda widget: native_calls.append(widget))
    host = QWidget()
    qtbot.addWidget(host)
    child = QWidget(host)
    QVBoxLayout(host).addWidget(child)
    host.show()
    popup_filter = acrylic.AcrylicPopupFilter()
    qapp.installEventFilter(popup_filter)
    try:
        menu = QMenu(child)
        menu.addAction("Action")
        assert not child.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        menu.ensurePolished()
        assert not native_calls
        assert menu.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert not child.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        target = host.mapToGlobal(QPoint(40, 50))
        menu.popup(target)
        qtbot.waitUntil(menu.isVisible)
        assert native_calls and all(widget is menu for widget in native_calls)
        assert menu.pos() == target
        assert not child.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        menu.close()
    finally:
        qapp.removeEventFilter(popup_filter)


@pytest.mark.parametrize("native_before_polish", [True, False])
def test_context_menu_uses_top_level_native_owner_and_follows_control_lifetime(
    qtbot,
    qapp,
    qtlog,
    native_before_polish,
):
    host = QWidget()
    qtbot.addWidget(host)
    host.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    host.resize(640, 420)
    child = QWidget(host)
    QVBoxLayout(host).addWidget(child)
    host.show()
    popup_filter = acrylic.AcrylicPopupFilter()
    qapp.installEventFilter(popup_filter)
    try:
        menu = QMenu(child)
        menu.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        action = menu.addAction("Action")
        clicked = []
        action.triggered.connect(lambda: clicked.append(True))
        if not native_before_polish:
            menu.ensurePolished()
        child.winId()
        assert not child.windowHandle().isTopLevel()
        target = host.mapToGlobal(QPoint(40, 50))

        menu.popup(target)
        qtbot.waitUntil(menu.isVisible)

        assert menu.pos() == target
        assert menu.windowHandle().transientParent() is host.windowHandle()
        assert menu.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        qtbot.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center())
        assert clicked == [True]
        menu.popup(target)
        child.deleteLater()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qtbot.waitUntil(lambda: not shiboken6.isValid(menu))
        assert not [record.message for record in qtlog.records if "must be a top level window" in record.message]
    finally:
        qapp.removeEventFilter(popup_filter)
