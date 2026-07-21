from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QComboBox, QScrollArea, QWidget

from src.ui import combo_wheel
from src.ui.widgets.icon_combo import LeadingIconComboBox


@pytest.fixture(autouse=True)
def wheel_policy(qapp):
    wheel_filter = combo_wheel.install(qapp)
    yield
    qapp.removeEventFilter(wheel_filter)


def _wheel(qapp, widget, delta=-120):
    pos = widget.rect().center()
    event = QWheelEvent(
        QPointF(pos),
        QPointF(widget.mapToGlobal(pos)),
        QPoint(),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    qapp.sendEvent(widget, event)
    return event


@pytest.mark.parametrize("kind", ["plain", "editable", "icon"])
@pytest.mark.parametrize("focused", [False, True])
def test_wheel_does_not_change_dropdown_value(qapp, qtbot, kind, focused):
    window = QWidget()
    qtbot.addWidget(window)
    combo = LeadingIconComboBox("actions/sort", parent=window) if kind == "icon" else QComboBox(window)
    combo.setEditable(kind == "editable")
    combo.addItems(["First", "Second", "Third"])
    combo.setCurrentIndex(1)
    window.show()
    window.activateWindow()
    qapp.processEvents()
    if focused:
        combo.setFocus()
    else:
        combo.clearFocus()
    assert combo.hasFocus() is focused
    changed = []
    combo.currentIndexChanged.connect(changed.append)

    _wheel(qapp, combo)
    assert combo.currentIndex() == 1
    _wheel(qapp, combo, 120)
    assert combo.currentIndex() == 1
    assert changed == []


def test_wheel_over_dropdown_is_available_to_surrounding_page(qapp, qtbot):
    area = QScrollArea()
    qtbot.addWidget(area)
    content = QWidget()
    content.setFixedSize(200, 2000)
    combo = QComboBox(content)
    combo.addItems(["First", "Second", "Third"])
    area.setWidget(content)
    area.resize(240, 200)
    area.show()

    event = _wheel(qapp, combo)

    assert combo.currentIndex() == 0
    assert not event.isAccepted()
    # Qt 只传播真实滚轮事件；将被忽略的合成事件继续交给滚动视口，模拟原生行为。
    qapp.sendEvent(area.viewport(), event)
    assert area.verticalScrollBar().value() > 0


def test_popup_wheel_scrolls_but_click_commits_value(qapp, qtbot):
    combo = QComboBox()
    qtbot.addWidget(combo)
    combo.addItems([f"Item {index}" for index in range(100)])
    combo.setMaxVisibleItems(5)
    with qtbot.waitExposed(combo):
        combo.show()
    # 直接展开列表，避免按钮开窗的双击释放保护影响选项点击；等待快照动画后的真实窗口。
    view = combo.view()
    popup = view.window()
    with qtbot.waitExposed(popup):
        combo.showPopup()
        qtbot.waitUntil(popup.isVisible)
    view.verticalScrollBar().setValue(0)

    _wheel(qapp, view.viewport())
    qapp.processEvents()

    assert combo.currentIndex() == 0
    assert view.verticalScrollBar().value() > 0
    index = view.indexAt(view.viewport().rect().center())
    assert index.isValid() and index.row() > 0
    pos = view.visualRect(index).center()
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    assert combo.currentIndex() == index.row()
    assert not view.isVisible()
