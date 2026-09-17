"""Mod 更新的下载并发配置。"""

from __future__ import annotations

from src.core.deploy_concurrency import recommended_deploy_concurrency

# 手动设置同样限制最大并发传输数。
MAX_UPDATE_CONCURRENCY = 16


def recommended_update_concurrency() -> int:
    """根据处理器和内存自动分配最多四个更新下载。

    手动上限独立配置，后续导入使用单独的工作线程设置。
    """
    return min(4, recommended_deploy_concurrency())


def effective_update_concurrency(configured: object) -> int:
    """解析自动或无效设置，优先使用范围内的手动值。"""
    if type(configured) is int and 1 <= configured <= MAX_UPDATE_CONCURRENCY:
        return configured
    return recommended_update_concurrency()
