"""使用 Qt 随附 FFmpeg 库加速普通动态范围视频转换。

原生转换释放 GIL，仅调用公开 C 接口，不依赖 Qt 私有对象布局。
"""

from __future__ import annotations

import ctypes as c
import sys
from functools import lru_cache
from pathlib import Path

import shiboken6
from PySide6.QtCore import QLibraryInfo, QSize, Qt
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat

from src.ui.backgrounds.video_frame import map_video_frame, unmap_video_frame
from src.ui.backgrounds.video_scaler import scale_video_frame
from src.utils.paths import is_frozen


@lru_cache(maxsize=1)
def _libraries():
    """查找并缓存 Qt 随附的 FFmpeg 动态库，配置公开转换接口的 C 参数类型。

    非 Windows 平台或缺少可用库时返回 None，供调用方退回 Qt 图像转换。
    """
    if sys.platform != "win32":
        return None
    from PySide6 import QtCore

    directories = [Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.BinariesPath)), Path(QtCore.__file__).parent]
    if is_frozen():
        directories.append(Path(sys.executable).parent)
    for directory in directories:
        scales = sorted(directory.glob("swscale-*.dll"), reverse=True)
        utils = sorted(directory.glob("avutil-*.dll"), reverse=True)
        if not scales or not utils:
            continue
        try:
            scale, util = c.CDLL(str(scales[0])), c.CDLL(str(utils[0]))
            util.av_get_pix_fmt.argtypes = [c.c_char_p]
            util.av_get_pix_fmt.restype = c.c_int
            scale.sws_getContext.argtypes = [c.c_int] * 7 + [c.c_void_p] * 3
            scale.sws_getContext.restype = c.c_void_p
            scale.sws_freeContext.argtypes = [c.c_void_p]
            scale.sws_freeContext.restype = None
            scale.sws_getCoefficients.argtypes = [c.c_int]
            scale.sws_getCoefficients.restype = c.POINTER(c.c_int)
            scale.sws_setColorspaceDetails.argtypes = [c.c_void_p, c.POINTER(c.c_int), c.c_int, c.POINTER(c.c_int)] + [
                c.c_int
            ] * 4
            scale.sws_setColorspaceDetails.restype = c.c_int
            scale.sws_scale.argtypes = [
                c.c_void_p,
                c.POINTER(c.c_void_p),
                c.POINTER(c.c_int),
                c.c_int,
                c.c_int,
                c.POINTER(c.c_void_p),
                c.POINTER(c.c_int),
            ]
            scale.sws_scale.restype = c.c_int
            return scale, util
        except (OSError, AttributeError):
            continue
    return None


