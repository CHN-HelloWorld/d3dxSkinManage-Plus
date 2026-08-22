from PySide6.QtGui import QColor, QImage
from PySide6.QtMultimedia import QVideoFrame

from src.ui.backgrounds.controller import BackgroundController
from src.ui.backgrounds.video_playback import VideoPlayback
from src.ui.widgets.background_overlay import BackgroundOverlay


def test_continuous_video_survives_pause_resize_and_source_replacement(qtbot, monkeypatch, tmp_path):
    class Frames(VideoPlayback):
        def run(self):
            while not self._stopping.wait(0.005):
                image = QImage(20, 10, QImage.Format.Format_RGB32)
                image.fill(QColor("blue"))
                self.offer_frame(QVideoFrame(image))
            self._discard_frame()

    monkeypatch.setattr("src.ui.backgrounds.video_playback.VideoPlayback", Frames)
    path = tmp_path / "video.mp4"
    path.touch()
    overlay = BackgroundOverlay()
    qtbot.addWidget(overlay)
    controller = BackgroundController(overlay)
    options = {"source": "video", "video_path": str(path), "fps": 60, "blur": 0}
    try:
        controller.apply(options)
        qtbot.waitUntil(lambda: controller.state == "playing")
        pump = controller._video_pump
        assert not controller._timer.isActive(), "GUI timer must not gate video conversion"
        controller.playback("pause")
        qtbot.wait(40)
        assert controller.state == "paused"
        controller.set_suspended(True)
        controller.apply({**options, "fps": 30, "fit": "contain"})
        controller.set_suspended(False)
        qtbot.wait(40)
        assert controller.state == "paused"
        controller.playback("resume")
        qtbot.waitUntil(lambda: controller.state == "playing")
        assert pump is controller._video_pump
        overlay.resize(120, 80)
        controller.resize()
        qtbot.waitUntil(
            lambda: overlay._orig is not None and overlay._orig.width() == round(120 * overlay.devicePixelRatioF())
        )
        controller.apply({"source": "off"})
        pump.available.emit()  # 来自先前来源的排队通知。
        qtbot.wait(40)
        assert controller._video_pump is None
        assert not controller.active and controller.state == "off"
        controller.apply(options)
        qtbot.waitUntil(lambda: controller.state == "playing")
        assert controller._video_pump is not pump
    finally:
        controller.dispose()
        qtbot.waitUntil(lambda: not controller._retiring)
        import shiboken6

        # Python 闭包仍存活时销毁控制器，避免延迟销毁与后续用例的后台任务重叠。
        shiboken6.delete(controller)


def test_category_dialog_uses_native_window_owner(qtbot):
    from PySide6.QtWidgets import QWidget

    from src.ui.pages.overview import CharacterDialog

    window = QWidget()
    qtbot.addWidget(window)
    page = QWidget(window)
    dialog = CharacterDialog(parent=page)
    assert dialog.parentWidget() is window
