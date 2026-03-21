"""GameBanana apiv11 客户端"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.services.gamebanana.cache import GbApiCache
from src.services.gamebanana.constants import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_USER_AGENT_TEMPLATE,
)
from src.services.gamebanana.models import (
    GbCategory,
    GbModel,
    GbModFile,
    GbModProfile,
    GbModRecord,
    GbModUpdateEntry,
)
from src.utils.i18n import t as tr
from src.utils.logger import logger

_DEPRECATION_HEADERS = ("deprecation", "sunset", "x-deprecated")

# 将界面排序键映射为 GameBanana apiv11 的 _sSort 参数。
# 未知值、default 和 best_match 均省略该参数，
# 由服务端使用默认排序。
_SORT_ALIASES: dict[str, str] = {
    "latest": "Generic_LatestModified",
    "newest": "Generic_Newest",
    "most_downloaded": "Generic_MostDownloaded",
    "most_viewed": "Generic_MostViewed",
    "most_liked": "Generic_MostLiked",
}


class UpdateCheckError(RuntimeError):
    """批量检查失败；保留已取得的文件，未完成部分不能展开成逐项请求。"""

    def __init__(self, message: str, *, partial_files: dict[int, list[GbModFile]] | None = None) -> None:
        """保存更新检查失败原因，并携带已成功获取的部分文件列表供调用方继续处理。"""
        super().__init__(message)
        self.partial_files = partial_files or {}


class UpdateCheckRateLimited(UpdateCheckError):
    """更新检查命中 429；手动检查也须遵守服务端的静默期。"""

    def __init__(self, seconds: float = 120.0) -> None:
        """将剩余冷却时间向上取整后写入本地化的更新限流异常。"""
        super().__init__(tr("GameBanana 请求过于频繁（429），请在 {value} 秒后重试更新检测", value=math.ceil(seconds)))


def _positive_int(value: Any) -> int | None:
    """只接受可转换为正整数的整数或字符串，拒绝布尔值、小数及非正数。"""
    # 外部响应的缺项、布尔值、小数等均不能变成用于「已是最新」判定的时间戳。
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number > 0 else None


class GameBananaClient:
    """封装在线浏览与更新查询，统一协调连接、请求节流、缓存及限流冷却。"""

    BASE = "https://gamebanana.com/apiv11"
    _UPDATE_FILES_BATCH = 50
    _UPDATE_RATE_LIMIT_COOLDOWN = 120.0

    def __init__(
        self,
        *,
        max_concurrency: int = 8,
        request_interval_ms: int = 200,
        proxy: str | None = None,
        user_agent: str | None = None,
        version: str = "0.1.0",
        cache: GbApiCache | None = None,
    ) -> None:
        """创建异步 HTTP 连接和并发配额，配置请求间隔、代理、缓存及更新检查冷却状态。"""
        ua = user_agent or DEFAULT_USER_AGENT_TEMPLATE.format(ver=version)
        self._ua = ua
        self._proxy = proxy
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=6, read=30, write=30, pool=30),
            headers={"User-Agent": ua, "Accept": "application/json"},
            proxy=proxy,
            follow_redirects=True,
        )
        self._sem = asyncio.Semaphore(max_concurrency)
        self._interval = request_interval_ms / 1000.0
        self._request_start_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._update_files_lock = asyncio.Lock()
        self._cache = cache or GbApiCache()
        self._deprecation_msg: str | None = None
        self._update_blocked_until = 0.0

    async def aclose(self) -> None:
        """异步关闭客户端拥有的 HTTP 连接池。"""
        await self._client.aclose()

    def invalidate_game_caches(self, game_id: int) -> int:
        """清除游戏分类及详情、更新缓存，返回清除数量。

        分类缓存按游戏编号精确失效；详情和更新缓存的键不含游戏编号，
        需清除相应前缀，再由后续请求按需获取。
        """
        n = self._cache.invalidate(f"cat:{game_id}")
        n += self._cache.invalidate_prefix("profile:")
        n += self._cache.invalidate_prefix("updates:")
        n += self._cache.invalidate_prefix("update_files:")
        return n

    def invalidate_mod_cache(self, model: GbModel, mod_id: int) -> int:
        """作废单个 Mod 的详情、更新记录和更新文件缓存。"""
        n = self._cache.invalidate(f"profile:{model.value}:{mod_id}")
        n += self._cache.invalidate(f"updates:{model.value}:{mod_id}")
        n += self._cache.invalidate(f"update_files:{model.value}:{mod_id}")
        return n
