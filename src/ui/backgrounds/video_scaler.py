"""按视频独立管理有界的 FFmpeg 分片缩放。

sws_scale_frame 执行分片工作线程；上下文和引用计数缓冲随当前视频转换器创建和释放。
"""

from __future__ import annotations

import ctypes as c
import os
from contextlib import contextmanager
from functools import lru_cache
from threading import local

import shiboken6
from PySide6.QtGui import QImage


class _Frame(c.Structure):
    # 仅声明已验证的公开结构前缀，完整帧结构由 FFmpeg 分配和释放，不假定其总大小。
    """映射 FFmpeg 公共帧结构中缩放所需的前缀字段。"""

    _fields_ = [
        ("data", c.c_void_p * 8),
        ("linesize", c.c_int * 8),
        ("extended_data", c.POINTER(c.c_void_p)),
        ("width", c.c_int),
        ("height", c.c_int),
        ("nb_samples", c.c_int),
        ("format", c.c_int),
    ]


@lru_cache(maxsize=1)
def _frame_api(libraries):
    """为已验证的 FFmpeg 主版本配置帧接口签名，不支持时返回 False。"""
    scale, util = libraries
    try:
        util.avutil_version.argtypes = []
        util.avutil_version.restype = c.c_uint
        scale.swscale_version.argtypes = []
        scale.swscale_version.restype = c.c_uint
        # 未验证的主版本或缺失接口不启用此优化，交给已有通用转换器处理。
        if util.avutil_version() >> 16 != 59 or scale.swscale_version() >> 16 != 8:
            return False
        util.av_frame_alloc.argtypes = []
        util.av_frame_alloc.restype = c.POINTER(_Frame)
        util.av_frame_free.argtypes = [c.POINTER(c.POINTER(_Frame))]
        util.av_frame_free.restype = None
        util.av_frame_get_buffer.argtypes = [c.POINTER(_Frame), c.c_int]
        util.av_frame_get_buffer.restype = c.c_int
        util.av_image_copy.argtypes = [
            c.POINTER(c.c_void_p),
            c.POINTER(c.c_int),
            c.POINTER(c.c_void_p),
            c.POINTER(c.c_int),
            c.c_int,
            c.c_int,
            c.c_int,
        ]
        util.av_image_copy.restype = None
        util.av_opt_set_int.argtypes = [c.c_void_p, c.c_char_p, c.c_int64, c.c_int]
        util.av_opt_set_int.restype = c.c_int
        scale.sws_alloc_context.argtypes = []
        scale.sws_alloc_context.restype = c.c_void_p
        scale.sws_init_context.argtypes = [c.c_void_p] * 3
        scale.sws_init_context.restype = c.c_int
        scale.sws_scale_frame.argtypes = [c.c_void_p, c.POINTER(_Frame), c.POINTER(_Frame)]
        scale.sws_scale_frame.restype = c.c_int
        return True
    except AttributeError:
        return False


