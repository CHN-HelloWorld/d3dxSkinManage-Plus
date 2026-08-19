import ctypes as c

import pytest
import shiboken6
from PySide6.QtCore import QSize
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat

from src.ui.backgrounds.video_convert import _libraries, convert_mapped_frame


def video_frame(pixel, space="BT709", full=False, size=(384, 216)):
    fmt = QVideoFrameFormat(QSize(*size), getattr(QVideoFrameFormat.PixelFormat, "Format_" + pixel))
    fmt.setColorSpace(getattr(QVideoFrameFormat.ColorSpace, "ColorSpace_" + space))
    fmt.setColorRange(getattr(QVideoFrameFormat.ColorRange, "ColorRange_" + ("Full" if full else "Video")))
    frame = QVideoFrame(fmt)
    assert frame.map(QVideoFrame.MapMode.WriteOnly)
    # 高频亮度和色度变化覆盖每个分片边界及带填充的行。
    for plane in range(frame.planeCount()):
        count = frame.mappedBytes(plane)
        pattern = bytes((i * 79 + plane * 97) % 256 for i in range(251))
        data = (pattern * (count // len(pattern) + 1))[:count]
        frame.bits(plane)[:] = data
    frame.unmap()
    return frame


def pixels(image):
    return bytes(image.constBits())


@pytest.mark.parametrize(
    "pixel,space,full,target",
    [
        ("YUV420P", "BT709", False, (180, 120)),
        ("NV12", "BT601", False, (180, 120)),
        ("YUV422P", "BT2020", False, (180, 120)),
        ("YUV420P", "BT709", True, (180, 120)),
        ("NV12", "BT709", False, (480, 270)),
    ],
)
def test_parallel_conversion_matches_serial_pixels_and_owns_output(qapp, pixel, space, full, target):
    from src.ui.backgrounds.video_scaler import FrameScaler

    libraries = _libraries()
    assert libraries is not None
    frame = video_frame(pixel, space, full)
    expected = convert_mapped_frame(frame, target, "cover")
    scale, util = libraries
    flags = 32 if expected.width() < frame.width() and expected.height() < frame.height() else 4
    spec = (
        frame.width(),
        frame.height(),
        util.av_get_pix_fmt(pixel.lower().encode()),
        expected.width(),
        expected.height(),
        flags,
        {"BT709": 1, "BT2020": 9}.get(space, 5),
        int(full),
    )
    scaler = FrameScaler(libraries, spec, threads=4)
    try:
        assert frame.map(QVideoFrame.MapMode.ReadOnly)
        source, strides = (c.c_void_p * 4)(), (c.c_int * 4)()
        buffers = [frame.bits(plane) for plane in range(frame.planeCount())]
        for plane, buffer in enumerate(buffers):
            source[plane], strides[plane] = int(shiboken6.VoidPtr(buffer)), frame.bytesPerLine(plane)
        first = scaler.convert(source, strides)
        assert first.size() == expected.size()
        assert pixels(first) == pixels(expected)
        frame.unmap()
        assert frame.map(QVideoFrame.MapMode.WriteOnly)
        frame.bits(0)[:] = bytes([16]) * frame.mappedBytes(0)
        frame.unmap()
        assert frame.map(QVideoFrame.MapMode.ReadOnly)
        buffers = [frame.bits(plane) for plane in range(frame.planeCount())]
        for plane, buffer in enumerate(buffers):
            source[plane], strides[plane] = int(shiboken6.VoidPtr(buffer)), frame.bytesPerLine(plane)
        second = scaler.convert(source, strides)
        assert pixels(second) != pixels(first)
    finally:
        if frame.isMapped():
            frame.unmap()
        scaler.close()
        scaler.close()  # 部分初始化和重复停止后的清理也须安全。
    assert pixels(first) == pixels(expected)


def test_scaling_session_handles_resize_and_color_changes_without_stale_pixels(qapp):
    from src.ui.backgrounds.video_scaler import scaling_session

    variants = [
        (video_frame("YUV420P", size=(1920, 1080)), (1280, 800), "cover"),
        (video_frame("NV12", "BT601", True, size=(1920, 1080)), (1440, 900), "contain"),
        (video_frame("YUV420P", "BT2020", size=(1920, 1080)), (1300, 800), "cover"),
    ]
    expected = [convert_mapped_frame(frame, size, fit) for frame, size, fit in variants]
    with scaling_session():
        actual = [convert_mapped_frame(frame, size, fit) for frame, size, fit in variants]
    for output, reference in zip(actual, expected):
        assert output.size() == reference.size()
        assert pixels(output) == pixels(reference)
