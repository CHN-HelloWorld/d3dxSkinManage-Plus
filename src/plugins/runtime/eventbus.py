"""事件总线（宿主侧）。带 per-plugin 熔断：handler 抛错 → 回调通知管理器停用该插件。"""

from __future__ import annotations

from collections.abc import Callable

from src.plugin_sdk.disposable import Disposable
from src.utils.logger import logger

EventHandler = Callable[..., None]
# 熔断回调：(plugin_id, event, exception) -> None
ErrorSink = Callable[[str, str, BaseException], None]


class EventBus:
    """宿主事件分发。订阅按 plugin_id 归组，便于停用时整批注销。"""

    def __init__(self, error_sink: ErrorSink | None = None) -> None:
        # 事件名映射到插件标识与处理函数列表。
        """初始化事件订阅列表和可选插件异常通知入口。"""
        self._subs: dict[str, list[tuple[str, EventHandler]]] = {}
        self._error_sink = error_sink

    # 订阅（供 SDK EventSubscription 调用，duck-typed）

    def subscribe(self, plugin_id: str, event: str, handler: EventHandler) -> Disposable:
        """将处理器附加到事件订阅列表，并记录所属插件以便统一注销。"""
        if not callable(handler):
            raise TypeError("handler 必须可调用")
        record = (plugin_id, handler)
        self._subs.setdefault(event, []).append(record)

        def remove() -> None:
            """按注册项身份撤销单个订阅，不影响重复订阅的其他句柄。"""
            entries = self._subs.get(event, [])
            self._subs[event] = [entry for entry in entries if entry is not record]
            if not self._subs[event]:
                self._subs.pop(event, None)

        return Disposable(remove)

    def unsubscribe_plugin(self, plugin_id: str) -> None:
        """注销某插件的全部订阅（停用/卸载时调用）。"""
        for event in list(self._subs.keys()):
            self._subs[event] = [record for record in self._subs[event] if record[0] != plugin_id]
            if not self._subs[event]:
                del self._subs[event]

    # 派发

    def emit(self, event: str, **payload) -> None:
        """向所有订阅者派发事件。某 handler 抛错被熔断，不影响其它订阅者与宿主。"""
        for record in list(self._subs.get(event, ())):
            plugin_id, handler = record
            # 前一个处理函数可能已停用或重新加载当前插件。
            if not any(entry is record for entry in self._subs.get(event, ())):
                continue
            try:
                handler(**payload)
            except Exception as e:
                if not any(entry is record for entry in self._subs.get(event, ())):
                    continue
                logger.exception("插件 '%s' 处理事件 '%s' 抛错，已熔断", plugin_id, event)
                if self._error_sink is not None:
                    try:
                        self._error_sink(plugin_id, event, e)
                    except Exception:
                        logger.exception("熔断回调自身抛错")
