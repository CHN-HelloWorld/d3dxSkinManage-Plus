"""命令表（宿主侧）。全局 command_id → (plugin_id, handler)，供贡献点引用与跨插件调用。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.plugin_sdk.disposable import Disposable
from src.utils.i18n import t as tr
from src.utils.logger import logger

CommandHandler = Callable[..., Any]
ErrorSink = Callable[[str, str, BaseException], None]


class CommandTable:
    """按命令 ID 保存处理器及插件归属，并隔离插件执行异常。"""

    def __init__(self, error_sink: ErrorSink | None = None, *, activate=None) -> None:
        """初始化命令注册表和可选的插件错误通知入口。"""
        self._cmds: dict[str, tuple[str, CommandHandler]] = {}
        self._error_sink = error_sink
        self._activate = activate
        self._declared: dict[str, str] = {}

    def declare(self, plugin_id: str, command_id: str) -> None:
        """预登记命令归属，使界面和快捷键无需加载插件即可发现入口。"""
        owner = self.owner(command_id)
        if owner is not None and owner != plugin_id:
            raise ValueError(tr("命令 {command_id!r} 已由插件 {owner!r} 注册", command_id=command_id, owner=owner))
        self._declared[command_id] = plugin_id

    # 注册（供 SDK CommandRegistry 调用，duck-typed）

    def register(self, plugin_id: str, command_id: str, handler: CommandHandler) -> Disposable:
        """登记非空命令及可调用处理器，拒绝覆盖其他插件的命令。"""
        if self.owner(command_id) is not None:
            owner = self.owner(command_id)
            if owner != plugin_id:
                raise ValueError(tr("命令 {command_id!r} 已由插件 {owner!r} 注册", command_id=command_id, owner=owner))
        if (
            not isinstance(command_id, str)
            or not command_id.startswith(plugin_id + ".")
            or len(command_id) <= len(plugin_id) + 1
            or not callable(handler)
        ):
            raise ValueError(tr("命令需要非空 ID 和可调用的处理函数"))
        record = (plugin_id, handler)
        self._cmds[command_id] = record

        def remove() -> None:
            """仅删除此句柄注册的处理器，避免旧句柄覆盖后续注册。"""
            if self._cmds.get(command_id) is record:
                del self._cmds[command_id]

        return Disposable(remove)

    def unregister_plugin(self, plugin_id: str) -> None:
        """删除指定插件拥有的全部命令注册项。"""
        for cid in [c for c, (pid, _) in self._cmds.items() if pid == plugin_id]:
            del self._cmds[cid]
        for cid in [command for command, owner in self._declared.items() if owner == plugin_id]:
            del self._declared[cid]

    # 执行

    def has(self, command_id: str) -> bool:
        """判断命令 ID 是否已有处理器。"""
        return command_id in self._cmds or command_id in self._declared

    def has_handler(self, command_id: str) -> bool:
        """判断已激活插件是否提供了真实处理器。"""
        return command_id in self._cmds

    def owner(self, command_id: str) -> str | None:
        """返回命令所属插件 ID；未注册时返回 None。"""
        rec = self._cmds.get(command_id)
        return rec[0] if rec else self._declared.get(command_id)

    def execute(self, command_id: str, *args, **kwargs) -> Any:
        """执行命令并返回结果；未注册抛出 KeyError，插件异常则记录、上报并返回 None。"""
        plugin_id = self.owner(command_id)
        if plugin_id is None:
            raise KeyError(tr("命令未注册: {command_id}", command_id=command_id))
        rec = self._cmds.get(command_id)
        try:
            if self._activate is not None and not self._activate(plugin_id, "onCommand:" + command_id):
                return None
            rec = self._cmds.get(command_id)
            if rec is None:
                raise ValueError(tr("激活后未注册命令：{command_id}", command_id=command_id))
            _, handler = rec
            return handler(*args, **kwargs)
        except Exception as e:
            if self._cmds.get(command_id) is not rec:
                return None
            logger.exception("插件 '%s' 执行命令 '%s' 抛错，已熔断", plugin_id, command_id)
            if self._error_sink is not None:
                self._error_sink(plugin_id, command_id, e)
            return None
