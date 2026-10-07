"""声明式插件设置，统一默认值、校验、持久化和变更订阅。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from src.plugin_sdk._lifecycle import ContextResource
from src.plugin_sdk.configuration_schema import validate_setting_value, validate_settings
from src.plugin_sdk.disposable import Disposable, DisposableStore
from src.plugin_sdk.storage import PluginStorage
from src.utils.i18n import t as tr


@dataclass(frozen=True)
class ConfigurationChange:
    """单个设置成功保存后的有效值变化。"""

    key: str
    value: Any
    previous: Any


class ConfigurationFacade(ContextResource):
    """对本插件清单声明的设置提供经过校验的统一读写入口。"""

    def __init__(
        self,
        storage: PluginStorage,
        definitions: list[dict],
        subscriptions: DisposableStore | None = None,
        error_sink=None,
    ) -> None:
        """复制声明，绑定私有存储和回调异常处理器，首次访问才读取磁盘。"""
        super().__init__()
        validate_settings(definitions)
        self._definitions = {item["key"]: deepcopy(item) for item in definitions}
        self._storage = storage
        self._subscriptions = subscriptions
        self._error_sink = error_sink
        self._listeners: dict[object, Callable[[ConfigurationChange], None]] = {}

    def _definition(self, key: str) -> dict:
        """读取设置定义，拒绝停用后的访问和拼写错误的键。"""
        self._require_open()
        if key not in self._definitions:
            raise KeyError(tr("未声明的插件设置：{key}", key=key))
        return self._definitions[key]

    def get(self, key: str) -> Any:
        """返回有效持久值或声明默认值；无效旧值不会被静默写回覆盖。"""
        definition = self._definition(key)
        value = self._storage.get("settings." + key, definition["default"])
        try:
            validate_setting_value(definition, value)
        except ValueError:
            logging.getLogger(__name__).warning("插件设置 %s 的持久值无效，使用声明默认值", key)
            value = definition["default"]
        return deepcopy(value)

    def set(self, key: str, value: Any) -> None:
        """验证并原子保存设置，仅在持久化成功且有效值改变后通知订阅者。"""
        definition = self._definition(key)
        validate_setting_value(definition, value)
        previous = self.get(key)
        self._storage.set("settings." + key, value)
        if value != previous:
            self._notify(ConfigurationChange(key, deepcopy(value), previous))

    def reset(self, key: str) -> None:
        """删除覆盖值并恢复声明默认值，成功后通知有效值变化。"""
        definition = self._definition(key)
        previous = self.get(key)
        self._storage.delete("settings." + key)
        value = deepcopy(definition["default"])
        if value != previous:
            self._notify(ConfigurationChange(key, value, previous))

    def on_changed(self, callback: Callable[[ConfigurationChange], None]) -> Disposable:
        """订阅设置成功保存后的变化，返回可独立注销的句柄。"""
        self._require_open()
        if not callable(callback):
            raise TypeError("callback 必须可调用")
        token = object()
        self._listeners[token] = callback
        handle = Disposable(lambda: self._listeners.pop(token, None))
        return self._subscriptions.add(handle) if self._subscriptions is not None else handle

    def _notify(self, event: ConfigurationChange) -> None:
        """按注册快照通知，停用或注销后的订阅不再收到后续回调。"""
        for token, callback in list(self._listeners.items()):
            if self._closed or token not in self._listeners:
                continue
            try:
                callback(event)
            except Exception as exc:
                if self._error_sink is not None:
                    self._error_sink(exc)
                else:
                    logging.getLogger(__name__).exception("插件设置回调失败")

    def _close(self) -> None:
        """撤销所有订阅并禁止继续访问配置。"""
        super()._close()
        self._listeners.clear()
        self._error_sink = None
