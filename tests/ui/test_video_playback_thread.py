from threading import Event, get_ident

import pytest
from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtMultimedia import QVideoFrame


@pytest.mark.parametrize("stop_loading", [False, True])
def test_player_initialization_and_controls_stay_off_gui(qtbot, monkeypatch, stop_loading):
    from PySide6 import QtMultimedia

    from src.ui.backgrounds.video_playback import VideoPlayback

    entered, release = Event(), Event()
    calls, beats = [], []

    class Player(QObject):
        errorOccurred = Signal(int, str)

        class Loops:
            Infinite = -1

        def __init__(self, parent):
            super().__init__(parent)
            calls.append(("init", get_ident()))
            entered.set()
            assert release.wait(3)

        def setAudioOutput(self, value):
            pass

        def setVideoSink(self, value):
            pass

        def setLoops(self, value):
            calls.append(("loops", get_ident(), value))

        def setSource(self, value):
            calls.append(("source", get_ident(), value))

        def play(self):
            calls.append(("play", get_ident()))

        def pause(self):
            calls.append(("pause", get_ident()))

        def stop(self):
            calls.append(("stop", get_ident()))

    class Audio(QObject):
        def setVolume(self, value):
            calls.append(("volume", get_ident(), value))

    class Sink(QObject):
        videoFrameChanged = Signal(QVideoFrame)

    monkeypatch.setattr(QtMultimedia, "QMediaPlayer", Player)
    monkeypatch.setattr(QtMultimedia, "QAudioOutput", Audio)
    monkeypatch.setattr(QtMultimedia, "QVideoSink", Sink)
    worker = VideoPlayback(QUrl("file:///original-4k60.mp4"), 0.25)
    timer = QTimer()
    timer.timeout.connect(lambda: beats.append(True))
    timer.start(1)
    worker.start()
    try:
        qtbot.waitUntil(entered.is_set)
        qtbot.waitUntil(lambda: len(beats) >= 3)
        if stop_loading:
            worker.stop()
        release.set()
        if not stop_loading:
            qtbot.waitUntil(lambda: any(c[0] == "play" for c in calls))
            worker.setVolume(0.7)
            worker.pause()
            qtbot.waitUntil(lambda: any(c[0] == "pause" for c in calls))
            worker.play()
            qtbot.waitUntil(lambda: sum(c[0] == "play" for c in calls) >= 2)
            assert ("source", calls[0][1], QUrl("file:///original-4k60.mp4")) in calls
            assert ("volume", calls[0][1], 0.7) in calls
            assert ("loops", calls[0][1], Player.Loops.Infinite) in calls
        worker.stop()
        qtbot.waitUntil(lambda: not worker.isRunning())
        assert all(c[1] != get_ident() for c in calls)
        if stop_loading:
            assert not any(c[0] == "play" for c in calls)
        assert worker.take_frame() is None
    finally:
        release.set()
        worker.stop()
        worker.wait()
        timer.stop()


def test_video_mailbox_keeps_latest_and_retires_after_stop(qapp, monkeypatch):
    from src.ui.backgrounds import video_playback as module

    retired = []
    monkeypatch.setattr(module, "retire_video_frame", retired.append)
    worker = module.VideoPlayback(QUrl(), 0)
    frames = []
    for color in ("red", "green", "blue"):
        pixels = QImage(2, 2, QImage.Format.Format_RGB32)
        pixels.fill(QColor(color))
        frames.append(QVideoFrame(pixels))
    worker.offer_frame(frames[0])
    worker.offer_frame(frames[1])
    assert retired == [frames[0]]
    assert worker.take_frame() is frames[1]
    assert worker.take_frame() is None
    worker.stop()
    worker.offer_frame(frames[2])
    assert retired == [frames[0], frames[2]]
    assert worker.take_frame() is None
