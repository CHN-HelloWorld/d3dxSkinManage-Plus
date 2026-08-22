from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QDialog, QFrame, QWidget

from src.ui import win_dnd_admin


@pytest.fixture
def armed_windows(qapp, monkeypatch):
    # 保留真实 Qt 原生窗口生命周期，仅隔离 Win32 拖放注册。
    handles = []
    monkeypatch.setattr(win_dnd_admin, "_arm", handles.append)
    event_filter = win_dnd_admin._ArmOnShow(qapp)
    qapp.installEventFilter(event_filter)
    yield handles
    qapp.removeEventFilter(event_filter)


def test_closed_popup_does_not_recreate_its_native_window(qapp, qtbot, armed_windows):
    host = QWidget()
    qtbot.addWidget(host)
    host.show()
    popup = QFrame(host, Qt.WindowType.Popup)

    for _ in range(3):
        armed_windows.clear()
        popup.show()
        qapp.processEvents()
        assert popup.isVisible()
        assert popup.testAttribute(Qt.WidgetAttribute.WA_WState_Created)
        assert int(popup.windowHandle().winId()) in armed_windows

        popup.close()
        qapp.processEvents()
        assert not popup.isVisible()
        assert not popup.testAttribute(Qt.WidgetAttribute.WA_WState_Created)


def test_visible_window_is_rearmed_on_activation(qapp, qtbot, armed_windows):
    window = QWidget()
    qtbot.addWidget(window)
    window.show()
    qapp.processEvents()
    hwnd = int(window.windowHandle().winId())
    assert hwnd in armed_windows

    armed_windows.clear()
    qapp.sendEvent(window, QEvent(QEvent.Type.WindowActivate))
    assert armed_windows == [hwnd]


@pytest.mark.parametrize("with_owner", [False, True])
def test_changing_window_flags_does_not_create_or_arm_a_missing_handle(
    qapp,
    qtbot,
    monkeypatch,
    with_owner,
):
    owner = QWidget() if with_owner else None
    if owner is not None:
        qtbot.addWidget(owner)
        owner.show()
    dialog = QDialog(owner)
    if owner is None:
        qtbot.addWidget(dialog)
    handles = []
    monkeypatch.setattr(win_dnd_admin, "_arm", handles.append)
    gaps, recreated, armed_during_gap = [], [], []

    class LifecycleObserver(win_dnd_admin._ArmOnShow):
        def eventFilter(self, obj, event):
            # 窗口标志变化会先清除自身句柄，而创建状态仍为真；所属对话框的有效编号可能退回所有者。
            wid = dialog.effectiveWinId() if obj is dialog else None
            missing = (
                obj is dialog
                and event.type() == QEvent.Type.WinIdChange
                and dialog.testAttribute(Qt.WidgetAttribute.WA_WState_Created)
                and (not wid or QWidget.find(wid) is not dialog)
            )
            before = len(handles)
            if missing:
                gaps.append(int(wid or 0))
            result = super().eventFilter(obj, event)
            if missing:
                armed_during_gap.extend(handles[before:])
                current = dialog.effectiveWinId()
                if current and QWidget.find(current) is dialog:
                    recreated.append(int(current))
            return result

    hook = LifecycleObserver(qapp)
    qapp.installEventFilter(hook)
    try:
        # 透明对话框会在首次 Polish 或 Show 前创建原生窗口句柄。
        dialog.winId()
        handles.clear()
        dialog.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        assert gaps, "The real Qt flag change must exercise the missing-HWND interval"
        assert recreated == [], "The drag/drop hook recreated a HWND inside WinIdChange"
        assert armed_during_gap == [], "No HWND, including the owner's, may be armed in this interval"

        dialog.show()
        qapp.processEvents()
        assert int(dialog.windowHandle().winId()) in handles
    finally:
        qapp.removeEventFilter(hook)
