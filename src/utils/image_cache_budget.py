"""统一管理活动图像缓存的字节预算，不持有缓存所有者。"""

from __future__ import annotations

import time
import weakref
from dataclasses import dataclass
from threading import RLock

from src.utils.image_memory_cache import ImageMemoryCache

MIB = 1024**2
# 768 MiB 总预算按 320/192/128/96/32 MiB 分配到各分区。
# 保留空闲分区的额度，避免打开其他页面时驱逐本页历史图像。
PARTITION_WEIGHTS = {
    "local_viewed": 10,
    "local_prefetch": 6,
    "local_rendered": 4,
    "online_viewed": 3,
    "online_prefetch": 1,
}


@dataclass(frozen=True)
class PartitionUsage:
    """记录一个图像缓存分区的额度、实际占用及缓存数量。"""

    name: str
    bytes_used: int
    limit_bytes: int
    entry_count: int
    cache_count: int


@dataclass(frozen=True)
class ImageBudgetSnapshot:
    """汇总配置额度、压力调整后的额度及各图像分区使用情况。"""

    configured_bytes: int
    limit_bytes: int
    bytes_used: int
    entry_count: int
    cache_count: int
    under_pressure: bool
    partitions: tuple[PartitionUsage, ...]


class ImageCacheBudget:
    """在各分区的活动缓存间分配预算，不调用所有者回调。

    锁顺序为预算锁、缓存锁；缓存不反向获取预算锁。注册表不持有页面、
    加载器、绑定方法或 Qt 对象。"""

    def __init__(self, *, total_bytes: int = 768 * MIB):
        """校验总字节额度，初始化弱引用注册表与内存压力采样状态。"""
        ImageMemoryCache._validate_limit(total_bytes)
        self._lock = RLock()
        self._configured_bytes = total_bytes
        self._caches: dict[int, tuple[weakref.ReferenceType[ImageMemoryCache], str]] = {}
        self._under_pressure = False
        self._prefetch_generation = 0
        self._low_samples = 0
        self._high_samples = 0
        self._pressure_since = 0.0

    @property
    def under_pressure(self) -> bool:
        """在锁内读取当前是否处于内存压力状态。"""
        with self._lock:
            return self._under_pressure

    @property
    def prefetch_allowed(self) -> bool:
        """仅在已分配预算且无内存压力时允许图像预取。"""
        with self._lock:
            return not self._under_pressure and self._configured_bytes > 0

    @property
    def prefetch_generation(self) -> int:
        """即使消费者在整个调整周期内空闲，也保留其预算策略记录。"""
        with self._lock:
            return self._prefetch_generation

    def register(self, cache: ImageMemoryCache, partition: str) -> None:
        """将缓存注册到唯一分区并重新分配额度；管理器仅保存弱引用。"""
        if partition not in PARTITION_WEIGHTS:
            raise ValueError(f"Unknown image partition: {partition}")
        cache_id = id(cache)
        manager_ref = weakref.ref(self)

        def collected(reference) -> None:
            """缓存被回收时移除对应注册项，并在锁内重新分配额度。"""
            manager = manager_ref()
            if manager is None:
                return
            with manager._lock:
                current = manager._caches.get(cache_id)
                if current is not None and current[0] is reference:
                    manager._caches.pop(cache_id)
                    manager._rebalance()

        with self._lock:
            current = self._caches.get(cache_id)
            if current is not None:
                if current[1] != partition:
                    raise ValueError("An image cache cannot belong to two partitions")
                return
            # 替换注册项时不重复计算完整额度。
            cache.resize(0)
            self._caches[cache_id] = weakref.ref(cache, collected), partition
            self._rebalance()

    def unregister(self, cache: ImageMemoryCache) -> None:
        """撤销注册、清空缓存并将其额度归零，再把额度分配给剩余缓存。"""
        with self._lock:
            if self._caches.pop(id(cache), None) is not None:
                cache.clear()
                cache.resize(0)
                self._rebalance()

    def set_budget(self, total_bytes: int) -> None:
        """更新总字节额度，递增预取代次并调整各缓存上限。"""
        ImageMemoryCache._validate_limit(total_bytes)
        with self._lock:
            if self._configured_bytes != total_bytes:
                self._configured_bytes = total_bytes
                self._prefetch_generation += 1
                self._rebalance()

    def _partition_limits(self) -> dict[str, int]:
        """计算分区额度；内存紧张时停用预取并将历史分区额度降至原来的 75%。"""
        limits = {name: self._configured_bytes * weight // 24 for name, weight in PARTITION_WEIGHTS.items()}
        if self._under_pressure:
            # 优先淘汰预取图像，历史分区保留 75% 额度。
            limits = {name: 0 if name.endswith("prefetch") else limit * 3 // 4 for name, limit in limits.items()}
        return limits

    def _live_caches(self) -> list[tuple[ImageMemoryCache, str]]:
        """取得仍存活的缓存及其分区，调用方负责持有管理器锁。"""
        return [
            (cache, partition) for reference, partition in self._caches.values() if (cache := reference()) is not None
        ]

    def _rebalance(self) -> None:
        """在管理器锁内均分分区额度，先收缩再扩容以避免临时超额。"""
        live = self._live_caches()
        targets = []
        for partition, limit in self._partition_limits().items():
            caches = [cache for cache, name in live if name == partition]
            if caches:
                share, remainder = divmod(limit, len(caches))
                targets.extend((cache, share + (index < remainder)) for index, cache in enumerate(caches))
        # 先执行所有额度收缩，再增加额度；加载器退出时遵循相同顺序。
        for cache, limit in targets:
            if limit < cache.max_bytes:
                cache.resize(limit)
        for cache, limit in targets:
            if limit > cache.max_bytes:
                cache.resize(limit)

    def clear(self) -> None:
        """使缓存清空前启动的工作失去写入资格。"""
        with self._lock:
            for cache, _partition in self._live_caches():
                cache.clear()

    def snapshot(self) -> ImageBudgetSnapshot:
        """在锁内汇总当前存活缓存的额度、占用、条目数及压力状态。"""
        with self._lock:
            live = self._live_caches()
            limits = self._partition_limits()
            partitions = []
            for name, limit in limits.items():
                caches = [cache for cache, partition in live if partition == name]
                partitions.append(
                    PartitionUsage(
                        name,
                        sum(cache.bytes_used for cache in caches),
                        limit,
                        sum(cache.entry_count for cache in caches),
                        len(caches),
                    )
                )
            return ImageBudgetSnapshot(
                self._configured_bytes,
                sum(limits.values()) if self._under_pressure else self._configured_bytes,
                sum(part.bytes_used for part in partitions),
                sum(part.entry_count for part in partitions),
                len(live),
                self._under_pressure,
                tuple(partitions),
            )

    def observe_memory(self, total: int | None, available: int | None, *, now: float | None = None) -> None:
        """每 5 秒采样一次，连续三次低内存时限流，连续六次充足时恢复。

        限流至少持续 60 秒；探测失败保持现有限额。瞬时可用内存不会修改已保存的用户偏好。"""
        if total is None or available is None or total <= 0 or not 0 <= available <= total:
            return
        now = time.monotonic() if now is None else now
        low = available < max(256 * MIB, total * 0.08)
        high = available > max(512 * MIB, total * 0.16)
        with self._lock:
            self._low_samples = self._low_samples + 1 if low else 0
            self._high_samples = self._high_samples + 1 if high else 0
            if not self._under_pressure and self._low_samples >= 3:
                self._under_pressure = True
                self._prefetch_generation += 1
                self._pressure_since = now
                self._rebalance()
            elif self._under_pressure and self._high_samples >= 6 and now - self._pressure_since >= 60:
                self._under_pressure = False
                self._prefetch_generation += 1
                self._low_samples = self._high_samples = 0
                self._rebalance()


image_cache_budget = ImageCacheBudget()
