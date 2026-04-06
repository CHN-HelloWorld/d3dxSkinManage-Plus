"""压缩导入和导出共用的硬件并发配置。"""

from __future__ import annotations

from src.core.deploy_concurrency import recommended_deploy_concurrency

# 每个作业独立分配压缩缓冲区，并竞争磁盘读写资源。
MAX_ARCHIVE_WORKERS = 16


def recommended_archive_workers() -> int:
    """根据处理器、内存和磁盘竞争限制并发压缩作业。"""
    return recommended_deploy_concurrency()


def effective_archive_workers(configured: object) -> int:
    """零表示自动选择，正整数按允许范围作为手动并发数。"""
    if type(configured) is int and 1 <= configured <= MAX_ARCHIVE_WORKERS:
        return configured
    return recommended_archive_workers()
