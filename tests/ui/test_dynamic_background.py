from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QSignalSpy

from src.ui.widgets.background_overlay import BackgroundOverlay


def test_dynamic_frame_supersedes_pending_static_read(qtbot, tmp_path):
    overlay = BackgroundOverlay()
    qtbot.addWidget(overlay)
    old = QImage(10, 10, QImage.Format.Format_RGB32)
    old.fill(QColor("red"))
    path = str(tmp_path / "old.png")
    old.save(path)
    overlay.configure(path, 0, 0, "cover")
    generation = overlay._generation
    frame = QImage(10, 10, QImage.Format.Format_RGB32)
    frame.fill(QColor("blue"))
    overlay.configure_dynamic(0, 0, "cover")
    overlay.set_dynamic_frame(frame)
    overlay._image_read(generation, old)
    qtbot.waitUntil(lambda: overlay._orig is not None)
    assert overlay._orig.toImage().pixelColor(0, 0) == QColor("blue")


def test_repeated_active_frames_do_not_request_extra_repaints(qtbot, monkeypatch):
    overlay = BackgroundOverlay()
    qtbot.addWidget(overlay)
    overlay.set_enabled(True)
    updates = []
    monkeypatch.setattr(overlay, "update", lambda: updates.append(True))
    for _ in range(10):
        overlay.set_enabled(True)
    assert not updates, "only completed frames should repaint, not the repeated active status"


def test_paused_dynamic_frame_refits_after_resize(qtbot):
    from PySide6.QtWidgets import QWidget

    host = QWidget()
    qtbot.addWidget(host)
    overlay = BackgroundOverlay(host)
    host.show()
    overlay.resize(100, 100)
    overlay.show()
    ratio = overlay.devicePixelRatioF()
    overlay.configure_dynamic(0, 0, "contain")
    frame = QImage(200, 100, QImage.Format.Format_RGB32)
    frame.fill(QColor("blue"))
    overlay.set_dynamic_frame(frame)
    overlay.set_enabled(True)
    qtbot.waitUntil(lambda: overlay._orig is not None and overlay._orig.width() == round(100 * ratio))
    overlay.resize(200, 200)
    qtbot.waitUntil(lambda: overlay._orig.width() == round(200 * ratio))
    assert overlay._orig.height() == round(100 * ratio)


def test_video_burst_only_converts_and_displays_latest_frame(qtbot, monkeypatch):
    from threading import get_ident

    import shiboken6
    from PySide6.QtCore import QUrl
    from PySide6.QtMultimedia import QVideoFrame

    from src.ui.backgrounds.controller import BackgroundController
    from src.ui.backgrounds.video_playback import VideoPlayback
    from src.ui.backgrounds.video_pump import VideoFramePump

    overlay = BackgroundOverlay()
    qtbot.addWidget(overlay)
    overlay.configure_dynamic(0, 0, "cover")
    player = BackgroundController(overlay)
    decoder = player._player = VideoPlayback(QUrl(), 0)
    pump = player._video_pump = VideoFramePump(decoder.take_frame, (10, 10), "cover", 30)
    pump.available.connect(player._pump_ready)
    converted = []
    threads = []
    original = QVideoFrame.toImage

    def convert(frame):
        converted.append(frame)
        threads.append(get_ident())
        return original(frame)

    monkeypatch.setattr(QVideoFrame, "toImage", convert)
    for color in ("red", "green", "blue"):
        pixels = QImage(10, 10, QImage.Format.Format_RGB32)
        pixels.fill(QColor(color))
        frame = QVideoFrame(pixels)
        decoder.offer_frame(QVideoFrame(frame))
        # 信号借出的帧可能在槽返回后被释放或复用。
        shiboken6.delete(frame)
    assert converted == []
    try:
        pump.start()
        qtbot.waitUntil(lambda: overlay._orig is not None)
        assert len(converted) == 1
        assert threads[0] != get_ident(), "4K conversion must leave the GUI thread available to paint"
        assert overlay._orig.toImage().pixelColor(0, 0) == QColor("blue")
        qtbot.wait(50)
        assert len(converted) == 1
    finally:
        player.dispose()
        qtbot.waitUntil(lambda: not player._retiring)
        shiboken6.delete(player)