class FrameScaler:
    """持有固定格式的 FFmpeg 多线程缩放上下文及对齐的输入输出缓冲。"""

    def __init__(self, libraries, spec, threads):
        """依据输入输出规格创建缩放资源；接口、分配或配置失败时释放已分配资源并抛出异常。"""
        if not _frame_api(libraries):
            raise ValueError("Unsupported FFmpeg frame API")
        self.scale, self.util = libraries
        self.context = self.source = self.destination = None
        self.width, self.height, self.pixel, dw, dh, flags, space, full = spec
        self.output_pixel = self.util.av_get_pix_fmt(b"bgra")
        try:
            self.context = self.scale.sws_alloc_context()
            self.source, self.destination = self.util.av_frame_alloc(), self.util.av_frame_alloc()
            if not self.context or not self.source or not self.destination:
                raise MemoryError("Could not allocate video scaler")
            for name, value in zip(
                (b"srcw", b"srch", b"src_format", b"dstw", b"dsth", b"dst_format", b"sws_flags", b"threads"),
                (self.width, self.height, self.pixel, dw, dh, self.output_pixel, flags, threads),
            ):
                if self.util.av_opt_set_int(self.context, name, value, 0) < 0:
                    raise ValueError("Unsupported video scaler option")
            if self.scale.sws_init_context(self.context, None, None) < 0:
                raise ValueError("Could not initialize video scaler")
            coefficients = self.scale.sws_getCoefficients(space)
            if (
                self.scale.sws_setColorspaceDetails(
                    self.context, coefficients, full, coefficients, 1, 0, 1 << 16, 1 << 16
                )
                < 0
            ):
                raise ValueError("Unsupported video color space")
            for pointer, width, height, pixel in (
                (self.source, self.width, self.height, self.pixel),
                (self.destination, dw, dh, self.output_pixel),
            ):
                frame = pointer.contents
                frame.width, frame.height, frame.format = width, height, pixel
                if self.util.av_frame_get_buffer(pointer, 32) < 0:
                    raise MemoryError("Could not allocate video frame buffers")
        except Exception:
            self.close()
            raise

    def convert(self, source, strides):
        """复制输入平面并执行缩放，返回独立 RGB32 图像；缩放或图像分配失败时返回 None。"""
        frame = self.source.contents
        # 帧接口会保留缓冲引用，将映射像素复制到对齐缓冲，避免借用过期指针。
        self.util.av_image_copy(frame.data, frame.linesize, source, strides, self.pixel, self.width, self.height)
        if self.scale.sws_scale_frame(self.context, self.destination, self.source) < 0:
            return None
        frame = self.destination.contents
        image = QImage(frame.width, frame.height, QImage.Format.Format_RGB32)
        if image.isNull():
            return None
        destination_buffer = image.bits()
        destination = (c.c_void_p * 4)(int(shiboken6.VoidPtr(destination_buffer)), 0, 0, 0)
        dest_strides = (c.c_int * 4)(image.bytesPerLine(), 0, 0, 0)
        # 复制时释放 GIL，返回图像独立拥有像素，不受后续帧或关闭影响。
        self.util.av_image_copy(
            destination, dest_strides, frame.data, frame.linesize, self.output_pixel, frame.width, frame.height
        )
        return image

    def close(self):
        """释放缩放上下文及两个帧缓冲，清空指针以允许重复调用。"""
        if self.context:
            self.scale.sws_freeContext(self.context)
            self.context = None
        for name in ("source", "destination"):
            pointer = getattr(self, name)
            if pointer:
                self.util.av_frame_free(c.byref(pointer))
            setattr(self, name, None)


class _ScalingSession:
    """为当前转换线程缓存一个缩放器及其格式，避免逐帧分配。"""

    def __init__(self):
        """初始化尚未绑定格式和缩放器的转换缓存。"""
        self.key = None
        self.scaler = None

    def convert(self, libraries, spec, source, strides):
        # 小帧无需承担额外复制开销。
        """仅为足够大的帧使用多线程缩放，按规格重建资源并缓存不支持的格式。"""
        threads = min(8, os.cpu_count() or 1)
        if threads < 2 or spec[0] * spec[1] < 1920 * 1080 or spec[3] * spec[4] < 1_000_000:
            return None
        key = (libraries, spec)
        if key != self.key:
            self.close()
            self.key = key
            try:
                self.scaler = FrameScaler(libraries, spec, threads)
            except (MemoryError, ValueError):
                # 按当前格式缓存分配失败，避免逐帧重试不支持的操作。
                return None
        if self.scaler is None:
            return None
        result = self.scaler.convert(source, strides)
        if result is None:
            self.close()
        return result

    def close(self):
        """释放当前缩放器，同时保留规格键以避免重复尝试失败配置。"""
        if self.scaler is not None:
            self.scaler.close()
            self.scaler = None


_sessions = local()


@contextmanager
def scaling_session():
    """可变 FFmpeg 资源只属于当前视频转换线程。"""
    previous = getattr(_sessions, "current", None)
    current = _ScalingSession()
    _sessions.current = current
    try:
        yield
    finally:
        current.close()
        _sessions.current = previous


def scale_video_frame(libraries, spec, source, strides):
    """使用当前线程的缩放上下文转换帧，无上下文或无法使用优化时返回 None。"""
    session = getattr(_sessions, "current", None)
    return session.convert(libraries, spec, source, strides) if session is not None else None
