"""保护上下文所属注册项的共同生命周期。"""

from threading import get_ident

from src.plugin_sdk.errors import PluginError
from src.utils.i18n import t as tr


class ContextResource:
    """随插件上下文停用而失效的资源基类。"""

    def __init__(self) -> None:
        """将资源标记为可用，等待插件停用时关闭。"""
        self._closed = False
        self._thread_id = get_ident()

    def _close(self) -> None:
        """标记资源已关闭；允许重复调用。"""
        self._closed = True

    def _require_open(self) -> None:
        """在业务调用前检查资源有效期，停用后抛出 PluginError。"""
        if self._closed:
            raise PluginError(tr("插件已停用"))
        if get_ident() != self._thread_id:
            raise PluginError(tr("插件接口只能在宿主线程调用"))
