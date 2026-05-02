"""独立视频合成器使用一个带确认机制的共享像素缓冲。

复制完成后发布描述符，读取方取得图像所有权后确认，写入方再复用缓冲，避免全屏帧队列。
"""

from __future__ import annotations

import ctypes
import math
import uuid

import shiboken6
from PySide6.QtCore import QSharedMemory
from PySide6.QtGui import QImage

_MAX_IMAGE_BYTES = 256 * 1024 * 1024


def _geometry(width, height, stride, ratio):
    """校验共享像素缓冲的整数尺寸、紧凑行跨度及有效像素比，非法值抛出 ValueError。"""
    if (
        any(type(value) is not int for value in (width, height, stride))
        or not 0 < width <= 16384
        or not 0 < height <= 16384
        or stride != width * 4
        or stride * height > _MAX_IMAGE_BYTES
        or not isinstance(ratio, (int, float))
        or not math.isfinite(ratio)
        or not 0 < ratio <= 16
    ):
        raise ValueError("Invalid shared foreground geometry")


class SharedImageWriter:
    """复用单块共享内存写入前景图像，缓冲复用时机由外部确认协议保证。"""

    def __init__(self):
        """初始化尚未分配的共享像素缓冲。"""
        self._memory = None

    def write(self, image: QImage) -> dict:
        """将图像复制到可复用共享内存并返回读取描述符；扩容或复制失败向调用方传播。"""
        image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        width, height, stride = image.width(), image.height(), image.bytesPerLine()
        ratio = image.devicePixelRatio()
        _geometry(width, height, stride, ratio)
        length = stride * height
        if self._memory is None or self._memory.size() < length:
            memory = QSharedMemory(f"d3dx-foreground-{uuid.uuid4().hex}")
            if not memory.create(length):
                raise OSError(memory.errorString())
            self.close()
            self._memory = memory
        destination, source = self._memory.data(), image.constBits()
        # 释放 GIL 复制期间保留内存视图及其 Qt 所有者。
        ctypes.memmove(int(shiboken6.VoidPtr(destination)), int(shiboken6.VoidPtr(source)), length)
        return dict(key=self._memory.key(), width=width, height=height, stride=stride, dpr=ratio)

    def close(self):
        """分离当前共享内存并清除引用，可重复调用。"""
        if self._memory is not None:
            self._memory.detach()
            self._memory = None


class SharedImageReader:
    """以只读方式挂接共享图像缓冲，并复制像素以脱离写入方的生命周期。"""

    def __init__(self):
        """初始化尚未挂接共享内存的读取器。"""
        self._memory = None

    def read(self, descriptor: dict) -> QImage:
        """验证描述符并按需切换共享内存，返回独立图像；非法描述符或挂接失败时抛出异常。"""
        width, height = descriptor.get("width"), descriptor.get("height")
        stride, ratio = descriptor.get("stride"), descriptor.get("dpr")
        _geometry(width, height, stride, ratio)
        key = descriptor.get("key")
        if not isinstance(key, str) or not key.startswith("d3dx-foreground-"):
            raise ValueError("Invalid shared foreground key")
        if self._memory is None or self._memory.key() != key:
            memory = QSharedMemory(key)
            if not memory.attach(QSharedMemory.AccessMode.ReadOnly):
                raise OSError(memory.errorString())
            self.close()
            self._memory = memory
        if stride * height > self._memory.size():
            raise ValueError("Shared foreground exceeds its allocation")
        image = QImage(
            self._memory.constData(), width, height, stride, QImage.Format.Format_ARGB32_Premultiplied
        ).copy()
        image.setDevicePixelRatio(ratio)
        return image

    def close(self):
        """分离已挂接的共享内存并清除引用，可重复调用。"""
        if self._memory is not None:
            self._memory.detach()
            self._memory = None
