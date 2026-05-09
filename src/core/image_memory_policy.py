"""图像缓存偏好及基于显示尺寸和物理内存的容量建议。"""

from __future__ import annotations

import ctypes
import math
import os
from functools import lru_cache

from src.core.deploy_concurrency import _total_memory_bytes

MIB = 1024**2
MIN_IMAGE_CACHE_MB = 64
MAX_IMAGE_CACHE_MB = 8192
DEFAULT_MANUAL_IMAGE_CACHE_MB = 768


@lru_cache(maxsize=1)
def physical_memory_bytes() -> int | None:
    """自动选择使用缓存的物理内存总量，不随瞬时可用内存波动。"""
    return _total_memory_bytes()


def available_memory() -> tuple[int | None, int | None]:
    """在 Windows 上使用一次系统调用；探测失败不视为内存不足。"""
    try:
        if os.name != "nt":
            return physical_memory_bytes(), os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")

        class MemoryStatus(ctypes.Structure):
            """映射 Windows 内存状态结构，用于读取物理内存总量及可用量。"""

            _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [
                (name, ctypes.c_uint64)
                for name in (
                    "total",
                    "available",
                    "total_page",
                    "available_page",
                    "total_virtual",
                    "available_virtual",
                    "available_extended",
                )
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        query = ctypes.WinDLL("kernel32", use_last_error=True).GlobalMemoryStatusEx
        query.argtypes = [ctypes.POINTER(MemoryStatus)]
        query.restype = ctypes.c_int
        if query(ctypes.byref(status)):
            return int(status.total), int(status.available)
    except (AttributeError, OSError, ValueError):
        pass
    return None, None


def recommend_image_cache_mb(
    total_bytes: int | None,
    *,
    width: int = 1920,
    height: int = 1080,
    dpr: float = 1.0,
    visible_cards: int | None = None,
    preview_width: int = 640,
    preview_height: int = 480,
) -> int:
    """估计保留图像的容量，最多使用物理内存的三十二分之一且不超过 2 GiB。

    按三屏 32 位预览图与两个物理屏幕图层估算近期图像和合成占用。
    卡片尺寸包含总览缓冲行，该估算用于工作集预算，不表示所有设备的最优容量。
    """
    if type(total_bytes) is not int or total_bytes <= 0:
        return 256
    width, height = max(1, width), max(1, height)
    dpr = dpr if math.isfinite(dpr) and dpr > 0 else 1.0
    if visible_cards is None or visible_cards <= 0:
        columns = max(1, (width - 320) // 204)
        visible_cards = columns * (math.ceil(max(1, height - 140) / 228) + 2)
    history = visible_cards * max(1, preview_width) * max(1, preview_height) * 4 * 3
    composed = width * height * dpr**2 * 4 * 2
    working_mb = max(256, math.ceil((history + composed) / MIB / 64) * 64)
    ram_ceiling_mb = max(MIN_IMAGE_CACHE_MB, total_bytes // 32 // MIB // 64 * 64)
    return min(2048, ram_ceiling_mb, working_mb)


def effective_image_cache_mb(mode: object, manual_mb: object, recommended_mb: int) -> int:
    """选择手动或推荐缓存容量；手动值类型或范围无效时采用默认手动容量。"""
    if mode == "manual":
        if type(manual_mb) is int and MIN_IMAGE_CACHE_MB <= manual_mb <= MAX_IMAGE_CACHE_MB:
            return manual_mb
        return DEFAULT_MANUAL_IMAGE_CACHE_MB
    return recommended_mb
