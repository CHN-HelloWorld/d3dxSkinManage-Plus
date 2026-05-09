"""卡片回收后保留本地预览像素，文件变化时使缓存失效。"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, replace

from PySide6.QtCore import QSize
from PySide6.QtGui import QImage, QPixmap

from src.utils import fs
from src.utils.image_cache_budget import image_cache_budget
from src.utils.image_memory_cache import ImageMemoryCache

# 总览、分类和画廊共用进程预算。
# 预取不能驱逐已查看图像，原图保留缩放分辨率，合成缓存避免重复模糊和裁剪。
_images = ImageMemoryCache(max_bytes=0)
_prefetched_images = ImageMemoryCache(max_bytes=0)
_rendered_images = ImageMemoryCache(max_bytes=0)
image_cache_budget.register(_images, "local_viewed")
image_cache_budget.register(_prefetched_images, "local_prefetch")
image_cache_budget.register(_rendered_images, "local_rendered")


def preview_cache_token() -> tuple[int, int, int]:
    """任务进入等待队列前采集版本。"""
    return _images.generation, _prefetched_images.generation, _rendered_images.generation


def _key(path: str, size: QSize, auto_transform: bool) -> tuple:
    """将规范化文件路径、目标尺寸和自动方向转换选项组合为预览缓存键。"""
    return (os.path.normcase(os.path.abspath(fs.normal_path(path))), size.width(), size.height(), auto_transform)


def _file_stamp(path: str) -> tuple | None:
    """读取文件时间戳、尺寸和文件标识，用于缓存失效校验；无法访问时返回 None。"""
    try:
        stat = os.stat(fs.native_path(path))
        return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino
    except OSError:
        return None


@dataclass(frozen=True)
class PreviewLayout:
    """保存预览的目标宽高、展示模式和设备像素比，作为渲染布局参数。"""

    width: int
    height: int
    mode: str
    dpr: float = 1.0


@dataclass(frozen=True)
class DecodedPreview:
    """保存解码图像、源文件版本与缓存代次，以及可选的预渲染图像和布局。"""

    image: QImage | None
    source_stamp: tuple | None
    cache_token: tuple[int, int, int] | None = None
    rendered: QImage | None = None
    layout: PreviewLayout | None = None

    def is_current(self, path: str) -> bool:
        """即使缓存项已被驱逐，也核对产生解码结果的源版本。"""
        return self.source_stamp == _file_stamp(path)


def _cached_entry(
    path: str,
    size: QSize,
    *,
    auto_transform: bool = False,
    validate: bool = True,
    promote: bool = True,
) -> tuple | None:
    """查找正式或预取预览缓存，并按需校验文件版本及将预取结果提升到正式缓存。

    文件已变化时同时清除两级缓存；写入提升结果时使用代次保护，避免跨越缓存重置。
    """
    generation = _images.generation
    key = _key(path, size, auto_transform)
    entry = _images.get(key)
    prefetched = entry is None
    if prefetched:
        entry = _prefetched_images.get(key)
    if entry is None:
        return None
    stamp, image = entry
    if validate and stamp != _file_stamp(path):
        _images.remove(key)
        _prefetched_images.remove(key)
        return None
    if prefetched and validate and promote:
        _images.put(key, entry, cost_bytes=image.sizeInBytes(), expected_generation=generation)
        _prefetched_images.remove(key)
    return stamp, image


def cached_preview(
    path: str,
    size: QSize,
    *,
    auto_transform: bool = False,
    validate: bool = True,
) -> QImage | None:
    """读取保留像素，可见绑定始终校验源文件。

    纯预取查询不访问元数据或提升缓存级别，实际绑定卡片时再校验。
    """
    entry = _cached_entry(path, size, auto_transform=auto_transform, validate=validate)
    return QImage(entry[1]) if entry is not None else None


def load_preview(
    path: str,
    size: QSize,
    decoder: Callable,
    *,
    auto_transform: bool = False,
    cache_token: tuple[int, int, int] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> DecodedPreview:
    """在工作线程解码和缓存，已完成的不可见请求仍可复用。"""
    token = preview_cache_token() if cache_token is None else cache_token
    entry = _cached_entry(path, size, auto_transform=auto_transform, promote=False)
    if entry is not None:
        return DecodedPreview(QImage(entry[1]), entry[0], token)
    stamp = _file_stamp(path)
    image = decoder(path, size, auto_transform=auto_transform)
    if (
        image is not None
        and not image.isNull()
        and stamp is not None
        and stamp == _file_stamp(path)
        and not (cancelled is not None and cancelled())
    ):
        _prefetched_images.put(
            _key(path, size, auto_transform),
            (stamp, QImage(image)),
            cost_bytes=image.sizeInBytes(),
            expected_generation=token[1],
        )
    # 排队投递期间保留像素对应的源版本，文件可能在解码或通知后改变。
    return DecodedPreview(image, stamp, token)


def retain_viewed_preview(
    path: str,
    size: QSize,
    preview: DecodedPreview | None,
    *,
    auto_transform: bool = False,
) -> bool:
    """匹配当前卡片后保留前景结果，即使禁用了预取存储。

    缓存清空和文件变化仍由解码代次及源版本校验。
    """
    if preview is None or preview.source_stamp is None or preview.image is None or preview.image.isNull():
        return False
    generation = preview.cache_token[0] if preview.cache_token is not None else _images.generation
    if not preview.is_current(path):
        return False
    key = _key(path, size, auto_transform)
    admitted = _images.put(
        key,
        (preview.source_stamp, QImage(preview.image)),
        cost_bytes=preview.image.sizeInBytes(),
        expected_generation=generation,
    )
    if admitted:
        _prefetched_images.remove(key)
    return admitted


def cached_rendered_preview(image: QImage, layout: PreviewLayout) -> QImage | None:
    """滚动查询不能现场合成缺失缩略图。"""
    cached = _rendered_images.get(("prepared", image.cacheKey(), layout))
    return QImage(cached) if cached is not None else None


def prepare_preview(
    preview: DecodedPreview, layout: PreviewLayout, *, cancelled: Callable[[], bool] | None = None
) -> DecodedPreview:
    """后台准备时保留原解码身份和令牌。"""
    if preview.image is None or preview.image.isNull():
        return replace(preview, layout=layout)
    rendered = cached_rendered_preview(preview.image, layout)
    if rendered is None:
        from src.ui.widgets.preview_compositor import compose_preview

        rendered = compose_preview(preview.image, layout.width, layout.height, layout.mode, dpr=layout.dpr)
        if not (cancelled is not None and cancelled()):
            generation = preview.cache_token[2] if preview.cache_token else _rendered_images.generation
            _rendered_images.put(
                ("prepared", preview.image.cacheKey(), layout), rendered, expected_generation=generation
            )
    return replace(preview, rendered=rendered, layout=layout)


def rendered_preview(
    image: QImage,
    width: int,
    height: int,
    mode: str,
    compositor: Callable,
    *,
    cache_generation: int | None = None,
) -> QPixmap:
    """界面合成缓存使用不可变像素与布局作为键。"""
    key = image.cacheKey(), width, height, mode
    generation = _rendered_images.generation if cache_generation is None else cache_generation
    cached = _rendered_images.get(key)
    if cached is not None:
        return QPixmap.fromImage(cached)
    pixmap = compositor(QPixmap.fromImage(image), width, height, radius=8)
    _rendered_images.put(key, pixmap.toImage(), expected_generation=generation)
    return pixmap
