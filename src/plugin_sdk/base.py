"""公开的插件元数据与生命周期基类。

身份信息由 plugin.json 声明；宿主据此构造只读 PluginMeta 并注入 ctx.meta。
PluginBase 负责生命周期接口，不重复声明身份信息。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.plugin_sdk.context import PluginContext


@dataclass(frozen=True)
class PluginMeta:
    """插件元数据只读视图（由宿主从 manifest 构造，注入 ctx.meta）。"""

    id: str
    name: str
    version: str
    api_version: str
    author: str = "Unknown"
    description: str = ""
    homepage: str = ""
    permissions: tuple[str, ...] = field(default_factory=tuple)


class PluginBase(ABC):
    """插件基类，定义 activate 和 deactivate 生命周期。

    插件须在清单 entry 指定的模块中导出名为 Plugin 的子类。
    activate 接收上下文，用于注册命令、事件和界面扩展；deactivate 释放插件资源。
    已注册的命令和事件由宿主统一清理。示例见 docs/tutorials/zh/插件开发指南.md。
    """

    @abstractmethod
    def activate(self, ctx: PluginContext) -> Any:
        """注册运行时贡献，可返回供声明依赖的插件调用的公开 API。"""
        ...

    def deactivate(self) -> None:
        """插件停用/卸载时调用。事件与命令由宿主自动清理；此处释放自有资源。"""
        # 默认空实现：多数插件无需手动清理。
        return None
