"""在一个异步会话中顺序下载、恢复和校验更新包，所有请求结束后才切换写入者。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from time import monotonic

import httpx

from src.services.app_update.download_sources import (
    DownloadPolicy,
    DownloadRoute,
    RouteUnavailable,
    SlowDownloadMonitor,
    download_client,
    probe_routes,
)
from src.services.app_update.download_state import (
    DownloadCache,
    DownloadError,
    InvalidResponse,
    UpdateArtifact,
    response_range,
)
from src.utils.i18n import t as tr
from src.utils.logger import logger


class _SlowSource(DownloadError):
    """当前源持续低速，已关闭写入请求，可以保留分片换源。"""


class DownloadSession:
    """独占一个附件的分片，统一处理 HTTP 重试、跨源续传与完成后的发布。"""

    def __init__(
        self,
        artifact: UpdateArtifact,
        cache_dir: Path,
        routes: Sequence[DownloadRoute],
        *,
        automatic: bool = True,
        policy: DownloadPolicy | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        on_source: Callable[[DownloadRoute, int], None] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        """固定目标和候选线路，回调只在所属工作线程执行，由宿主转发至界面。"""
        self.artifact = artifact
        self.cache = DownloadCache(Path(cache_dir), artifact)
        self.routes = list(routes)
        self.automatic = automatic
        self.policy = policy or DownloadPolicy()
        self.on_progress = on_progress
        self.on_source = on_source
        self.on_status = on_status
        self._monitor = SlowDownloadMonitor(self.policy)
        self._received = 0
        self._downloaded = 0
        self._last_progress_time = 0.0

    def _progress(self, downloaded: int, *, force: bool = False) -> None:
        """合并高频小块进度，监控使用即时计数，界面至多每 0.1 秒接收一次更新。"""
        self._downloaded = downloaded
        now = monotonic()
        if self.on_progress and (force or downloaded == self.artifact.size or now - self._last_progress_time >= 0.1):
            self._last_progress_time = now
            self.on_progress(downloaded, self.artifact.size)

    def _source(self, route: DownloadRoute, offset: int) -> None:
        """线路开始或全量重下时重建界面速度基线，已有分片不计为新增流量。"""
        if self.on_source:
            self.on_source(route, offset)
        self._progress(offset, force=True)

    def _status(self, message: str) -> None:
        """向宿主报告测速、恢复和校验等阶段。"""
        if self.on_status:
            self.on_status(message)

    async def run(self) -> Path:
        """独占缓存完成当前下载；无论取消或最终失败，关闭请求后都清理未完成分片。"""
        with self.cache.locked():
            try:
                self.cache.prepare()
                return await self._download()
            finally:
                self.cache.reset()

    async def _download(self) -> Path:
        """仅在当前会话内保留连续前缀，支持网络重试和低速换源时的续传。"""
        self._status(tr("正在校验更新包…"))
        if await self.cache.verify(self.cache.final_path):
            self._progress(self.artifact.size)
            return self.cache.final_path
        self.cache.final_path.unlink(missing_ok=True)
        routes = self.routes
        if self.automatic and len(routes) > 1:
            self._status(tr("正在选择下载线路…"))
            probes = await probe_routes(routes, self.artifact, self.policy)
            routes = [probe.route for probe in probes]
        slow_routes = []
        for route in routes:
            try:
                result = await self._try_route(route, allow_switch=self.automatic and len(routes) > 1)
            except _SlowSource:
                slow_routes.append((route, self._monitor.speed))
                self._status(tr("当前线路持续低速，正在切换下载线路…"))
                continue
            if result is not None:
                return result
        # 每源只因低速淘汰一次；全部线路都慢时按实测速度继续，避免无限切换。
        for route, _speed in sorted(slow_routes, key=lambda item: item[1], reverse=True):
            self._status(tr("可用线路速度较慢，正在继续下载…"))
            result = await self._try_route(route, allow_switch=False)
            if result is not None:
                return result
        raise DownloadError(tr("所有下载源均无法获取完整更新包，请检查网络或稍后重试。"))

    async def _publish_complete_part(self) -> Path | None:
        """分片达到完整大小时先校验，避免末尾连接错误造成越界 Range 或丢弃正确文件。"""
        if self.cache.offset() != self.artifact.size:
            return None
        self._status(tr("正在校验更新包…"))
        if await self.cache.verify(self.cache.part_path):
            self._progress(self.artifact.size)
            return self.cache.publish()
        self.cache.reset()
        return None

    async def _try_route(self, route: DownloadRoute, *, allow_switch: bool) -> Path | None:
        """限制每条线路的网络重试和完整重下次数，失败后保留可恢复的连续前缀。"""
        retries = 0
        clean_retry = False
        while True:
            try:
                await self._watch_transfer(route, allow_switch=allow_switch)
                self._status(tr("正在校验更新包…"))
                if not await self.cache.verify(self.cache.part_path):
                    raise InvalidResponse(tr("更新包 SHA-256 校验失败。"))
                return self.cache.publish()
            except InvalidResponse as exc:
                logger.warning("程序更新：下载响应或校验失败（%s）：%s", route.name, exc)
                self.cache.reset()
                if clean_retry:
                    return None
                clean_retry = True
                self._status(tr("当前下载源无法恢复进度，正在重新下载…"))
            except RouteUnavailable:
                logger.warning("程序更新：线路配置不可用（%s）", route.name)
                return None
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                logger.warning("程序更新：线路下载失败（%s）：%s", route.name, type(exc).__name__)
                completed = await self._publish_complete_part()
                if completed is not None:
                    return completed
                if retries >= self.policy.network_retries or isinstance(exc, (httpx.HTTPStatusError, httpx.InvalidURL)):
                    return None
                retries += 1

    async def _watch_transfer(self, route: DownloadRoute, *, allow_switch: bool) -> None:
        """独立于正文读取检查低速；取消并等待请求关闭后才允许下一个源写入。"""
        self._monitor = SlowDownloadMonitor(self.policy)
        self._received = 0
        transfer = asyncio.create_task(self._transfer(route))
        try:
            while True:
                done, _pending = await asyncio.wait({transfer}, timeout=self.policy.poll_interval)
                if done:
                    transfer.result()
                    return
                if allow_switch and self._monitor.should_switch(remaining=self.artifact.size - self._downloaded):
                    raise _SlowSource()
        finally:
            if not transfer.done():
                transfer.cancel()
            await asyncio.gather(transfer, return_exceptions=True)

    async def _transfer(self, route: DownloadRoute) -> None:
        """仅在有效响应头确认后打开分片，响应未按 Range 返回时从零写入。"""
        timeout = httpx.Timeout(self.policy.read_timeout, connect=self.policy.connect_timeout)
        async with download_client(route, timeout) as client:
            while True:
                offset = self.cache.offset()
                self._source(route, offset)
                headers = {"Accept-Encoding": "identity"}
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                async with client.stream("GET", route.url, headers=headers) as response:
                    start, end = response_range(response, offset, self.artifact.size)
                    if start != offset:
                        self._source(route, start)
                    downloaded = start
                    with self.cache.part_path.open("ab" if start else "wb") as target:
                        async for chunk in response.aiter_bytes():
                            if downloaded + len(chunk) > end + 1:
                                raise InvalidResponse(tr("下载源返回的文件长度不符。"))
                            target.write(chunk)
                            downloaded += len(chunk)
                            self._received += len(chunk)
                            self._monitor.record(self._received)
                            self._progress(downloaded)
                    if downloaded != end + 1:
                        raise httpx.ReadError("Incomplete update response", request=response.request)
                if downloaded == self.artifact.size:
                    return
