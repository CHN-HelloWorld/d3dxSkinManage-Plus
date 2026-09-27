from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QCursor, QMouseEvent, QWindow
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QVBoxLayout

from src.ui.dialogs.mod_updates_dialog import ModUpdatesDialog
from tests.ui.test_mod_updates_dialog import _info, keep_dialog

# 记录窗口内起点、指针位移及最终位置和尺寸变化。
EDGES = [
    (Qt.Edge.LeftEdge, (2, 300), (-40, 0), (-40, 0, 40, 0), Qt.CursorShape.SizeHorCursor),
    (Qt.Edge.RightEdge, (845, 300), (40, 0), (0, 0, 40, 0), Qt.CursorShape.SizeHorCursor),
    (Qt.Edge.TopEdge, (400, 2), (0, -30), (0, -30, 0, 30), Qt.CursorShape.SizeVerCursor),
    (Qt.Edge.BottomEdge, (400, 623), (0, 30), (0, 0, 0, 30), Qt.CursorShape.SizeVerCursor),
    (Qt.Edge.LeftEdge | Qt.Edge.TopEdge, (5, 5), (-40, -30), (-40, -30, 40, 30), Qt.CursorShape.SizeFDiagCursor),
    (Qt.Edge.RightEdge | Qt.Edge.TopEdge, (842, 5), (40, -30), (0, -30, 40, 30), Qt.CursorShape.SizeBDiagCursor),
    (Qt.Edge.LeftEdge | Qt.Edge.BottomEdge, (5, 620), (-40, 30), (-40, 0, 40, 30), Qt.CursorShape.SizeBDiagCursor),
    (Qt.Edge.RightEdge | Qt.Edge.BottomEdge, (842, 620), (40, 30), (0, 0, 40, 30), Qt.CursorShape.SizeFDiagCursor),
]


def show_dialog(qtbot):
    dialog = ModUpdatesDialog([_info(str(i)) for i in range(8)], enable_actions=True)
    keep_dialog(qtbot, dialog)
    dialog._sized_for_screen = True
    dialog.show()
    qtbot.wait(10)
    assert dialog.size().width() == 848 and dialog.size().height() == 626
    return dialog


def pointer(widget, kind, global_pos, button, buttons):
    local = widget.mapFromGlobal(global_pos)
    event = QMouseEvent(kind, QPointF(local), QPointF(global_pos), button, buttons, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, event)