def test_video_conversion_finishing_after_disable_cannot_restore_background(qtbot, monkeypatch):
    from threading import Event

    from PySide6.QtCore import QUrl
    from PySide6.QtMultimedia import QVideoFrame

    from src.ui.backgrounds import video_pump
    from src.ui.backgrounds.controller import BackgroundController
    from src.ui.backgrounds.video_playback import VideoPlayback

    overlay = BackgroundOverlay()
    qtbot.addWidget(overlay)
    overlay.configure_dynamic(0, 0, "cover")
    player = BackgroundController(overlay)
    decoder = player._player = VideoPlayback(QUrl(), 0)
    pump = player._video_pump = video_pump.VideoFramePump(decoder.take_frame, (10, 10), "cover", 30)
    pump.available.connect(player._pump_ready)
    entered, release = Event(), Event()
    original = video_pump.convert_owned_frame

    def gated(frame, size, fit):
        entered.set()
        release.wait(3)
        return original(frame, size, fit)

    monkeypatch.setattr(video_pump, "convert_owned_frame", gated)
    image = QImage(10, 10, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    decoder.offer_frame(QVideoFrame(image))
    pump.start()
    try:
        qtbot.waitUntil(entered.is_set)
        player.apply({"source": "off"})
    finally:
        release.set()
        player.dispose()
        qtbot.waitUntil(lambda: not player._retiring)
    assert not player.active
    assert overlay._orig is None
    import shiboken6

    shiboken6.delete(player)


class FakeScene(QObject):
    error = Signal(str)
    finished = Signal()
    instances = []

    def __init__(self, *args):
        super().__init__()
        self.stopped = False
        self.is_closed = False
        self.instances.append(self)

    def start(self, size, fps):
        self.size = size

    def stop(self):
        self.stopped = self.is_closed = True
        self.finished.emit()

    def resize(self, size):
        self.size = size

    def set_fps(self, fps):
        pass

    def set_volume(self, volume):
        self.volume = volume

    def take_frame(self):
        return None


def test_invalid_engine_stops_scene_but_preserves_selection(qtbot, monkeypatch):
    from src.ui.backgrounds import controller

    monkeypatch.setattr(controller, "SceneSource", FakeScene)
    overlay = BackgroundOverlay()
    qtbot.addWidget(overlay)
    player = controller.BackgroundController(overlay)
    cfg = {
        "source": "scene",
        "scene_path": "project.json",
        "engine_path": "engine.exe",
        "fps": 30,
        "scene_scale": 2,
        "blur": 0,
        "fit": "cover",
        "scrim_dark": 45,
    }
    player.apply(cfg, engine_valid=True)
    scene = FakeScene.instances[-1]
    player.apply(cfg, engine_valid=False)
    assert scene.stopped
    assert not player.active
    assert player.options["source"] == "scene"
    player.playback("retry")
    assert not player.active
    assert FakeScene.instances[-1] is scene
    player.dispose()


def test_scene_volume_is_applied_live_and_restored_after_resume(qtbot, monkeypatch):
    from src.ui.backgrounds import controller

    monkeypatch.setattr(controller, "SceneSource", FakeScene)
    overlay = BackgroundOverlay()
    qtbot.addWidget(overlay)
    player = controller.BackgroundController(overlay)
    options = {
        "source": "scene",
        "scene_path": "project.json",
        "engine_path": "engine.exe",
        "volume": 25,
        "fps": 30,
        "blur": 0,
    }
    player.apply(options, engine_valid=True)
    source = player._scene
    assert source.volume == 25
    player.apply({**options, "volume": 70}, engine_valid=True)
    assert player._scene is source and source.volume == 70
    player.playback("pause")
    player.playback("resume")
    assert player._scene is not source and player._scene.volume == 70
    player.dispose()


def test_queued_scene_error_after_pause_is_ignored(qtbot, monkeypatch):
    from src.ui.backgrounds import controller

    monkeypatch.setattr(controller, "SceneSource", FakeScene)
    overlay = BackgroundOverlay()
    qtbot.addWidget(overlay)
    player = controller.BackgroundController(overlay)
    player.apply(
        {"source": "scene", "scene_path": "project.json", "engine_path": "engine.exe", "fps": 30, "blur": 0},
        engine_valid=True,
    )
    scene = player._scene
    player.playback("pause")
    scene.error.emit("late capture error")
    assert player.state == "paused"
    player.dispose()


def test_old_blur_result_cannot_replace_new_source(qtbot):
    overlay = BackgroundOverlay()
    qtbot.addWidget(overlay)
    overlay.configure_dynamic(0, 0, "cover")
    generation = overlay._generation
    overlay.reset_dynamic_frames()
    stale = QImage(5, 5, QImage.Format.Format_RGB32)
    stale.fill(QColor("red"))
    overlay._frame_blurred(generation, stale)
    assert overlay._orig is None


def test_missing_image_never_enables_transparent_surfaces(qtbot, tmp_path):
    from src.ui.backgrounds.controller import BackgroundController

    overlay = BackgroundOverlay()
    qtbot.addWidget(overlay)
    player = BackgroundController(overlay)
    transitions = QSignalSpy(player.active_changed)
    cfg = {"source": "image", "path": str(tmp_path / "missing.png"), "blur": 0}
    player.apply(cfg)
    qtbot.waitUntil(lambda: player.state == "error")
    player.apply(cfg)
    assert transitions.count() == 0
    player.dispose()


def test_manual_pause_survives_minimize_and_parameter_changes(qtbot, monkeypatch):
    from src.ui.backgrounds import controller

    monkeypatch.setattr(controller, "SceneSource", FakeScene)
    overlay = BackgroundOverlay()
    qtbot.addWidget(overlay)
    player = controller.BackgroundController(overlay)
    cfg = {
        "source": "scene",
        "scene_path": "project.json",
        "engine_path": "engine.exe",
        "fps": 30,
        "scene_scale": 2,
        "blur": 0,
        "fit": "cover",
        "scrim_dark": 45,
    }
    player.apply(cfg, engine_valid=True)
    player.playback("pause")
    player.set_suspended(True)
    player.apply({**cfg, "blur": 10}, engine_valid=True)
    player.set_suspended(False)
    assert player.state == "paused"
    assert player._scene is None
    player.playback("resume")
    assert player._scene is not None
    player.dispose()
