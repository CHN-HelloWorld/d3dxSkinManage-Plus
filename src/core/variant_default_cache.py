"""按 Mod 和来源退役旧版本，以有界 LRU 复用原始 INI 正文。"""

from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from sys import getsizeof

IniContents = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _Entry:
    """保存一个来源当前版本的不可变正文和计费大小。"""

    signature: tuple
    inis: IniContents
    size_bytes: int


class DefaultIniCache:
    """最多保留 256 个来源及 32 MiB 正文，快照复制共享不可变正文。"""

    def __init__(self, *, max_entries: int = 256, max_bytes: int = 32 * 1024 * 1024) -> None:
        """建立独立的淘汰顺序和来源所有权索引；容量为零时仅提供直读。"""
        self.max_entries = max(0, max_entries)
        self.max_bytes = max(0, max_bytes)
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._sources: dict[str, str] = {}
        self._size_bytes = 0

    def __len__(self) -> int:
        """返回当前保留的来源数量。"""
        return len(self._entries)

    def read(self, mod_id: str, signature: tuple, load: Callable[[], IniContents]) -> IniContents:
        """命中时复用正文；变化时退役同 Mod 或同来源旧版本，再按容量收纳新正文。"""
        source = os.path.normcase(os.path.abspath(signature[0]))
        signature = (source, *signature[1:])
        entry = self._entries.get(mod_id)
        if entry is not None and entry.signature == signature:
            self._entries.move_to_end(mod_id)
            return entry.inis
        self.discard(mod_id)
        previous_owner = self._sources.get(source)
        previous = self._entries.get(previous_owner) if previous_owner is not None else None
        inis = previous.inis if previous is not None and previous.signature == signature else None
        if previous_owner is not None:
            self.discard(previous_owner)
        if inis is None:
            inis = load()
        size = (
            getsizeof(mod_id)
            + getsizeof(signature)
            + sum(getsizeof(value) for value in signature)
            + getsizeof(inis)
            + sum(getsizeof(pair) + getsizeof(pair[0]) + getsizeof(pair[1]) for pair in inis)
        )
        if not self.max_entries or size > self.max_bytes:
            return inis
        while self._entries and (len(self._entries) >= self.max_entries or self._size_bytes + size > self.max_bytes):
            self.discard(next(iter(self._entries)))
        self._entries[mod_id] = _Entry(signature, inis, size)
        self._sources[source] = mod_id
        self._size_bytes += size
        return inis

    def discard(self, mod_id: str) -> None:
        """退役单个 Mod，不影响其他来源的热缓存。"""
        entry = self._entries.pop(mod_id, None)
        if entry is not None:
            self._size_bytes -= entry.size_bytes
            self._sources.pop(entry.signature[0], None)

    def clear(self) -> None:
        """清空正文引用、来源索引及计费大小。"""
        self._entries.clear()
        self._sources.clear()
        self._size_bytes = 0

    def copy(self) -> DefaultIniCache:
        """为独立工作线程复制索引和 LRU 顺序，保持容量策略而不复制正文。"""
        clone = DefaultIniCache(max_entries=self.max_entries, max_bytes=self.max_bytes)
        clone._entries = self._entries.copy()
        clone._sources = self._sources.copy()
        clone._size_bytes = self._size_bytes
        return clone
