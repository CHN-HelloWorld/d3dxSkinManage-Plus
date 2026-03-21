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

    @property
    def proxy(self) -> str | None:
        """返回创建请求连接时配置的代理地址，未设置时返回空值。"""
        return self._proxy

    async def update_proxy(self, proxy: str | None) -> None:
        """关闭现有 HTTP 客户端并按新代理配置重新创建。

        在客户端所属的事件循环调用。连接替换可能中断在途请求，调用方须处理资源关闭异常。
        """
        if proxy == self._proxy:
            return
        old = self._client
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=6, read=30, write=30, pool=30),
            headers={"User-Agent": self._ua, "Accept": "application/json"},
            proxy=proxy,
            follow_redirects=True,
        )
        self._proxy = proxy
        try:
            await old.aclose()
        except Exception:
            pass

    def _ensure_update_requests_allowed(self) -> None:
        """共享更新冷却期尚未结束时抛出限流异常，阻止继续发送更新查询。"""
        remaining = self._update_blocked_until - time.monotonic()
        if remaining > 0:
            raise UpdateCheckRateLimited(remaining)

    def _trip_update_rate_limit(self, response: httpx.Response) -> None:
        """根据 Retry-After 秒数或日期延长更新冷却期，并保留至少默认时长的限制。"""
        cooldown = self._UPDATE_RATE_LIMIT_COOLDOWN
        retry_after = response.headers.get("Retry-After", "")
        try:
            seconds = float(retry_after)
        except ValueError:
            try:
                date = parsedate_to_datetime(retry_after)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                seconds = date.timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                seconds = 0.0
        if math.isfinite(seconds):
            cooldown = max(cooldown, seconds)
        self._update_blocked_until = max(self._update_blocked_until, time.monotonic() + cooldown)
        logger.warning("GameBanana 请求被限流（429），更新检测静默 %.0f 秒", cooldown)

    async def _get(
        self,
        path: str,
        *,
        update_check: bool = False,
        **params: Any,
    ) -> Any:
        """执行带并发限额、节流与重试的 JSON 请求；代理连接故障时尝试直连，并记录接口弃用提示。"""
        url = f"{self.BASE}{path}"

        async def call(client: httpx.AsyncClient) -> httpx.Response:
            """在并发配额内安排请求开始时间，发送前复查更新冷却期，并记录新的限流响应。"""
            async with self._sem:
                if update_check:
                    self._ensure_update_requests_allowed()
                if self._interval > 0:
                    # 统一排队分配请求开始时间，避免独立休眠结束后同时发送请求。
                    async with self._request_start_lock:
                        delay = self._next_request_at - time.monotonic()
                        if delay > 0:
                            await asyncio.sleep(delay)
                        self._next_request_at = time.monotonic() + self._interval
                if update_check:
                    # 排队期间其他请求也可能收到 429，发送前再次检查共享静默期。
                    self._ensure_update_requests_allowed()
                resp = await client.get(url, params=params)
                if resp.status_code == 429:
                    # 更新检查可能等待商店发起的共享请求；共享请求收到限流响应后，
                    # 更新检查也须遵守静默期。
                    self._trip_update_rate_limit(resp)
                    if update_check:
                        self._ensure_update_requests_allowed()
                return resp

        def _retry_p(exc: BaseException) -> bool:
            """仅将限流、服务端错误、网络错误及超时判为可重试，其他异常直接传播。"""
            if isinstance(exc, httpx.HTTPStatusError):
                code = exc.response.status_code
                return code == 429 or code >= 500
            return isinstance(exc, (httpx.NetworkError, httpx.TimeoutException))

        async def _attempt(client: httpx.AsyncClient) -> httpx.Response:
            """对给定连接执行至多两次带退避的请求尝试，并将失败状态转换为 HTTP 异常。"""
            r: httpx.Response | None = None
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(2),
                wait=wait_exponential(multiplier=1, min=1, max=4),
                retry=retry_if_exception(_retry_p),
                reraise=True,
            ):
                with attempt:
                    r = await call(client)
                    r.raise_for_status()
            assert r is not None
            return r

        try:
            resp = await _attempt(self._client)
        except (httpx.NetworkError, httpx.TimeoutException) as e:
            # 代理连接失败或超时时尝试一次直连；未配置代理时传播原异常。
            if not self._proxy:
                raise
            logger.warning("GameBanana 经代理失败(%s)，降级直连重试", e)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=6, read=30, write=30, pool=30),
                headers={"User-Agent": self._ua, "Accept": "application/json"},
                proxy=None,
                follow_redirects=True,
            ) as direct:
                resp = await _attempt(direct)

        for h in _DEPRECATION_HEADERS:
            if h in resp.headers:
                self._deprecation_msg = f"GameBanana API marked {h}: {resp.headers[h]}"
                logger.warning(self._deprecation_msg)
                break

        try:
            return resp.json()
        except ValueError as exc:
            raise httpx.HTTPError(f"Invalid JSON from {url}") from exc

    @staticmethod
    def _extract_records(payload: Any) -> list[dict]:
        """提取列表响应或记录包装对象中的条目，其余响应按空结果处理。"""
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            return payload.get("_aRecords") or []
        return []

    @staticmethod
    def _extract_has_more(payload: Any) -> bool:
        """优先读取服务端完成标记，否则按页码和记录总数判断是否还有下一页。"""
        if isinstance(payload, dict):
            meta = payload.get("_aMetadata") or {}
            if "_bIsComplete" in meta:
                return not bool(meta["_bIsComplete"])
            total = meta.get("_nRecordCount")
            per_page = meta.get("_nPerpage") or DEFAULT_PAGE_SIZE
            page = meta.get("_nPage") or 1
            if total is not None:
                return page * per_page < int(total)
        return False
