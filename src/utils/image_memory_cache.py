"""支持并发访问的字节受限 LRU 缓存，用于隐式共享的 Qt 图像。"""

from collections import OrderedDict
from collections.abc import Hashable
from threading import RLock
from typing import Any


class ImageMemoryCache:
    """独立于显示控件保存不可变图像。

    调用方可通过 cost_bytes 计入附带元数据。返回值不得原地修改；
    需要修改时可先复制 QImage 或 QPixmap，Qt 会在写入前共享像素。"""

    def __init__(self, *, max_bytes: int):
        """建立带重入锁的空 LRU 缓存，以字节数约束容量。"""
        self._validate_limit(max_bytes)
        self._max_bytes = max_bytes
        self._entries: OrderedDict[Hashable, tuple[Any, int]] = OrderedDict()
        self._bytes_used = 0
        self._lock = RLock()
        self._generation = 0

    @staticmethod
    def _validate_limit(max_bytes: int) -> None:
        """要求缓存额度为非负整数，拒绝布尔值和其他类型。"""
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")

    @property
    def max_bytes(self) -> int:
        """在锁内读取当前缓存字节上限。"""
        with self._lock:
            return self._max_bytes

    @max_bytes.setter
    def max_bytes(self, value: int) -> None:
        """调整缓存字节上限，并按 resize 的规则淘汰超额条目。"""
        self.resize(value)

    def resize(self, max_bytes: int) -> None:
        """应用容量限制，返回前释放最旧的图像。"""
        self._validate_limit(max_bytes)
        with self._lock:
            self._max_bytes = max_bytes
            while self._entries and self._bytes_used > max_bytes:
                _, (_, cost) = self._entries.popitem(last=False)
                self._bytes_used -= cost

    @property
    def generation(self) -> int:
        """读取缓存代次，供异步加载结果判断是否已经失效。"""
        with self._lock:
            return self._generation

    @property
    def entry_count(self) -> int:
        """在锁内读取已缓存图像的数量。"""
        with self._lock:
            return len(self._entries)

    @property
    def bytes_used(self) -> int:
        """在锁内读取图像条目累计记账的字节数。"""
        with self._lock:
            return self._bytes_used

    def get(self, key: Hashable) -> Any | None:
        """返回指定图像并将其标记为最近使用；未命中返回 None。"""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry[0]

    def put(
        self,
        key: Hashable,
        image: Any,
        *,
        cost_bytes: int | None = None,
        expected_generation: int | None = None,
    ) -> bool:
        """按图像字节成本写入缓存并淘汰最久未使用项。

        代次不匹配、图像为空或单图超额时返回 False；同键旧条目在容量判断前移除。"""
        if cost_bytes is None:
            if hasattr(image, "sizeInBytes"):
                cost_bytes = int(image.sizeInBytes())
            else:
                # QPixmap 未提供 sizeInBytes，按扫描行对齐计算占用。
                cost_bytes = ((image.width() * image.depth() + 31) // 32) * 4 * image.height()
        if cost_bytes < 0:
            raise ValueError("cost_bytes must be non-negative")
        with self._lock:
            if expected_generation is not None and expected_generation != self._generation:
                return False
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._bytes_used -= previous[1]
            if not cost_bytes or cost_bytes > self._max_bytes:
                return False
            while self._entries and self._bytes_used + cost_bytes > self._max_bytes:
                _, (_, evicted_bytes) = self._entries.popitem(last=False)
                self._bytes_used -= evicted_bytes
            self._entries[key] = image, cost_bytes
            self._bytes_used += cost_bytes
            return True

    def remove(self, key: Hashable) -> None:
        """移除指定条目并扣除其字节成本；未命中时不操作。"""
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is not None:
                self._bytes_used -= entry[1]

    def clear(self) -> None:
        """清空图像和占用计数，同时递增代次以拒绝清理前发出的异步结果。"""
        with self._lock:
            self._generation += 1
            self._entries.clear()
            self._bytes_used = 0
