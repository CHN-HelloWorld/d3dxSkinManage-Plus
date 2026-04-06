"""有界的分类预设读写，快照暂存与最终发布由调用方负责。

所有函数同步执行，应由调用方的后台线程运行。输入字段不能决定文件系统目标。
写入器独占创建指定文件并读回校验，业务服务再使用不冲突的名称发布。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import struct
from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from pathlib import Path
from typing import BinaryIO

from PIL import Image

from src.core.category_preset import (
    CATEGORY_PRESET_EXTENSION,
    HEADER_SIZE,
    MAGIC,
    MANIFEST_KIND,
    MAX_FILE_BYTES,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    MAX_JSON_DEPTH,
    MAX_MANIFEST_BYTES,
    CategoryPresetCancelled,
    CategoryPresetDocument,
    CategoryPresetError,
    CategoryPresetFileAccessError,
    CategoryPresetFormatError,
    CategoryPresetImage,
    validate_manifest,
)
from src.utils import fs
from src.utils.i18n import t as tr

Cancelled = Callable[[], bool] | None
Progress = Callable[[str, int], None] | None
_HEADER = struct.Struct("<8sIQ32s")
_CHUNK_SIZE = 1024 * 1024
_IMAGE_FORMATS = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "BMP": "image/bmp",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}


def _check_cancelled(cancelled: Cancelled) -> None:
    """调用取消检查器，收到取消请求时以分类预设专用异常终止当前操作。"""
    if cancelled is not None and cancelled():
        raise CategoryPresetCancelled(tr("分类预设操作已取消"))


def _progress(progress: Progress, phase: str, completed: int, total: int) -> None:
    """将已处理数量换算为零到一百的整数百分比，并通知可选的进度回调。"""
    if progress is not None:
        progress(phase, min(100, max(0, completed * 100 // max(1, total))))


def _signature(info: os.stat_result) -> tuple:
    """组合文件标识、大小和时间戳，供读取前后的来源一致性核对使用。"""
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, fs.stat_identity_time_ns(info)


def _open_source(path: Path) -> BinaryIO:
    """读取内容前拒绝目录、链接及其他重解析点。"""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise CategoryPresetError(tr("请选择普通文件，不能使用目录或链接"))
    stream = path.open("rb")
    if _signature(os.fstat(stream.fileno())) != _signature(info):
        stream.close()
        raise CategoryPresetError(tr("来源文件在打开时发生变化，请重新选择"))
    return stream


def _unchanged(stream: BinaryIO, path: Path, original: os.stat_result) -> None:
    """同时核对已打开句柄与路径当前指向的文件，任一身份或内容属性变化时拒绝继续。"""
    expected = _signature(original)
    if _signature(os.fstat(stream.fileno())) != expected or _signature(path.stat()) != expected:
        raise CategoryPresetError(tr("来源文件发生变化，请重新选择"))


def _remove_owned(path: Path, identity: tuple[int, int] | None) -> None:
    """仅删除仍具有本次创建身份的输出文件。"""
    if identity is None:
        return
    try:
        info = path.lstat()
        if (info.st_dev, info.st_ino) == identity:
            path.unlink()
    except FileNotFoundError:
        pass


class _ImageWindow(io.BufferedIOBase):
    """可定位的有界视图，限制 Pillow 只能读取指定图片的数据区。"""

    def __init__(self, stream: BinaryIO, offset: int, length: int, cancelled: Cancelled):
        """借用原始二进制流中的指定区间，建立独立读取位置并保存取消检查器。"""
        self._stream = stream
        self._offset = offset
        self._length = length
        self._position = 0
        self._cancelled = cancelled

    def readable(self):
        """声明图片区间支持读取，供图像解码器查询流能力。"""
        return True

    def seekable(self):
        """声明图片区间支持定位，允许解码器回读文件头或逐帧访问。"""
        return True

    def tell(self):
        """返回相对于图片区间起点的当前位置，不暴露底层流的绝对偏移。"""
        return self._position

    def seek(self, offset, whence=io.SEEK_SET):
        """按图片区间的起点、当前位置或末尾定位；拒绝负位置与无效基准，并在定位前检查取消。"""
        _check_cancelled(self._cancelled)
        position = offset + (self._position if whence == io.SEEK_CUR else self._length if whence == io.SEEK_END else 0)
        if whence not in (io.SEEK_SET, io.SEEK_CUR, io.SEEK_END) or position < 0:
            raise ValueError("Invalid image seek")
        self._position = position
        return position

    def read(self, size=-1):
        """从当前位置读取至多区间内剩余字节，越过区间末尾时返回空数据。"""
        _check_cancelled(self._cancelled)
        remaining = max(0, self._length - self._position)
        amount = remaining if size is None or size < 0 else min(size, remaining)
        self._stream.seek(self._offset + self._position)
        data = self._stream.read(amount)
        self._position += len(data)
        return data

    def readinto(self, buffer):
        """将区间内可读数据复制到调用方缓冲区，并返回实际写入字节数。"""
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)


def _check_image_size(width: int, height: int) -> None:
    """拒绝非正尺寸或超过单张图片像素上限的图像。"""
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise CategoryPresetError(tr("单张图片像素数超过 40,000,000 或尺寸无效"))


def _preflight_gif(window: _ImageWindow) -> None:
    """在 Pillow 为帧分配内存前限制 GIF 画布与各帧尺寸。

    仅遍历头部、调色板和带长度前缀的数据块，单次读取不超过 768 字节。
    像素内容仍交给 Pillow 完整解码校验。"""
    if window.read(6) not in (b"GIF87a", b"GIF89a"):
        return

    def exact(length):
        """读取指定长度的 GIF 数据，数据不足时报告截断而非继续解析。"""
        data = window.read(length)
        if len(data) != length:
            raise CategoryPresetError(tr("GIF 图片数据不完整"))
        return data

    def skip_palette(flags):
        """根据 GIF 标志位跳过存在的颜色表，并验证表数据完整。"""
        if flags & 0x80:
            exact(3 * (1 << ((flags & 7) + 1)))

    def skip_blocks():
        """逐个跳过 GIF 长度前缀数据块，直到零长度结束标记。"""
        while True:
            length = exact(1)[0]
            if not length:
                return
            exact(length)

    width, height, flags, _, _ = struct.unpack("<HHBBB", exact(7))
    _check_image_size(width, height)
    skip_palette(flags)
    has_frame = False
    while True:
        marker = exact(1)
        if marker == b";":
            if not has_frame:
                raise CategoryPresetError(tr("GIF 图片没有有效帧"))
            return
        if marker == b"!":
            exact(1)  # 扩展标签后的数据使用长度前缀。
            skip_blocks()
        elif marker == b",":
            left, top, frame_width, frame_height, flags = struct.unpack("<HHHHB", exact(9))
            _check_image_size(frame_width, frame_height)
            # 分配原生图像内存前，按 Pillow 的规则累计帧偏移和画布范围。

            width = max(width, left + frame_width)
            height = max(height, top + frame_height)
            _check_image_size(width, height)
            skip_palette(flags)
            exact(1)  # 读取 LZW 最小编码尺寸；压缩数据由 Pillow 校验。
            skip_blocks()
            has_frame = True
        else:
            raise CategoryPresetError(tr("GIF 图片包含无效数据块"))


def _decode_image(stream: BinaryIO, offset: int, length: int, cancelled: Cancelled) -> str:
    """校验受支持图片的容器结构并逐帧解码，检查像素上限和取消请求，返回媒体类型。"""

    try:
        _check_cancelled(cancelled)
        _preflight_gif(_ImageWindow(stream, offset, length, cancelled))
        with Image.open(_ImageWindow(stream, offset, length, cancelled)) as image:
            media_type = _IMAGE_FORMATS.get(image.format)
            if media_type is None:
                raise CategoryPresetError(tr("只支持 PNG、JPEG、BMP、GIF 和 WebP 图片"))
            _check_image_size(*image.size)
            image.verify()
        # verify 不会解码 JPEG/GIF 像素，需逐帧解码，
        # 同时避免保存全部帧或修改 Pillow 的全局设置。
        with Image.open(_ImageWindow(stream, offset, length, cancelled)) as image:
            frame = 0
            while True:
                _check_cancelled(cancelled)
                _check_image_size(*image.size)
                image.load()
                frame += 1
                try:
                    image.seek(frame)
                except EOFError:
                    break
        _check_cancelled(cancelled)
        return media_type
    except (CategoryPresetError, CategoryPresetCancelled):
        raise
    except (OSError, ValueError, SyntaxError, struct.error, IndexError, Image.DecompressionBombError) as exc:
        raise CategoryPresetError(tr("目录图片损坏或无法解码：{exc}", exc=exc)) from exc
