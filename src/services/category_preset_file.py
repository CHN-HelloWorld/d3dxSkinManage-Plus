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
