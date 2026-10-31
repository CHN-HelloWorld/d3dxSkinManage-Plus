"""视图注册表（宿主侧）。plugin_id+view_id → 控件工厂。供 PluginsPage 渲染 views 贡献点。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.plugin_sdk.disposable import Disposable

ViewFactory = Callable[[], Any]


class ViewRegistry:
    """以插件 ID 和视图 ID 共同索引界面工厂。"""

    def __init__(self) -> None:
        # 插件标识与视图标识共同定位视图工厂。
        """创建空的插件视图工厂注册表。"""
        self._factories: dict[tuple[str, str], ViewFactory] = {}
        self._tokens: dict[tuple[str, str], object] = {}

    def register(self, plugin_id: str, view_id: str, factory: ViewFactory) -> Disposable:
        """登记或替换指定插件视图的构造工厂。"""
        if not view_id or not callable(factory):
            raise ValueError("view_id 必须非空且 factory 必须可调用")
        key = (plugin_id, view_id)
        self._factories[key] = factory
        token = object()
        self._tokens[key] = token

        def remove() -> None:
            """注销本次注册的工厂，保护同名视图的后续替换。"""
            if self._tokens.get(key) is token:
                del self._factories[key]
                del self._tokens[key]

        return Disposable(remove)

    def get(self, plugin_id: str, view_id: str) -> ViewFactory | None:
        """查找指定插件的视图工厂，未注册时返回 None。"""
        return self._factories.get((plugin_id, view_id))

    def unregister_plugin(self, plugin_id: str) -> None:
        """删除指定插件的全部视图工厂。"""
        for key in [k for k in self._factories if k[0] == plugin_id]:
            del self._factories[key]
            del self._tokens[key]
