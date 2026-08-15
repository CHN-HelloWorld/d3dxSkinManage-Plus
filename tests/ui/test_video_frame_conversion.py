import pytest
from PySide6.QtCore import QSize
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat


@pytest.mark.parametrize("level,expected", [(16, 0), (126, 128), (235, 255)])
def test_nv12_conversion_preserves_limited_range_and_scales(qapp, level, expected):
    from src.ui.backgrounds.video import convert_mapped_frame

    fmt = QVideoFrameFormat(QSize(128, 72), QVideoFrameFormat.PixelFormat.Format_NV12)
    fmt.setColorRange(QVideoFrameFormat.ColorRange.ColorRange_Video)
    fmt.setColorSpace(QVideoFrameFormat.ColorSpace.ColorSpace_BT709)
    frame = QVideoFrame(fmt)
    assert frame.map(QVideoFrame.MapMode.WriteOnly)
    frame.bits(0)[:] = bytes([level]) * frame.mappedBytes(0)
    frame.bits(1)[:] = bytes([128]) * frame.mappedBytes(1)
    frame.unmap()
    image = convert_mapped_frame(frame, (64, 64), "cover")
    assert image is not None, "bundled FFmpeg must support the common NV12 decoder output"
    assert image.size() == QSize(113, 64)
    color = image.pixelColor(32, 32)
    assert abs(color.red() - expected) <= 2
    assert abs(color.green() - expected) <= 2
    assert abs(color.blue() - expected) <= 2
    assert color.alpha() == 255
    assert not frame.isMapped()


def test_unsupported_video_format_uses_qt_fallback(qapp):
    from src.ui.backgrounds.video import convert_mapped_frame

    assert convert_mapped_frame(QVideoFrame(), (64, 64), "contain") is None


def test_video_downsampling_averages_fine_detail_without_aliasing(qapp):
    from src.ui.backgrounds.video import convert_mapped_frame

    fmt = QVideoFrameFormat(QSize(128, 128), QVideoFrameFormat.PixelFormat.Format_NV12)
    fmt.setColorRange(QVideoFrameFormat.ColorRange.ColorRange_Video)
    fmt.setColorSpace(QVideoFrameFormat.ColorSpace.ColorSpace_BT709)
    frame = QVideoFrame(fmt)
    assert frame.map(QVideoFrame.MapMode.WriteOnly)
    stride = frame.bytesPerLine(0)
    for row in range(128):
        values = bytes(16 if (col + row) % 2 else 235 for col in range(128))
        frame.bits(0)[row * stride : row * stride + len(values)] = values
    frame.bits(1)[:] = bytes([128]) * frame.mappedBytes(1)
    frame.unmap()
    image = convert_mapped_frame(frame, (16, 16), "contain")
    assert image.size() == QSize(16, 16)
    for y in range(2, 14):
        for x in range(2, 14):
            color = image.pixelColor(x, y)
            assert all(abs(value - 128) <= 3 for value in (color.red(), color.green(), color.blue()))


@pytest.mark.parametrize("space,expected", [("BT601", (255, 0, 0)), ("BT709", (255, 24, 0))])
def test_yuv_color_matrix_and_owned_output(qapp, space, expected):
    from src.ui.backgrounds.video import convert_mapped_frame

    fmt = QVideoFrameFormat(QSize(32, 32), QVideoFrameFormat.PixelFormat.Format_YUV420P)
    fmt.setColorRange(QVideoFrameFormat.ColorRange.ColorRange_Video)
    fmt.setColorSpace(getattr(QVideoFrameFormat.ColorSpace, "ColorSpace_" + space))
    frame = QVideoFrame(fmt)
    assert frame.map(QVideoFrame.MapMode.WriteOnly)
    for plane, value in enumerate((81, 90, 240)):
        frame.bits(plane)[:] = bytes([value]) * frame.mappedBytes(plane)
    frame.unmap()
    image = convert_mapped_frame(frame, (32, 32), "contain")
    assert frame.map(QVideoFrame.MapMode.WriteOnly)
    frame.bits(0)[:] = bytes([16]) * frame.mappedBytes(0)
    frame.unmap()
    actual = image.pixelColor(8, 8)
    for value, reference in zip((actual.red(), actual.green(), actual.blue()), expected):
        assert abs(value - reference) <= 3


def test_qt_conversion_exception_returns_an_error_instead_of_raising_from_worker(qapp, monkeypatch):
    from PySide6.QtMultimedia import QVideoFrame

    from src.ui.backgrounds import video

    class BrokenFrame(QVideoFrame):
        def toImage(self):
            raise RuntimeError("conversion failed")

    monkeypatch.setattr(video, "convert_mapped_frame", lambda *args: None)
    image, error = video.convert_owned_frame(BrokenFrame(), (64, 64), "cover")
    assert image.isNull()
    assert "conversion failed" in error
