from types import SimpleNamespace

import pytest
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

UPDATE = QEvent.Type.UpdateRequest


class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.updates = 0
        self.on_update = None

    def event(self, event):
        if event.type() == UPDATE:
            self.updates += 1
            if self.on_update:
                self.on_update()
        return super().event(event)


@pytest.fixture
def capture(qtbot):
    from src.ui.backgrounds.repaint_coalescer import WindowRepaintCoalescer

    window = Window()
    root = QWidget(window)
    effect = QGraphicsOpacityEffect(root)
    root.setGraphicsEffect(effect)
    coalescer = WindowRepaintCoalescer(window, root, effect)
    yield SimpleNamespace(window=window, root=root, effect=effect, coalescer=coalescer, create=WindowRepaintCoalescer)
    if shiboken6.isValid(coalescer):
        coalescer.stop()
    # 生命周期测试自行销毁窗口并管理所有权，避免 qtbot 再次关闭已失效对象。
    if shiboken6.isValid(window):
        shiboken6.delete(window)


def request(window, count=1):
    for _ in range(count):
        QCoreApplication.sendEvent(window, QEvent(UPDATE))


def test_native_presenter_defers_its_window_sync(qtbot, monkeypatch):
    from src.ui.backgrounds import video_presenter
    from src.ui.widgets.background_overlay import BackgroundOverlay

    # 保留真实控件和效果所有权，仅隔离视频子进程。
    monkeypatch.setattr(
        video_presenter.subprocess,
        "Popen",
        lambda *args, **kwargs: SimpleNamespace(poll=lambda: None, wait=lambda **kwargs: 0),
    )
    window = Window()
    qtbot.addWidget(window)
    root = QWidget(window)
    overlay = BackgroundOverlay(root)
    presenter = video_presenter.NativeVideoPresenter(overlay, window)
    try:
        before = window.updates
        request(window, 5)
        assert window.updates == before
        qtbot.waitUntil(lambda: window.updates == before + 1)
        presenter.playback("pause")
        presenter.set_suspended(True)
        request(window)
        qtbot.waitUntil(lambda: window.updates == before + 2)
        request(window)
        presenter.stop()
        QCoreApplication.sendPostedEvents(window, UPDATE)
        assert window.updates == before + 3
    finally:
        presenter.stop()
        presenter._finish()


def test_burst_is_delayed_then_delivered_once(capture, qtbot):
    request(capture.window, 12)
    assert capture.window.updates == 0
    qtbot.waitUntil(lambda: capture.window.updates == 1)
    qtbot.wait(25)
    assert capture.window.updates == 1


def test_continuous_requests_do_not_restart_the_first_deadline(capture, qtbot):
    requests = []
    flood = QTimer()
    flood.setInterval(1)

    def request_again():
        requests.append(True)
        request(capture.window)

    flood.timeout.connect(request_again)
    try:
        flood.start()
        qtbot.waitUntil(lambda: capture.window.updates > 0, timeout=500)
        assert flood.isActive()
        assert len(requests) > 1
    finally:
        flood.stop()


def test_request_during_replay_waits_until_nested_event_loop_returns(capture, qtbot):
    seen = []

    def replayed():
        if capture.window.updates != 1:
            return
        request(capture.window)
        seen.append(capture.window.updates)
        loop = QEventLoop()
        QTimer.singleShot(25, loop.quit)
        loop.exec()
        seen.append(capture.window.updates)

    capture.window.on_update = replayed
    request(capture.window)
    qtbot.waitUntil(lambda: capture.window.updates == 2)
    assert seen == [1, 1]


def test_stop_hands_pending_request_back_to_qt_without_synchronous_paint(capture):
    request(capture.window, 3)
    capture.coalescer.stop()
    assert capture.window.updates == 0
    QCoreApplication.sendPostedEvents(capture.window, UPDATE)
    assert capture.window.updates == 1
    request(capture.window)
    assert capture.window.updates == 2


def test_stop_during_replay_transfers_the_nested_request(capture, qtbot):
    seen = []

    def replayed():
        if capture.window.updates == 1:
            request(capture.window)
            capture.coalescer.stop()
            seen.append(capture.window.updates)

    capture.window.on_update = replayed
    request(capture.window)
    qtbot.waitUntil(lambda: capture.window.updates == 2)
    assert seen == [1]


def test_replacing_capture_effect_hands_pending_sync_back_to_qt(capture):
    request(capture.window)
    capture.root.setGraphicsEffect(None)
    QCoreApplication.sendPostedEvents(capture.window, UPDATE)
    assert capture.window.updates == 1
    request(capture.window)
    assert capture.window.updates == 2


def test_destroyed_window_cancels_queued_callback(capture, qtbot):
    request(capture.window)
    shiboken6.delete(capture.window)
    qtbot.wait(25)
    assert not shiboken6.isValid(capture.coalescer)


def test_destroyed_root_transfers_sync_to_surviving_window(capture):
    request(capture.window)
    shiboken6.delete(capture.root)
    QCoreApplication.sendPostedEvents(capture.window, UPDATE)
    assert capture.window.updates == 1


def test_only_one_coalescer_per_window_can_consume_requests(capture, qtbot):
    request(capture.window)
    replacement = capture.create(capture.window, capture.root, capture.effect)
    try:
        capture.coalescer.stop()
        request(capture.window)
        qtbot.waitUntil(lambda: capture.window.updates == 1)
        qtbot.wait(25)
        assert capture.window.updates == 1
    finally:
        replacement.stop()


def test_other_windows_and_root_receive_update_requests_immediately(capture, qtbot):
    other = Window()
    qtbot.addWidget(other)
    child = Window()
    child.setParent(capture.root)
    request(other)
    request(child)
    assert (other.updates, child.updates, capture.window.updates) == (1, 1, 0)


def test_hidden_window_still_receives_its_pending_sync(capture, qtbot):
    request(capture.window)
    capture.window.hide()
    qtbot.waitUntil(lambda: capture.window.updates == 1)


def test_deferred_sync_captures_the_latest_real_widget_pixels(qtbot):
    from src.ui.backgrounds.foreground import ForegroundCaptureEffect
    from src.ui.backgrounds.repaint_coalescer import WindowRepaintCoalescer

    window = QWidget()
    qtbot.addWidget(window)
    window.resize(80, 60)
    root = QWidget(window)
    root.setGeometry(0, 0, 80, 60)
    child = QWidget(root)
    child.setGeometry(0, 0, 80, 60)
    child.setStyleSheet("background: blue")
    effect = ForegroundCaptureEffect(root)
    frames = []
    effect.frame_ready.connect(frames.append)
    root.setGraphicsEffect(effect)
    coalescer = WindowRepaintCoalescer(window, root, effect)
    try:
        window.show()
        qtbot.waitUntil(lambda: bool(frames))
        frames.clear()
        child.setStyleSheet("background: red")
        child.repaint()
        child.setStyleSheet("background: green")
        child.repaint()
        assert frames == []
        qtbot.waitUntil(lambda: bool(frames))
        assert frames[-1].pixelColor(10, 10) == QColor("green")
    finally:
        coalescer.stop()
