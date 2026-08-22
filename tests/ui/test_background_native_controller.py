import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from src.ui.backgrounds.controller import BackgroundController
from src.ui.widgets.background_overlay import BackgroundOverlay


@pytest.fixture
def native_background(qtbot, monkeypatch, tmp_path):
    from src.ui.backgrounds import video_presenter

    class Presenter(QObject):
        state_changed = Signal(str, str)
        preview_ready = Signal(QImage)

        def __init__(self, parent):
            super().__init__(parent)
            self.paused = False
            self.suspended = False

        def configure(self, options):
            pass

        def playback(self, action):
            self.paused = action == "pause"

        def set_suspended(self, value):
            self.suspended = value

        def refresh(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(video_presenter, "create_presenter", lambda overlay, parent: Presenter(parent))
    path = tmp_path / "video.mp4"
    path.touch()
    overlay = BackgroundOverlay()
    qtbot.addWidget(overlay)
    controller = BackgroundController(overlay)
    controller.apply(dict(source="video", video_path=str(path), fps=60, blur=0))
    yield controller, controller._native_presenter
    controller.dispose()


@pytest.mark.parametrize("gate", ["suspended", "paused"])
def test_first_native_frame_received_while_inactive_is_available_on_resume(native_background, gate):
    controller, presenter = native_background
    if gate == "suspended":
        controller.set_suspended(True)
    else:
        controller.playback("pause")
    presenter.state_changed.emit("playing", "")
    assert controller.state != "playing"

    if gate == "suspended":
        controller.set_suspended(False)
    else:
        controller.playback("resume")
    # 恢复之前渲染器已发送唯一一次播放状态转换。
    assert controller.state == "playing"
    assert controller.active


def test_resume_while_suspended_updates_renderer_desired_playback(native_background):
    controller, presenter = native_background
    presenter.state_changed.emit("playing", "")
    controller.playback("pause")
    controller.set_suspended(True)
    controller.playback("resume")
    assert not presenter.paused
    assert presenter.suspended
    controller.set_suspended(False)
    assert not presenter.paused and not presenter.suspended
    assert controller.state == "playing"


def test_stale_native_status_cannot_resume_paused_or_replaced_background(qtbot, monkeypatch, tmp_path):
    from src.ui.backgrounds import video_presenter

    class Presenter(QObject):
        state_changed = Signal(str, str)
        preview_ready = Signal(QImage)

        def configure(self, options):
            pass

        def playback(self, action):
            pass

        def set_suspended(self, value):
            pass

        def refresh(self):
            pass

        def stop(self):
            pass

    created = []

    def create(overlay, parent):
        presenter = Presenter(parent)
        created.append(presenter)
        return presenter

    monkeypatch.setattr(video_presenter, "create_presenter", create)
    path = tmp_path / "video.mp4"
    path.touch()
    overlay = BackgroundOverlay()
    qtbot.addWidget(overlay)
    controller = BackgroundController(overlay)
    options = dict(source="video", video_path=str(path), fps=60, blur=0)
    controller.apply(options)
    first = created[0]
    first.state_changed.emit("playing", "")
    assert controller.state == "playing" and controller.active
    controller.playback("pause")
    first.state_changed.emit("playing", "")
    assert controller.state == "paused"
    controller.apply(dict(source="off"))
    first.state_changed.emit("playing", "")
    first.state_changed.emit("error", "late failure")
    assert controller.state == "off" and not controller.active
    controller.apply(options)
    first.state_changed.emit("playing", "")
    assert controller.state == "loading"
    created[-1].state_changed.emit("playing", "")
    assert controller.state == "playing"
    controller.dispose()
