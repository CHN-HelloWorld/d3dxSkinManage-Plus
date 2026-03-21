"""GameBanana API 响应缓存与图片缓存"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.utils.paths import get_gb_api_cache_dir, get_gb_image_cache_dir


def _is_transient_http_error(exc: BaseException) -> bool:
    """对 429、服务端错误及网络或超时异常重试，其他客户端错误直接失败。"""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return isinstance(exc, (httpx.NetworkError, httpx.TimeoutException))


class GbApiCache:
    """GameBanana 接口响应缓存，提供内存 LRU、请求去重和可选磁盘持久化。

    持久化项将原始 JSON 与绝对过期时间保存到 cache/gb_api，启动时加载有效条目。
    使用墙钟时间比较跨进程保存的到期时刻，同一请求的并发消费者共享进行中的结果。
    """

    MEM_TTL_DEFAULT = 600
    CAT_TTL_SECONDS = 1800

    def __init__(self, max_entries: int = 256, *, persist_dir: Path | None = None) -> None:
        """建立有容量上限的异步请求缓存与同键请求表，并加载尚未过期的磁盘条目。"""
        self._entries: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}
        self._max = max_entries
        self._lock = asyncio.Lock()
        # 未指定持久化目录时使用默认路径；写入失败不影响内存缓存。
        self._persist_dir = persist_dir if persist_dir is not None else get_gb_api_cache_dir()
        self._load_disk()

    # 磁盘持久化

    def _disk_path(self, key: str) -> Path:
        """将请求键散列为固定长度的 JSON 文件名，避免原始请求字符影响缓存路径。"""
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self._persist_dir / f"{h}.json"

    def _load_disk(self) -> None:
        """加载尚未过期的磁盘缓存，并尝试清理过期或损坏文件。"""
        now = time.time()
        try:
            files = sorted(self._persist_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for f in files:
            try:
                obj = json.loads(f.read_text(encoding="utf-8"))
                exp = float(obj.get("expires", 0))
                key = obj["key"]
            except (OSError, ValueError, KeyError, TypeError):
                try:
                    f.unlink()
                except OSError:
                    pass
                continue
            if exp <= now:
                try:
                    f.unlink()
                except OSError:
                    pass
                continue
            self._entries[key] = (exp, obj["value"])
            # 加载时遵守内存容量上限，超限后移除最旧条目及其磁盘副本。
            while len(self._entries) > self._max:
                old_key, _ = self._entries.popitem(last=False)
                self._remove_disk(old_key)

    def _write_disk(self, key: str, expires: float, value: Any) -> None:
        """原子写入缓存项；无法序列化或写入失败时保留内存缓存。"""
        try:
            path = self._disk_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps({"key": key, "expires": expires, "value": value}, ensure_ascii=False)
        except (TypeError, ValueError):
            return  # 无法序列化为 JSON 的值仅在内存中缓存。
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    def _remove_disk(self, key: str) -> None:
        """尽力移除指定请求键的磁盘副本，文件缺失或删除失败不阻断缓存处理。"""
        try:
            self._disk_path(key).unlink(missing_ok=True)
        except OSError:
            pass

    async def get_or_fetch(
        self,
        key: str,
        ttl: int,
        fetcher: Callable[[], Awaitable[Any]],
        *,
        persist: bool = False,
    ) -> Any:
        """有效条目立即返回；同键未命中请求合并获取，成功后按容量淘汰并可选写入磁盘。"""
        now = time.time()
        async with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                expires_at, value = entry
                if expires_at > now:
                    self._entries.move_to_end(key)
                    return value
                self._entries.pop(key, None)
                if persist:
                    self._remove_disk(key)

            future = self._inflight.get(key)
            if future is None:
                # 使用当前运行的事件循环，使 Future 与调用协程属于同一循环。
                future = asyncio.get_running_loop().create_future()
                self._inflight[key] = future
                primary = True
            else:
                primary = False

        if primary:
            try:
                value = await fetcher()
                expires = time.time() + ttl
                async with self._lock:
                    self._entries[key] = (expires, value)
                    self._entries.move_to_end(key)
                    while len(self._entries) > self._max:
                        old_key, _ = self._entries.popitem(last=False)
                        self._remove_disk(old_key)
                if persist:
                    # 在锁外持久化，避免磁盘操作占用异步缓存锁。
                    self._write_disk(key, expires, value)
                future.set_result(value)
            except asyncio.CancelledError:
                # 外层取消任务时直接取消共享 Future，使等待者收到取消结果，
                # 避免将 CancelledError 作为未消费异常记录。
                future.cancel()
                raise
            except BaseException as exc:
                future.set_exception(exc)
                # 通过回调消费 Future 异常，避免没有等待者时产生未处理异常告警；
                # 已有等待者仍会正常收到同一异常。
                future.add_done_callback(lambda f: f.exception())
                raise
            finally:
                async with self._lock:
                    self._inflight.pop(key, None)
            return value
        return await future

    def peek(self, key: str) -> Any | None:
        """同步读取未过期的内存缓存，不存在或已过期时返回 None。

        不发送请求或排队，供批量接口先筛选未命中的键再组建请求。
        """
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.time():
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        return value

    def put(self, key: str, ttl: int, value: Any) -> None:
        """将批量接口的单项结果写入内存缓存，不进行磁盘持久化。"""
        self._entries[key] = (time.time() + ttl, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            old_key, _ = self._entries.popitem(last=False)
            self._remove_disk(old_key)
