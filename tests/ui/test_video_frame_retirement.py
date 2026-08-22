import ctypes
import threading

import pytest
from PySide6.QtCore import QSize, QUrl
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat

from src.ui.backgrounds import video_frame, video_playback


def frame():
    return QVideoFrame(QVideoFrameFormat(QSize(16, 16), QVideoFrameFormat.PixelFormat.Format_NV12))


def test_video_arrival_hands_old_frame_to_retirement(qapp, monkeypatch):
    old, incoming = frame(), frame()
    owner = video_playback.VideoPlayback(QUrl(), 0)
    owner.offer_frame(old)
    retired = []
    monkeypatch.setattr(video_playback, "retire_video_frame", retired.append)
    owner.offer_frame(incoming)
    assert retired == [old]
    assert owner.take_frame() is incoming
    assert incoming.isValid()


@pytest.mark.parametrize("fallback", [False, True])
def test_clear_releases_only_owned_frame_reference(qapp, monkeypatch, fallback):
    original = frame()
    owned = QVideoFrame(original)
    if fallback:
        monkeypatch.setattr(video_frame, "_native_clear", lambda: None)
    video_frame.clear_video_frame(owned)
    assert owned.isValid() is fallback
    assert original.isValid()


def test_retirement_runs_without_blocking_caller(qapp, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    seen = []

    def clear(value):
        seen.append((threading.get_ident(), value.isValid()))
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(video_frame, "clear_video_frame", clear)
    future = video_frame.retire_video_frame(frame())
    try:
        assert entered.wait(3)
        assert not future.done()
        assert seen == [(seen[0][0], True)]
        assert seen[0][0] != threading.get_ident()
    finally:
        release.set()
        future.result(timeout=3)


def test_native_clear_releases_gil(qapp):
    if video_frame.sys.platform != "win32":
        pytest.skip("Windows Qt exports")
    clear = video_frame._native_clear()
    assert clear is not None
    assert not clear._flags_ & ctypes._FUNCFLAG_PYTHONAPI
