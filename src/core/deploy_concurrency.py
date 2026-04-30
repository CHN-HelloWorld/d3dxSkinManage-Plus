"""共用部署并发配置与轻量硬件建议值。"""

from __future__ import annotations

import ctypes
import os
from functools import lru_cache

# 手动并发允许高于建议值，但限制磁盘竞争和线程数量；零表示自动选择。
MAX_DEPLOY_CONCURRENCY = 16


def _total_memory_bytes() -> int | None:
    """通过系统接口读取物理内存，不创建子进程或执行磁盘探测。"""
    try:
        if os.name == "nt":

            class MemoryStatus(ctypes.Structure):
                """映射 Windows 内存状态结构，供自动部署并发策略读取物理内存容量。"""

                _fields_ = [
                    ("length", ctypes.c_uint32),
                    ("memory_load", ctypes.c_uint32),
                    ("total_phys", ctypes.c_uint64),
                    ("avail_phys", ctypes.c_uint64),
                    ("total_page_file", ctypes.c_uint64),
                    ("avail_page_file", ctypes.c_uint64),
                    ("total_virtual", ctypes.c_uint64),
                    ("avail_virtual", ctypes.c_uint64),
                    ("avail_extended_virtual", ctypes.c_uint64),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            query = ctypes.WinDLL("kernel32", use_last_error=True).GlobalMemoryStatusEx
            query.argtypes = [ctypes.POINTER(MemoryStatus)]
            query.restype = ctypes.c_int
            if not query(ctypes.byref(status)):
                return None
            total = int(status.total_phys)
        else:
            total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        return total if total > 0 else None
    except (AttributeError, OSError, ValueError):
        return None


@lru_cache(maxsize=1)
def recommended_deploy_concurrency() -> int:
    """为界面保留处理器容量，每个部署作业按 4 GiB 内存预算。

    自动并发限制在 1 至 8 个工作线程，并在进程内缓存建议值，
    避免重复探测硬件或随瞬时内存占用改变默认配置。
    """
    try:
        cpus = os.cpu_count() or 2
    except (OSError, NotImplementedError):
        cpus = 2
    cpu_limit = max(1, cpus // 2)
    memory = _total_memory_bytes()
    memory_limit = max(1, memory // (4 * 1024**3)) if memory else 2
    return min(8, cpu_limit, memory_limit)


def effective_deploy_concurrency(configured: object) -> int:
    """解析自动或无效配置，并保留范围内的手动选择。"""
    if type(configured) is int and 1 <= configured <= MAX_DEPLOY_CONCURRENCY:
        return configured
    return recommended_deploy_concurrency()
