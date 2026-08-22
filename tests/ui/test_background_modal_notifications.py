from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt
from PySide6.QtGui import QFocusEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QMainWindow, QPushButton, QWidget

from src.ui.backgrounds import video_presenter
from src.ui.widgets.background_overlay import BackgroundOverlay

BLOCKED = QEvent.Type.WindowBlocked
UNBLOCKED = QEvent.Type.WindowUnblocked


class EventRecorder(QObject):
    def __init__(self, watched, kinds=(BLOCKED, UNBLOCKED)):
        super().__init__(watched)
        self.kinds = kinds
        self.events = []
        watched.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() in self.kinds:
            self.events.append(event.type())
        return False


@pytest.fixture
def foreground(qtbot, monkeypatch):
    # 使用真实显示器、控件层次和 QWindow 事件分发，仅省略独立视频进程。
    monkeypatch.setattr(
        video_presenter.subprocess,
        "Popen",
        lambda *args, **kwargs: SimpleNamespace(poll=lambda: None, wait=lambda **kwargs: 0),
    )
    window = QMainWindow()
    qtbot.addWidget(window)
    window.resize(240, 180)
    root = QWidget()
    window.setCentralWidget(root)
    overlay = BackgroundOverlay(root)
    overlay.setGeometry(0, 0, 240, 180)
    visible = QWidget(root)
    visible.setGeometry(0, 0, 240, 90)
    button = QPushButton("Click", visible)
    button.setGeometry(10, 10, 80, 30)
    edit = QLineEdit(visible)
    edit.setGeometry(100, 10, 100, 30)
    hidden = QWidget(root)
    hidden.setGeometry(0, 90, 240, 90)
    hidden_child = QWidget(hidden)
    hidden_child.setGeometry(0, 0, 20, 20)
    hidden.hide()
    root.winId()
    widgets = dict(
        root=root, overlay=overlay, visible=visible, button=button, edit=edit, hidden=hidden, hidden_child=hidden_child
    )
    # 先安装观察器，使其看到经过显示器过滤后实际分发的事件。
    widget_events = {name: EventRecorder(widget) for name, widget in widgets.items()}
    native_events = {
        name: EventRecorder(widget.windowHandle()) for name, widget in dict(window=window, root=root).items()
    }
    presenters = []

    def start_presenter():
        presenter = video_presenter.NativeVideoPresenter(overlay, window)
        presenters.append(presenter)
        return presenter

    window.show()
    QCoreApplication.processEvents()
    assert not root.isWindow()
    assert root.windowHandle() is not None
    assert not hidden.isVisible()
    assert not hidden_child.isVisible()
    yield SimpleNamespace(
        window=window,
        root=root,
        overlay=overlay,
        button=button,
        edit=edit,
        widgets=widgets,
        widget_events=widget_events,
        native_events=native_events,
        start_presenter=start_presenter,
    )
    for presenter in presenters:
        presenter.stop()
        presenter._finish()


def show_modal(qtbot, parent, modality):
    dialog = QDialog(parent)
    qtbot.addWidget(dialog)
    dialog.resize(100, 70)
    dialog.setWindowModality(modality)
    dialog.show()
    QCoreApplication.processEvents()
    return dialog


def assert_events(recorders, expected):
    assert {name: recorder.events for name, recorder in recorders.items()} == {name: expected for name in recorders}


@pytest.mark.parametrize("modality", [Qt.WindowModality.ApplicationModal, Qt.WindowModality.WindowModal])
def test_each_modal_transition_reaches_the_complete_foreground_once(foreground, qtbot, modality):
    foreground.start_presenter()
    expected = []
    for _ in range(2):
        dialog = show_modal(qtbot, foreground.window, modality)
        expected.append(BLOCKED)
        assert_events(foreground.widget_events, expected)
        assert_events(foreground.native_events, expected)
        dialog.close()
        QCoreApplication.processEvents()
        expected.append(UNBLOCKED)
        assert_events(foreground.widget_events, expected)
        assert_events(foreground.native_events, expected)