def convert_mapped_frame(frame, size, fit):
    """返回缩放后的独立 RGB 图像，无法处理时交给 Qt 通用转换。"""
    if not frame.isValid():
        return None
    fmt = frame.surfaceFormat()
    pixel = fmt.pixelFormat()
    formats = QVideoFrameFormat.PixelFormat
    names = {formats.Format_NV12: b"nv12", formats.Format_YUV420P: b"yuv420p", formats.Format_YUV422P: b"yuv422p"}
    name = names.get(pixel)
    # 特殊格式、高动态范围及表面变换由 Qt 处理。
    if (
        name is None
        or fmt.rotation().value != 0
        or fmt.isMirrored()
        or fmt.scanLineDirection() != QVideoFrameFormat.Direction.TopToBottom
        or fmt.colorTransfer()
        in (QVideoFrameFormat.ColorTransfer.ColorTransfer_ST2084, QVideoFrameFormat.ColorTransfer.ColorTransfer_STD_B67)
    ):
        return None
    libraries = _libraries()
    if libraries is None:
        return None
    scale, util = libraries
    aspect = Qt.AspectRatioMode.KeepAspectRatio if fit == "contain" else Qt.AspectRatioMode.KeepAspectRatioByExpanding
    target = fmt.frameSize().scaled(QSize(*size), aspect)
    if target.isEmpty():
        return None
    source_format, dest_format = util.av_get_pix_fmt(name), util.av_get_pix_fmt(b"bgra")
    # 缩小时使用区域平均采样，减少高分辨率插值开销，并保留设备像素尺寸。
    flags = 32 if target.width() < frame.width() and target.height() < frame.height() else 4
    context = None
    mapped = False
    try:
        color_space = fmt.colorSpace()
        spaces = QVideoFrameFormat.ColorSpace
        space = {spaces.ColorSpace_BT709: 1, spaces.ColorSpace_BT2020: 9}.get(color_space, 5)
        full = int(fmt.colorRange() == QVideoFrameFormat.ColorRange.ColorRange_Full)
        mapped = map_video_frame(frame, QVideoFrame.MapMode.ReadOnly)
        if not mapped:
            return None
        expected_planes = 2 if name == b"nv12" else 3
        if frame.planeCount() != expected_planes:
            return None
        source, strides = (c.c_void_p * 4)(), (c.c_int * 4)()
        # 原生工作完成前保留全部内存视图和已映射帧，包括只读缓冲。
        source_buffers = []
        for plane in range(frame.planeCount()):
            buffer = frame.bits(plane)
            stride = frame.bytesPerLine(plane)
            plane_height = frame.height()
            if plane and name in (b"nv12", b"yuv420p"):
                plane_height = (plane_height + 1) // 2
            if (
                buffer is None
                or stride <= 0
                or frame.mappedBytes(plane) < stride * plane_height
                or buffer.nbytes < stride * plane_height
            ):
                return None
            source_buffers.append(buffer)
            source[plane], strides[plane] = int(shiboken6.VoidPtr(buffer)), stride
        spec = (frame.width(), frame.height(), source_format, target.width(), target.height(), flags, space, full)
        image = scale_video_frame(libraries, spec, source, strides)
        if image is not None:
            return image
        context = scale.sws_getContext(
            frame.width(),
            frame.height(),
            source_format,
            target.width(),
            target.height(),
            dest_format,
            flags,
            None,
            None,
            None,
        )
        if not context:
            return None
        coefficients = scale.sws_getCoefficients(space)
        if scale.sws_setColorspaceDetails(context, coefficients, full, coefficients, 1, 0, 1 << 16, 1 << 16) < 0:
            return None
        image = QImage(target, QImage.Format.Format_RGB32)
        if image.isNull():
            return None
        destination_buffer = image.bits()
        destination = (c.c_void_p * 4)(int(shiboken6.VoidPtr(destination_buffer)), 0, 0, 0)
        dest_strides = (c.c_int * 4)(image.bytesPerLine(), 0, 0, 0)
        rows = scale.sws_scale(context, source, strides, 0, frame.height(), destination, dest_strides)
        return image if rows == target.height() else None
    finally:
        if mapped:
            unmap_video_frame(frame)
        if context:
            scale.sws_freeContext(context)


def scale_image(image: QImage, target: QSize) -> QImage:
    """缩放常用 RGB 捕获缓冲，避免完整的 Python 或 Pillow 像素复制。"""
    if image.size() == target:
        return image
    if image.isNull() or target.isEmpty():
        return QImage()
    formats = {
        QImage.Format.Format_RGB32: b"bgra",
        QImage.Format.Format_ARGB32: b"bgra",
        QImage.Format.Format_RGBA8888: b"rgba",
    }
    name = formats.get(image.format())
    libraries = _libraries() if name else None
    if libraries is None:
        return image.scaled(target, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    scale, util = libraries
    flags = 32 if target.width() < image.width() else 2  # 缩小采用区域采样，放大采用双线性插值。
    context = scale.sws_getContext(
        image.width(),
        image.height(),
        util.av_get_pix_fmt(name),
        target.width(),
        target.height(),
        util.av_get_pix_fmt(b"bgra"),
        flags,
        None,
        None,
        None,
    )
    if not context:
        return image.scaled(target, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    try:
        fmt = QImage.Format.Format_ARGB32 if image.hasAlphaChannel() else QImage.Format.Format_RGB32
        result = QImage(target, fmt)
        if result.isNull():
            return result
        source_buffer, destination_buffer = image.constBits(), result.bits()
        source = (c.c_void_p * 4)(int(shiboken6.VoidPtr(source_buffer)), 0, 0, 0)
        destination = (c.c_void_p * 4)(int(shiboken6.VoidPtr(destination_buffer)), 0, 0, 0)
        source_strides = (c.c_int * 4)(image.bytesPerLine(), 0, 0, 0)
        dest_strides = (c.c_int * 4)(result.bytesPerLine(), 0, 0, 0)
        rows = scale.sws_scale(context, source, source_strides, 0, image.height(), destination, dest_strides)
        if rows == target.height():
            return result
        return image.scaled(target, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    finally:
        scale.sws_freeContext(context)
