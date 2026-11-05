"""声明更新下载线路及有界重试、测速和低速检测策略。"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx

from src.services.app_update.download_state import DownloadError, InvalidResponse, UpdateArtifact, response_range
from src.utils.constants import APP_UPDATE_DOWNLOAD_MIRRORS
from src.utils.i18n import t as tr


@dataclass(frozen=True)
class DownloadRoute:
    """同一附件的一种源和代理组合，直连由空代理明确表示。"""

    name: str
    url: str
    proxy: str | None = None


@dataclass(frozen=True)
class DownloadPolicy:
    """集中声明线路探测及传输限制，时间均使用秒，速度使用字节每秒。"""

    network_retries: int = 1
    connect_timeout: float = 10.0
    read_timeout: float = 15.0
    probe_timeout: float = 3.0
    probe_budget: float = 6.0
    probe_bytes: int = 256 * 1024
    probe_concurrency: int = 2
    warmup: float = 5.0
    speed_window: float = 10.0
    slow_duration: float = 15.0
    slow_speed: float = 64 * 1024
    poll_interval: float = 0.25
    finish_grace: float = 15.0


@dataclass(frozen=True)
class ProbeResult:
    """记录含首包等待时间的有效吞吐量及实际 Range 支持情况。"""

    route: DownloadRoute
    speed: float = 0.0
    resumable: bool = False


class RouteUnavailable(DownloadError):
    """当前代理配置或可选协议依赖不可用，不影响其它候选线路。"""


def download_client(route: DownloadRoute, timeout: float | httpx.Timeout) -> httpx.AsyncClient:
    """明确使用指定代理，隔离单条线路的构造错误，禁止环境变量隐式改变直连语义。"""
    try:
        return httpx.AsyncClient(proxy=route.proxy, trust_env=False, follow_redirects=True, timeout=timeout)
    except (httpx.InvalidURL, ValueError, ImportError) as exc:
        raise RouteUnavailable(tr("下载线路配置不可用。")) from exc


def build_download_routes(url: str, source: str, proxy: str | None) -> list[DownloadRoute]:
    """按用户来源限制构造候选，自动模式分别比较代理与直连，手动模式不跨源。"""
    selected = [item for item in APP_UPDATE_DOWNLOAD_MIRRORS if item[0] == source]
    mirrors = selected or list(APP_UPDATE_DOWNLOAD_MIRRORS)
    routes = []
    seen = set()
    # 官方代理优先进入探测队列；直连镜像与代理镜像均是独立候选。
    choices = [(item, proxy) for item in mirrors] if proxy else []
    if selected:
        choices.extend((item, None) for item in mirrors)
    else:
        official = [choice for choice in choices if not choice[0][2]]
        choices = official + [(item, None) for item in mirrors] + [choice for choice in choices if choice[0][2]]
    for (_key, name, prefix), connection in choices:
        target = f"{prefix}{url}" if prefix else url
        identity = (target, connection)
        if identity in seen:
            continue
        seen.add(identity)
        label = tr("{source} · {connection}", source=tr(name), connection=tr("经代理") if connection else tr("直连"))
        routes.append(DownloadRoute(label, target, connection))
    return routes


class SlowDownloadMonitor:
    """按单调时钟观察新增字节，低速持续时间与短时速度波动分开计算。"""

    def __init__(self, policy: DownloadPolicy, clock: Callable[[], float] = time.monotonic) -> None:
        """每次线路尝试建立独立观察窗口，不计入此前已下载的分片。"""
        self.policy = policy
        self._clock = clock
        self._started = clock()
        self._samples = deque([(self._started, 0)])
        self._received = 0
        self._slow_since: float | None = None
        self.speed = 0.0

    def record(self, received: int) -> None:
        """记录当前线路累计新增字节，由定时采样统一维护窗口。"""
        self._received = received

    def should_switch(self, *, remaining: int) -> bool:
        """无正文事件时同样采样；连续低速且无法很快完成时才请求换源。"""
        now = self._clock()
        self._samples.append((now, self._received))
        while len(self._samples) > 1 and self._samples[1][0] <= now - self.policy.speed_window:
            self._samples.popleft()
        oldest_time, oldest_bytes = self._samples[0]
        elapsed = now - oldest_time
        self.speed = max(0, self._received - oldest_bytes) / elapsed if elapsed > 0 else 0.0
        if now - self._started < self.policy.warmup + self.policy.speed_window:
            return False
        if self.speed >= self.policy.slow_speed:
            self._slow_since = None
            return False
        if self._slow_since is None:
            self._slow_since = now
        if remaining <= 0 or (self.speed > 0 and remaining / self.speed <= self.policy.finish_grace):
            return False
        return now - self._slow_since >= self.policy.slow_duration


async def probe_routes(
    routes: Sequence[DownloadRoute], artifact: UpdateArtifact, policy: DownloadPolicy
) -> list[ProbeResult]:
    """在并发数、单源时限、总时限和正文大小限制内测速，取消时关闭全部探测连接。"""
    semaphore = asyncio.Semaphore(policy.probe_concurrency)
    results = {route: ProbeResult(route) for route in routes}
    limit = min(artifact.size, policy.probe_bytes)

    async def measure(route: DownloadRoute) -> None:
        """独立客户端读取有限正文，吞吐量包含连接和首包时间。"""
        async with semaphore:
            started = time.monotonic()
            received = 0
            resumable = False

            async def receive() -> None:
                """验证与目标一致的响应头，正文达到上限后主动关闭流。"""
                nonlocal received, resumable
                async with download_client(route, policy.probe_timeout) as client:
                    async with client.stream(
                        "GET", route.url, headers={"Range": f"bytes=0-{limit - 1}", "Accept-Encoding": "identity"}
                    ) as response:
                        response_range(response, 0, artifact.size, probe=True)
                        resumable = response.status_code == 206
                        async for chunk in response.aiter_bytes():
                            received += min(len(chunk), limit - received)
                            if received >= limit:
                                break

            try:
                await asyncio.wait_for(receive(), policy.probe_timeout)
            except (httpx.HTTPError, httpx.InvalidURL, InvalidResponse, RouteUnavailable, asyncio.TimeoutError):
                pass
            finally:
                elapsed = max(time.monotonic() - started, 0.001)
                results[route] = ProbeResult(route, received / elapsed, resumable)

    tasks = [asyncio.create_task(measure(route)) for route in routes]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), policy.probe_budget)
    except asyncio.TimeoutError:
        pass
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return sorted(results.values(), key=lambda result: result.speed, reverse=True)