@pytest.mark.parametrize("modality", [Qt.WindowModality.ApplicationModal, Qt.WindowModality.WindowModal])
@pytest.mark.parametrize("target_name", ["window", "root"])
def test_native_mouse_and_keyboard_stay_blocked_until_modal_closes(foreground, qtbot, modality, target_name):
    foreground.start_presenter()
    target = getattr(foreground, target_name)
    native = target.windowHandle()
    clicks = []
    foreground.button.clicked[bool].connect(clicks.append)

    def click(widget):
        QTest.mouseClick(native, Qt.MouseButton.LeftButton, pos=widget.mapTo(target, widget.rect().center()))

    # 通过 QWindow 进入原生模态输入路径，直接发送 QWidget 事件无法验证输入阻断。
    click(foreground.button)
    assert len(clicks) == 1
    click(foreground.edit)
    QTest.keyClick(native, Qt.Key.Key_A)
    assert foreground.edit.text() == "a"
    dialog = show_modal(qtbot, foreground.window, modality)
    click(foreground.button)
    QTest.keyClick(native, Qt.Key.Key_B)
    assert len(clicks) == 1
    assert foreground.edit.text() == "a"
    dialog.close()
    QCoreApplication.processEvents()
    click(foreground.button)
    assert len(clicks) == 2
    click(foreground.edit)
    QTest.keyClick(native, Qt.Key.Key_C)
    assert foreground.edit.text() == "ac"


@pytest.mark.parametrize("modality", [Qt.WindowModality.ApplicationModal, Qt.WindowModality.WindowModal])
def test_nested_modal_does_not_unblock_the_foreground_early(foreground, qtbot, modality):
    foreground.start_presenter()
    outer = show_modal(qtbot, foreground.window, modality)
    outer_events = EventRecorder(outer)
    inner = show_modal(qtbot, outer, modality)
    assert QApplication.activeModalWidget() is inner
    assert_events(foreground.widget_events, [BLOCKED])
    assert_events(foreground.native_events, [BLOCKED])
    assert outer_events.events == [BLOCKED]
    inner.close()
    QCoreApplication.processEvents()
    assert QApplication.activeModalWidget() is outer
    assert_events(foreground.widget_events, [BLOCKED])
    assert_events(foreground.native_events, [BLOCKED])
    assert outer_events.events == [BLOCKED, UNBLOCKED]
    outer.close()
    QCoreApplication.processEvents()
    assert_events(foreground.widget_events, [BLOCKED, UNBLOCKED])
    assert_events(foreground.native_events, [BLOCKED, UNBLOCKED])


def test_installing_while_blocked_keeps_the_first_unblocked_notification(foreground, qtbot):
    dialog = show_modal(qtbot, foreground.window, Qt.WindowModality.ApplicationModal)
    assert_events(foreground.widget_events, [BLOCKED, BLOCKED])
    foreground.start_presenter()
    dialog.close()
    QCoreApplication.processEvents()
    assert_events(foreground.widget_events, [BLOCKED, BLOCKED, UNBLOCKED])
    assert_events(foreground.native_events, [BLOCKED, UNBLOCKED])


def test_stopping_while_blocked_restores_normal_qt_propagation(foreground, qtbot):
    presenter = foreground.start_presenter()
    dialog = show_modal(qtbot, foreground.window, Qt.WindowModality.ApplicationModal)
    assert_events(foreground.widget_events, [BLOCKED])
    presenter.stop()
    dialog.close()
    QCoreApplication.processEvents()
    assert_events(foreground.widget_events, [BLOCKED, UNBLOCKED, UNBLOCKED])
    second = show_modal(qtbot, foreground.window, Qt.WindowModality.ApplicationModal)
    second.close()
    QCoreApplication.processEvents()
    assert_events(foreground.widget_events, [BLOCKED, UNBLOCKED, UNBLOCKED, BLOCKED, BLOCKED, UNBLOCKED, UNBLOCKED])
    assert_events(foreground.native_events, [BLOCKED, UNBLOCKED, BLOCKED, UNBLOCKED])


def test_focus_leave_and_activation_events_are_never_deduplicated(foreground):
    kinds = (
        QEvent.Type.Leave,
        QEvent.Type.WindowActivate,
        QEvent.Type.WindowDeactivate,
        QEvent.Type.FocusIn,
        QEvent.Type.FocusOut,
    )
    recorder = foreground.widget_events["root"]
    recorder.kinds = (BLOCKED, UNBLOCKED, *kinds)
    foreground.start_presenter()
    recorder.events.clear()
    QCoreApplication.sendEvent(foreground.root, QEvent(BLOCKED))
    expected = [BLOCKED]
    for kind in kinds:
        for _ in range(2):
            event = QFocusEvent(kind) if kind in (QEvent.Type.FocusIn, QEvent.Type.FocusOut) else QEvent(kind)
            QCoreApplication.sendEvent(foreground.root, event)
            expected.append(kind)
    QCoreApplication.sendEvent(foreground.root, QEvent(BLOCKED))
    QCoreApplication.sendEvent(foreground.root, QEvent(UNBLOCKED))
    expected.append(UNBLOCKED)
    assert recorder.events == expected
