"""插件专用日志器（公开 API）。自动带 `[plugin:<id>]` 前缀，便于排查归因。"""

from __future__ import annotations

from src.utils.logger import logger as _root_logger


class PluginLogger:
    """为宿主统一日志附加插件身份前缀的轻量日志接口。"""

    def __init__(self, plugin_id: str) -> None:
        """固定插件身份前缀，后续日志复用宿主日志配置。"""
        self._prefix = f"[plugin:{plugin_id}] "

    def debug(self, msg: str, *args: object) -> None:
        """记录带插件前缀的调试消息，格式参数交由日志系统延迟处理。"""
        _root_logger.debug(self._prefix + str(msg), *args)

    def info(self, msg: str, *args: object) -> None:
        """记录带插件前缀的一般运行消息。"""
        _root_logger.info(self._prefix + str(msg), *args)

    def warning(self, msg: str, *args: object) -> None:
        """记录带插件前缀的警告消息。"""
        _root_logger.warning(self._prefix + str(msg), *args)

    def error(self, msg: str, *args: object) -> None:
        """记录带插件前缀的错误消息。"""
        _root_logger.error(self._prefix + str(msg), *args)

    def exception(self, msg: str, *args: object) -> None:
        """在异常处理上下文中记录消息和当前异常堆栈。"""
        _root_logger.exception(self._prefix + str(msg), *args)
