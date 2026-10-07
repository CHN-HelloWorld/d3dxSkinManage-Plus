"""插件命令注册（公开 API）。

命令是一切 UI 贡献点（工具页按钮 / 菜单项 / 激活事件）的执行单元。
插件在 activate 中 `ctx.commands.register("myplugin.doThing", handler)`；
manifest 的 contributes.commands / toolsPage / menus 通过 command id 引用它。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from src.plugin_sdk._lifecycle import ContextResource
from src.plugin_sdk.disposable import Disposable, DisposableStore

CommandHandler = Callable[..., Any]


class _CommandTableLike(Protocol):
    """插件命令注册门面依赖的最小宿主命令表协议。"""

    def register(self, plugin_id: str, command_id: str, handler: CommandHandler) -> Disposable:
        """将命令处理器登记到所属插件，以便停用时按插件统一清理。"""
        ...

    def execute(self, command_id: str, *args: Any, **kwargs: Any) -> Any:
        """调用已注册命令并透传位置参数、关键字参数和返回值。"""
        ...


class CommandRegistry(ContextResource):
    """插件侧命令门面（绑定 plugin_id，由宿主注入运行时命令表）。"""

    def __init__(self, plugin_id: str, table: _CommandTableLike, subscriptions: DisposableStore | None = None) -> None:
        """绑定插件身份及宿主命令表，并初始化上下文有效期。"""
        super().__init__()
        self._pid = plugin_id
        self._table = table
        self._subscriptions = subscriptions if subscriptions is not None else DisposableStore()

    def register(self, command_id: str, handler: CommandHandler) -> Disposable:
        """注册以完整插件 ID 为前缀的命令，返回可提前注销的句柄。"""
        self._require_open()
        return self._subscriptions.add(self._table.register(self._pid, command_id, handler))

    def execute(self, command_id: str, *args: Any, **kwargs: Any) -> Any:
        """执行任意已注册命令（可跨插件调用，便于组合）。"""
        self._require_open()
        return self._table.execute(command_id, *args, **kwargs)
