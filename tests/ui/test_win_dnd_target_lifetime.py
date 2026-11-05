"""验证管理员拖放桥持有的 COM 目标随原生窗口销毁而释放。"""

from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QDialog, QFrame, QWidget

from src.ui import win_dnd_admin


class _CountedComTarget:
    """提供真实 IUnknown 虚表与引用计数，仅隔离操作系统拥有的 OLE 注册表。"""

    def __init__(self):
        """保活 ctypes 函数指针与虚表，使生产代码执行真实 AddRef 和 Release 调用。"""
        self.references = 1
        method_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(ctypes.c_ulong, ctypes.c_void_p)
        self._add_ref = method_type(self._retain)
        self._release = method_type(self._release_reference)
        self._vtable = (ctypes.c_void_p * 3)(
            0, ctypes.cast(self._add_ref, ctypes.c_void_p).value, ctypes.cast(self._release, ctypes.c_void_p).value
        )
        self._instance = (ctypes.c_void_p * 1)(ctypes.addressof(self._vtable))
        self.pointer = ctypes.addressof(self._instance)

    def _retain(self, _pointer):
        """增加桥接器借用的引用，并返回当前引用数。"""
        self.references += 1
        return self.references

    def _release_reference(self, _pointer):
        """释放桥接器持有的引用，保留模拟注册表的初始所有权。"""
        self.references -= 1
        return self.references


@pytest.fixture
def com_target(qapp, monkeypatch):
    """替换原生指针查询，保留真实 Qt 句柄创建、改变和销毁的事件顺序。"""
    target = _CountedComTarget()

    def get_property(_hwnd, _name):
        """为测试窗口返回仍由模拟注册表持有的 COM 目标。"""
        return target.pointer

    monkeypatch.setattr(win_dnd_admin, "_u32", lambda: SimpleNamespace(GetPropW=get_property))
    monkeypatch.setattr(win_dnd_admin, "_saved_targets", {})
    yield target
    for saved in list(win_dnd_admin._saved_targets.values()):
        if hasattr(saved, "release"):
            saved.release()


@pytest.mark.parametrize("finish", ["delete", "close_popup", "change_flags"])
def test_native_window_retirement_releases_saved_com_target(qapp, qtbot, com_target, finish):
    """窗口对象、弹层句柄或窗口标志任一结束原生生命周期，都必须释放旧拖放目标。"""
    host = QWidget()
    qtbot.addWidget(host)
    host.show()
    window = QFrame(host, Qt.WindowType.Popup) if finish == "close_popup" else QDialog(host)
    window.show()
    qapp.processEvents()
    hwnd = int(window.winId())
    assert win_dnd_admin._grab_qt_drop_target(hwnd) == com_target.pointer
    assert com_target.references == 2
    assert win_dnd_admin._grab_qt_drop_target(hwnd) == com_target.pointer
    assert com_target.references == 2

    if finish == "delete":
        window.deleteLater()
    elif finish == "close_popup":
        window.close()
    else:
        window.setWindowFlag(Qt.WindowType.FramelessWindowHint)
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    assert com_target.references == 1
    assert hwnd not in win_dnd_admin._saved_targets


def test_handle_lookup_does_not_create_a_native_window(qapp, qtbot, monkeypatch):
    """原生消息查询只使用现存 HWND 映射，不调用可能重建窗口的 winId。"""
    window = QWidget()
    qtbot.addWidget(window)
    window.show()
    hwnd = int(window.winId())

    def forbidden_win_id():
        """把创建型句柄查询变为可观察失败，真实 Qt 句柄查找仍保持可用。"""
        raise RuntimeError("Native message lookup must not create a window")

    with monkeypatch.context() as patch:
        patch.setattr(window, "winId", forbidden_win_id)
        assert win_dnd_admin._widget_for_hwnd(hwnd) is window


def test_internal_drag_does_not_rearm_a_retired_native_handle(qapp, qtbot, monkeypatch):
    """内部拖动的嵌套事件循环销毁源窗口时，退出上下文不能重新注册旧 HWND。"""
    host = QWidget()
    qtbot.addWidget(host)
    popup = QFrame(host, Qt.WindowType.Popup)
    popup.show()
    qapp.processEvents()
    rearmed = []
    monkeypatch.setattr(win_dnd_admin, "_installed", True)
    monkeypatch.setattr(win_dnd_admin, "_restore_ole_target", lambda _hwnd: True)
    monkeypatch.setattr(win_dnd_admin, "_arm", rearmed.append)
    with win_dnd_admin.suspend_for_internal_drag(popup):
        popup.close()
        qapp.processEvents()
    assert rearmed == []