@pytest.mark.parametrize("edge,position,delta,changes,cursor", EDGES)
def test_every_border_requests_the_correct_native_resize(qtbot, monkeypatch, edge, position, delta, changes, cursor):
    monkeypatch.setattr(QApplication, "platformName", staticmethod(lambda: "wayland"))
    requested = []
    monkeypatch.setattr(QWindow, "startSystemResize", lambda window, edges: requested.append(edges) or True)
    dialog = show_dialog(qtbot)
    point = QPoint(*position)
    handle = dialog.childAt(point)
    assert handle is not None and handle.cursor().shape() == cursor
    global_pos = dialog.mapToGlobal(point)
    pointer(handle, QEvent.Type.MouseButtonPress, global_pos, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    pointer(handle, QEvent.Type.MouseButtonRelease, global_pos, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    assert requested == [edge]


@pytest.mark.parametrize("kind", ["shared", "updates"])
@pytest.mark.parametrize("edge,position,delta,changes,cursor", EDGES)
def test_windows_drag_follows_pointer_without_entering_native_resize(
    qtbot,
    monkeypatch,
    kind,
    edge,
    position,
    delta,
    changes,
    cursor,
):
    from src.ui.acrylic import apply_dialog_acrylic

    monkeypatch.setattr(QApplication, "platformName", staticmethod(lambda: "windows"))

    requested = []
    monkeypatch.setattr(QWindow, "startSystemResize", lambda window, edges: requested.append(edges) or True)
    if kind == "updates":
        dialog = show_dialog(qtbot)
    else:
        dialog = QDialog()
        qtbot.addWidget(dialog)
        QVBoxLayout(dialog).addWidget(QLabel("Resize me"))
        apply_dialog_acrylic(dialog, animate=False)
        dialog.resize(848, 626)
        dialog.show()
        qtbot.wait(10)
    original = QRect(dialog.geometry())
    point = QPoint(*position)
    handle = dialog.childAt(point)
    start = dialog.mapToGlobal(point)
    pointer(handle, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    assert requested == [], "Windows dialog drags must not enter the blocking system resize loop"
    end = start + QPoint(*delta)
    pointer(handle, QEvent.Type.MouseMove, end, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    dx, dy, dw, dh = changes
    expected = QRect(original.x() + dx, original.y() + dy, original.width() + dw, original.height() + dh)
    qtbot.waitUntil(lambda: dialog.geometry() == expected)
    pointer(handle, QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    assert dialog.geometry() == expected


def test_drag_coalesces_pending_sizes_and_applies_the_release_position(qtbot, monkeypatch):
    monkeypatch.setattr(QApplication, "platformName", staticmethod(lambda: "windows"))
    monkeypatch.setattr(QWindow, "startSystemResize", lambda *_: False)
    dialog = show_dialog(qtbot)
    sizes = []

    class ResizeEvents(QObject):
        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.Resize:
                sizes.append(event.size())
            return False

    recorder = ResizeEvents(dialog)
    dialog.installEventFilter(recorder)
    handle = dialog.childAt(QPoint(842, 620))
    start = dialog.mapToGlobal(QPoint(842, 620))
    pointer(handle, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    for step in range(1, 31):
        pointer(
            handle,
            QEvent.Type.MouseMove,
            start + QPoint(step, step),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
    # 输入积压时只按最新尺寸布局一次。
    assert sizes == []
    qtbot.waitUntil(lambda: dialog.size().width() == 878)
    assert len(sizes) == 1
    pointer(handle, QEvent.Type.MouseMove, start + QPoint(40, 40), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    pointer(
        handle,
        QEvent.Type.MouseButtonRelease,
        start + QPoint(50, 50),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
    )
    assert dialog.width() == 898 and dialog.height() == 676
    qtbot.wait(20)
    assert len(sizes) == 2, "A stale queued resize must not run after release"


@pytest.mark.parametrize("edge,position,delta,changes,cursor", EDGES)
def test_native_drag_uses_live_pointer_when_delivered_moves_are_stale(
    qtbot,
    monkeypatch,
    edge,
    position,
    delta,
    changes,
    cursor,
):
    monkeypatch.setattr(QApplication, "platformName", staticmethod(lambda: "windows"))
    monkeypatch.setattr(QMouseEvent, "spontaneous", lambda _: True)
    dialog = show_dialog(qtbot)
    original = QRect(dialog.geometry())
    point = QPoint(*position)
    handle = dialog.childAt(point)
    start = dialog.mapToGlobal(point)
    live_position = [start]
    monkeypatch.setattr(QCursor, "pos", staticmethod(lambda: QPoint(live_position[0])))
    pointer(handle, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)

    live_position[0] = start + QPoint(*delta)
    stale_position = start + QPoint(delta[0] // 4, delta[1] // 4)
    pointer(handle, QEvent.Type.MouseMove, stale_position, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    dx, dy, dw, dh = changes
    expected = QRect(original.x() + dx, original.y() + dy, original.width() + dw, original.height() + dh)
    qtbot.waitUntil(lambda: dialog.geometry() == expected)

    # 指针反向移动后立即采用当前位置，不能重放尚未处理的旧方向输入。
    live_position[0] = start
    pointer(handle, QEvent.Type.MouseMove, stale_position, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: dialog.geometry() == original)
    pointer(handle, QEvent.Type.MouseButtonRelease, start, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)


def test_native_release_uses_release_coordinates_after_pointer_has_moved_on(qtbot, monkeypatch):
    monkeypatch.setattr(QApplication, "platformName", staticmethod(lambda: "windows"))
    monkeypatch.setattr(QMouseEvent, "spontaneous", lambda _: True)
    dialog = show_dialog(qtbot)
    point = QPoint(842, 620)
    handle = dialog.childAt(point)
    start = dialog.mapToGlobal(point)
    live_position = [start]
    monkeypatch.setattr(QCursor, "pos", staticmethod(lambda: QPoint(live_position[0])))
    pointer(handle, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    live_position[0] = start + QPoint(60, 40)
    pointer(handle, QEvent.Type.MouseMove, start + QPoint(10, 5), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: dialog.width() == 908 and dialog.height() == 666)

    # 延迟释放事件对应释放位置，而非后来自由移动的光标。
    live_position[0] = start + QPoint(400, 300)
    pointer(handle, QEvent.Type.MouseMove, start + QPoint(15, 8), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    pointer(
        handle,
        QEvent.Type.MouseButtonRelease,
        start + QPoint(70, 50),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
    )
    assert dialog.size().width() == 918 and dialog.size().height() == 676
    qtbot.wait(20)
    assert dialog.size().width() == 918 and dialog.size().height() == 676


def test_windows_live_resize_defers_layout_until_input_callback_returns(qtbot):
    if QApplication.platformName() != "windows":
        pytest.skip("Requires real Windows window positioning")
    dialog = show_dialog(qtbot)
    applying = [False]
    synchronous_layouts = []

    class ObserveResize(QObject):
        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.Resize and applying[0]:
                synchronous_layouts.append(event.size())
            return False

    observer = ObserveResize(dialog)
    dialog.installEventFilter(observer)
    handle = dialog.childAt(QPoint(842, 620))
    start = dialog.mapToGlobal(QPoint(842, 620))
    end = start + QPoint(40, 30)
    pointer(handle, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    pointer(handle, QEvent.Type.MouseMove, end, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    handle._resize_timer.stop()
    applying[0] = True
    try:
        handle._apply_pending_resize()
    finally:
        applying[0] = False
    assert synchronous_layouts == [], "Resizing must yield to input before Qt reflows the contents"
    qtbot.waitUntil(lambda: dialog.width() == 888 and dialog.height() == 656)
    pointer(handle, QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    assert dialog.width() == 888 and dialog.height() == 656


def test_hidden_dialog_cancels_pending_resize(qtbot, monkeypatch):
    monkeypatch.setattr(QApplication, "platformName", staticmethod(lambda: "windows"))
    monkeypatch.setattr(QWindow, "startSystemResize", lambda *_: False)
    dialog = show_dialog(qtbot)
    handle = dialog.childAt(QPoint(842, 620))
    start = dialog.mapToGlobal(QPoint(842, 620))
    original = dialog.size()
    pointer(handle, QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    pointer(handle, QEvent.Type.MouseMove, start + QPoint(40, 30), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    dialog.hide()
    qtbot.wait(20)
    dialog.show()
    assert dialog.size() == original


@pytest.mark.parametrize("edge,position,delta,changes,cursor", EDGES)
@pytest.mark.parametrize("platform", ["windows", "wayland"])
def test_dragging_resizes_when_system_resize_is_unavailable(
    qtbot,
    monkeypatch,
    edge,
    position,
    delta,
    changes,
    cursor,
    platform,
):
    monkeypatch.setattr(QApplication, "platformName", staticmethod(lambda: platform))
    monkeypatch.setattr(QWindow, "startSystemResize", lambda window, edges: False)
    dialog = show_dialog(qtbot)
    original = QRect(dialog.geometry())
    point = QPoint(*position)
    handle = dialog.childAt(point)
    global_pos = dialog.mapToGlobal(point)
    pointer(handle, QEvent.Type.MouseButtonPress, global_pos, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    end = global_pos + QPoint(*delta)
    pointer(handle, QEvent.Type.MouseMove, end, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    pointer(handle, QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    dx, dy, dw, dh = changes
    assert dialog.geometry() == QRect(
        original.x() + dx, original.y() + dy, original.width() + dw, original.height() + dh
    )
    qtbot.wait(10)
    action = dialog._update_all_btn
    assert dialog.rect().contains(QRect(action.mapTo(dialog, QPoint()), action.size()))
    assert dialog._scroll.horizontalScrollBar().maximum() == 0
    # 松开鼠标即结束拖动，随后移回旧手柄也不能继续调整。
    pointer(handle, QEvent.Type.MouseMove, end + QPoint(50, 50), Qt.MouseButton.NoButton, Qt.MouseButton.NoButton)
    assert dialog.size().width() == original.width() + dw


@pytest.mark.parametrize("limit,delta", [("minimum", (2000, 2000)), ("maximum", (-2000, -2000))])
def test_dragging_top_left_honors_size_limits_and_keeps_opposite_corner(qtbot, monkeypatch, limit, delta):
    monkeypatch.setattr(QWindow, "startSystemResize", lambda window, edges: False)
    dialog = show_dialog(qtbot)
    dialog.setMaximumSize(1000, 760)
    original = QRect(dialog.geometry())
    point = QPoint(5, 5)
    handle = dialog.childAt(point)
    global_pos = dialog.mapToGlobal(point)
    pointer(handle, QEvent.Type.MouseButtonPress, global_pos, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    end = global_pos + QPoint(*delta)
    pointer(handle, QEvent.Type.MouseMove, end, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    pointer(handle, QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    assert dialog.size() == (dialog.minimumSize() if limit == "minimum" else dialog.maximumSize())
    assert dialog.geometry().bottomRight() == original.bottomRight()


def test_consecutive_corner_drags_use_the_resized_window_bounds(qtbot, monkeypatch):
    monkeypatch.setattr(QWindow, "startSystemResize", lambda window, edges: False)
    dialog = show_dialog(qtbot)
    for width, height in ((620, 430), (1000, 720), (420, 340), (848, 626)):
        original = QRect(dialog.geometry())
        point = dialog.rect().bottomRight() - QPoint(5, 5)
        handle = dialog.childAt(point)
        assert handle.cursor().shape() == Qt.CursorShape.SizeFDiagCursor
        global_pos = dialog.mapToGlobal(point)
        pointer(handle, QEvent.Type.MouseButtonPress, global_pos, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
        end = global_pos + QPoint(width - dialog.width(), height - dialog.height())
        pointer(handle, QEvent.Type.MouseMove, end, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
        pointer(handle, QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
        qtbot.wait(10)
        assert dialog.geometry() == QRect(original.x(), original.y(), width, height)
        assert dialog._scroll.horizontalScrollBar().maximum() == 0
        action = dialog._update_all_btn
        assert dialog.rect().contains(QRect(action.mapTo(dialog, QPoint()), action.size()))


@pytest.mark.parametrize("state", [Qt.WindowState.WindowMaximized, Qt.WindowState.WindowFullScreen])
def test_maximized_and_fullscreen_windows_hide_handles_until_restored(qtbot, state):
    dialog = show_dialog(qtbot)
    handles = [dialog.childAt(QPoint(*position)) for _, position, _, _, _ in EDGES]
    dialog.setWindowState(state)
    qtbot.wait(10)
    assert all(not handle.isVisibleTo(dialog) for handle in handles)
    dialog.showNormal()
    qtbot.wait(10)
    assert all(handle.isVisibleTo(dialog) for handle in handles)
    corner = dialog.childAt(dialog.rect().bottomRight() - QPoint(5, 5))
    assert corner.cursor().shape() == Qt.CursorShape.SizeFDiagCursor


def test_resize_regions_leave_scrollbar_and_close_button_accessible(qtbot):
    dialog = show_dialog(qtbot)
    scrollbar = dialog._scroll.verticalScrollBar()
    assert scrollbar.isVisible()
    center = scrollbar.mapTo(dialog, scrollbar.rect().center())
    assert dialog.childAt(center) is scrollbar
    close = dialog.findChild(type(dialog._recheck_btn), "updateClose")
    center = close.mapTo(dialog, close.rect().center())
    assert dialog.childAt(center) is close


def test_shrinking_keeps_the_ignore_message_and_undo_action_inside_window(qtbot):
    dialog = show_dialog(qtbot)
    dialog.show_message("已忽略所选 Mod 的这个版本，需要恢复时可以点击撤销。", undo=lambda: True)
    dialog.resize(420, 340)
    qtbot.wait(10)
    message = dialog._message
    assert dialog.rect().contains(QRect(message.mapTo(dialog, QPoint()), message.size()))
    undo = dialog._undo_btn
    center = undo.mapTo(dialog, undo.rect().center())
    assert dialog.childAt(center) is undo
