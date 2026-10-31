"""插件登记记录与状态（宿主内存态）。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from src.plugin_sdk.base import PluginBase
from src.plugin_sdk.configuration import ConfigurationFacade
from src.plugin_sdk.context import PluginContext
from src.plugin_sdk.storage import PluginStorage
from src.plugins.runtime.localization import PluginLocalizer
from src.plugins.runtime.manifest import Manifest
from src.plugins.runtime.module_loader import PluginModuleLoader


class PluginState(enum.Enum):
    """插件从发现、加载、激活到停用或故障的运行状态。"""

    DISCOVERED = "discovered"  # 已发现 manifest，未加载代码
    READY = "ready"  # 已授权且声明可用，等待激活条件
    ACTIVATING = "activating"  # 正在执行激活回调
    LOADED = "loaded"  # 代码已导入并实例化，未激活
    ACTIVE = "active"  # 已 activate，正在运行
    DISABLED = "disabled"  # 用户停用
    ERROR = "error"  # 加载/激活/运行抛错被熔断
    BLOCKED = "blocked"  # 依赖尚不可用


@dataclass
class PluginRecord:
    """汇总插件清单、运行实例、授权状态及最近错误，供管理器协调生命周期。"""

    manifest: Manifest
    state: PluginState = PluginState.DISCOVERED
    instance: PluginBase | None = None
    context: PluginContext | None = None
    error: str = ""  # 最近一次错误摘要（供管理面板展示）
    enabled: bool = True  # 用户期望的启用态（持久化）
    trusted: bool = False  # 用户是否已信任（首次启用确认）
    granted_permissions: frozenset[str] = field(default_factory=frozenset)
    translations: PluginLocalizer | None = field(default=None, repr=False)
    loader: PluginModuleLoader | None = field(default=None, repr=False)
    exports: object = field(default=None, repr=False)
    activation_reason: str = ""
    load_duration_ms: float = 0.0
    activation_duration_ms: float = 0.0
    storage: PluginStorage | None = field(default=None, repr=False)
    configuration: ConfigurationFacade | None = field(default=None, repr=False)

    @property
    def id(self) -> str:
        """返回清单中的插件唯一标识。"""
        return self.manifest.id

    @property
    def name(self) -> str:
        """返回跟随当前语言的插件显示名称。"""
        return self.translate(self.manifest.name)

    @property
    def description(self) -> str:
        """返回跟随当前语言的插件说明。"""
        return self.translate(self.manifest.description)

    def translate(self, source: str, /, **values) -> str:
        """优先使用插件独立语言包；未绑定时交给宿主翻译接口。"""
        if self.translations is not None:
            return self.translations.t(source, **values)
        from src.utils.i18n import t

        return t(source, **values)
