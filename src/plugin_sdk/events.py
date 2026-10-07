"""提供插件事件订阅接口。

插件通过 ctx.events.on(EVENT, handler) 订阅宿主事件，
订阅在插件停用时由宿主自动注销。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from src.plugin_sdk._lifecycle import ContextResource
from src.plugin_sdk.disposable import Disposable, DisposableStore

# 宿主事件常量（稳定契约的一部分；新增事件走 MINOR 版本）
MOD_IMPORTED = "mod_imported"  # 参数：mod_id、node_id、game_id。
MOD_ENABLED = "mod_enabled"  # 参数：mod_id、game_id。
MOD_DISABLED = "mod_disabled"  # 参数：mod_id、game_id。
MOD_DELETED = "mod_deleted"  # 参数：mod_id、game_id。
GAME_CHANGED = "game_changed"  # 参数：game_id。
PRESET_APPLIED = "preset_applied"  # 参数：affected_ids、game_id。
MOD_METADATA_CHANGED = "mod_metadata_changed"  # 参数：mod_id、game_id。
TAGS_CHANGED = "tags_changed"  # 参数：affected_ids、game_id。
MOD_UPDATED = "mod_updated"  # 参数：mod_id、game_id。
INDEX_REBUILT = "index_rebuilt"  # 参数：game_id；保留持久化的 mod_id。
VARIANT_RESTORED = "variant_restored"  # 参数：mod_id、game_id、applied、removed_keys。
VARIANT_STATE_CHANGED = "variant_state_changed"  # 参数：mod_id、game_id；接收后查询新快照。
VARIANT_REFERENCES_MISSING = "variant_references_missing"  # 参数：game_id、preset_id、mod_ids、names。

ALL_EVENTS = (
    MOD_IMPORTED,
    MOD_ENABLED,
    MOD_DISABLED,
    MOD_DELETED,
    GAME_CHANGED,
    PRESET_APPLIED,
    MOD_METADATA_CHANGED,
    TAGS_CHANGED,
    MOD_UPDATED,
    INDEX_REBUILT,
    VARIANT_RESTORED,
    VARIANT_STATE_CHANGED,
    VARIANT_REFERENCES_MISSING,
)

EventHandler = Callable[..., None]


class _BusLike(Protocol):
    """插件事件订阅门面依赖的最小宿主事件总线协议。"""

    def subscribe(self, plugin_id: str, event: str, handler: EventHandler) -> Disposable:
        """登记带插件所有权的事件处理器，供宿主分发与停用清理。"""
        ...


class EventSubscription(ContextResource):
    """插件侧事件订阅门面（绑定到具体 plugin_id，由宿主注入运行时 bus）。"""

    def __init__(self, plugin_id: str, bus: _BusLike, subscriptions: DisposableStore | None = None) -> None:
        """绑定插件身份和事件总线，并初始化上下文有效期。"""
        super().__init__()
        self._pid = plugin_id
        self._bus = bus
        self._subscriptions = subscriptions if subscriptions is not None else DisposableStore()

    def on(self, event: str, handler: EventHandler) -> Disposable:
        """订阅某宿主事件。`event` 取本模块常量之一。

        handler 异常会被宿主熔断（记录 + 自动停用该插件），不会拖垮宿主。
        """
        self._require_open()
        if event not in ALL_EVENTS:
            raise ValueError(f"未知事件: {event!r}，可用: {ALL_EVENTS}")
        return self._subscriptions.add(self._bus.subscribe(self._pid, event, handler))
