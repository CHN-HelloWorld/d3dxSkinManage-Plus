import ctypes
import importlib
import platform
import sys

import pytest
from PySide6.QtCore import QSize
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat


@pytest.fixture
def mapping():
    return importlib.import_module("src.ui.backgrounds.video_frame")


@pytest.mark.parametrize("fallback", [False, True])
def test_frame_write_read_and_unmap(qapp, mapping, monkeypatch, fallback):
    if fallback:
        monkeypatch.setattr(mapping, "_native_functions", lambda: None)
    frame = QVideoFrame(QVideoFrameFormat(QSize(16, 16), QVideoFrameFormat.PixelFormat.Format_NV12))
    assert mapping.map_video_frame(frame, QVideoFrame.MapMode.WriteOnly)
    try:
        frame.bits(0)[:] = bytes([93]) * frame.mappedBytes(0)
    finally:
        mapping.unmap_video_frame(frame)
    assert not frame.isMapped()
    assert mapping.map_video_frame(frame, QVideoFrame.MapMode.ReadOnly)
    try:
        assert bytes(frame.bits(0)[:16]) == bytes([93]) * 16
    finally:
        mapping.unmap_video_frame(frame)
    assert not frame.isMapped()


@pytest.mark.parametrize("fallback", [False, True])
def test_invalid_frame_map_returns_false(qapp, mapping, monkeypatch, fallback):
    if fallback:
        monkeypatch.setattr(mapping, "_native_functions", lambda: None)
    frame = QVideoFrame()
    assert mapping.map_video_frame(frame, QVideoFrame.MapMode.ReadOnly) is False
    assert not frame.isMapped()


@pytest.mark.skipif(
    sys.platform != "win32" or platform.machine().lower() not in ("amd64", "x86_64"), reason="MSVC x64 public exports"
)
def test_native_functions_release_the_gil(mapping):
    native = mapping._native_functions()
    assert native is not None, "current Qt build provides public map/unmap exports"
    library, map_frame, unmap_frame = native
    assert isinstance(library, ctypes.CDLL)
    assert not map_frame._flags_ & ctypes._FUNCFLAG_PYTHONAPI
    assert not unmap_frame._flags_ & ctypes._FUNCFLAG_PYTHONAPI


def test_unsupported_platform_uses_binding(mapping, monkeypatch):
    mapping._native_functions.cache_clear()
    with monkeypatch.context() as patch:
        patch.setattr(mapping.sys, "platform", "unsupported")
        assert mapping._native_functions() is None
    mapping._native_functions.cache_clear()


def test_missing_public_symbols_uses_binding(mapping, monkeypatch):
    mapping._native_functions.cache_clear()
    with monkeypatch.context() as patch:
        patch.setattr(mapping.ctypes, "CDLL", lambda *args, **kwargs: object())
        assert mapping._native_functions() is None
    mapping._native_functions.cache_clear()
