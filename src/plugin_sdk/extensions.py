"""通过声明依赖访问其他插件公开的 API，不暴露运行时记录或内部实例。"""

from __future__ import annotations

from typing import Any

from src.plugin_sdk._lifecycle import ContextResource
from src.plugin_sdk.errors import PluginError
from src.utils.i18n import t as tr


class ExtensionDependencies(ContextResource):
    """绑定调用插件身份，通过宿主解析依赖的导出对象。"""

    def __init__(self, plugin_id: str, host=None) -> None:
        """保存插件身份与可选依赖解析入口。"""
        super().__init__()
        self._plugin_id = plugin_id
        self._host = host

    def get_exports(self, plugin_id: str) -> Any:
        """激活已声明且可用的依赖，并取得 activate 返回的公开 API。"""
        self._require_open()
        if self._host is None:
            raise PluginError(tr("宿主未提供插件依赖服务"))
        return self._host.get_exports(self._plugin_id, plugin_id)
