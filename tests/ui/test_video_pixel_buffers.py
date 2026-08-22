import gc
from concurrent.futures import ThreadPoolExecutor

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat
from PySide6.QtTest import QSignalSpy


@pytest.mark.parametrize(
    "image_format,pixel,want",
    [
        (QImage.Format.Format_RGB32, bytes((56, 34, 12, 255)), (12, 34, 56, 255)),
        (QImage.Format.Format_ARGB32, bytes((56, 34, 12, 78)), (12, 34, 56, 78)),
        (QImage.Format.Format_RGBA8888, bytes((12, 34, 56, 78)), (12, 34, 56, 78)),
    ],
)
def test_rgb_scaling_reads_padded_readonly_pixels_without_detaching_source(image_format, pixel, want):
    from src.ui.backgrounds.video_convert import scale_image

    data = (pixel * 3 + b"\xff" * 8) * 2
    source = QImage(memoryview(data), 3, 2, 20, image_format)
    alias = QImage(source)
    cache_key = source.cacheKey()
    result = scale_image(source, QSize(6, 4))
    assert result.size() == QSize(6, 4)
    assert source.cacheKey() == cache_key == alias.cacheKey()
    assert bytes(source.constBits()) == data
    assert result.pixelColor(0, 0).getRgb() == want
    assert result.pixelColor(5, 3).getRgb() == want
    del alias, source, data
    gc.collect()
    assert result.pixelColor(3, 2).getRgb() == want


@pytest.mark.parametrize(
    "source,target",
    [
        (QImage(), QSize(4, 4)),
        (QImage(4, 4, QImage.Format.Format_RGB32), QSize(0, 0)),
        (QImage(4, 4, QImage.Format.Format_RGB32), QSize(-1, 4)),
    ],
)
def test_rgb_scaling_rejects_empty_source_or_destination(source, target):
    from src.ui.backgrounds.video_convert import scale_image

    assert scale_image(source, target).isNull()


@pytest.mark.parametrize("size", [(0, 0), (0, 10), (-1, 10)])
def test_video_conversion_rejects_empty_destination_and_leaves_frame_unmapped(size):
    from src.ui.backgrounds.video_convert import convert_mapped_frame

    frame = QVideoFrame(QVideoFrameFormat(QSize(16, 16), QVideoFrameFormat.PixelFormat.Format_NV12))
    assert convert_mapped_frame(frame, size, "contain") is None
    assert not frame.isMapped()


def test_native_video_conversion_on_worker_returns_owned_pixels(qapp):
    from src.ui.backgrounds.video import convert_owned_frame

    fmt = QVideoFrameFormat(QSize(32, 16), QVideoFrameFormat.PixelFormat.Format_NV12)
    fmt.setColorSpace(QVideoFrameFormat.ColorSpace.ColorSpace_BT709)
    fmt.setColorRange(QVideoFrameFormat.ColorRange.ColorRange_Video)
    source = QVideoFrame(fmt)
    assert source.map(QVideoFrame.MapMode.WriteOnly)
    source.bits(0)[:] = bytes([126]) * source.mappedBytes(0)
    source.bits(1)[:] = bytes([128]) * source.mappedBytes(1)
    source.unmap()
    with ThreadPoolExecutor(max_workers=1) as worker:
        image, error = worker.submit(convert_owned_frame, QVideoFrame(source), (16, 16), "contain").result(5)
    assert not error
    assert image.size() == QSize(16, 8)
    assert source.isValid() and not source.isMapped()
    del source
    gc.collect()
    # 保持缩放器在该缩小比例下的整数舍入结果。
    assert image.pixelColor(8, 4).getRgb() == (126, 126, 126, 255)


def test_dynamic_blur_processes_pixels_and_returns_owned_alpha(qapp):
    from src.ui.backgrounds.blur import BlurFrame

    pixels = (bytes((255, 0, 0, 128)) * 32 + bytes((0, 0, 0, 128)) * 32) * 32
    source = QImage(pixels, 64, 32, 256, QImage.Format.Format_RGBA8888).copy()
    task = BlurFrame(7, source, 100)
    results = QSignalSpy(task.signals.ready)
    task.run()
    assert results.count() == 1
    generation, result = results.at(0)
    assert generation == 7
    assert result.size() == QSize(640, 320)
    color = result.pixelColor(320, 160)
    assert 0 < color.red() < 255
    assert color.green() == color.blue() == 0
    assert color.alpha() == 129  # 双线性放大使用一致的像素舍入规则。
    source.fill(QColor("blue"))
    assert result.pixelColor(320, 160) == color
