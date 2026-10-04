"""PluginContext —— 注入给每个插件的唯一入口（公开 API）。

插件在 `activate(ctx)` 中通过 ctx 访问一切宿主能力：日志、存储、事件、命令、Mod/游戏门面。
ctx 由宿主运行时（src/plugins/runtime/manager.py）构造，插件不应自行实例化。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from src.plugin_sdk.base import PluginMeta
from src.plugin_sdk.commands import CommandRegistry
from src.plugin_sdk.configuration import ConfigurationFacade
from src.plugin_sdk.disposable import DisposableStore
from src.plugin_sdk.events import EventSubscription
from src.plugin_sdk.extensions import ExtensionDependencies
from src.plugin_sdk.facades import GameFacade, ModFacade
from src.plugin_sdk.i18n import TranslationFacade
from src.plugin_sdk.logger import PluginLogger
from src.plugin_sdk.presets import PresetFacade
from src.plugin_sdk.storage import PluginStorage
from src.plugin_sdk.tasks import TaskFacade
from src.plugin_sdk.ui_ext import UIExtensionPoints
from src.plugin_sdk.variants import VariantFacade

if TYPE_CHECKING:
    from src.core.mod_manager import ModManager


class PluginContext:
    """插件运行上下文（每插件一个实例）。"""

    def __init__(
        self,
        *,
        meta: PluginMeta,
        app_version: str,
        api_version: str,
        plugin_dir: Path,
        data_dir: Path,
        mod_manager: ModManager,
        event_bus,  # runtime EventBus（duck-typed，见 events._BusLike）
        command_table,  # runtime CommandTable（duck-typed，见 commands._CommandTableLike）
        view_registry,  # runtime ViewRegistry（duck-typed，见 ui_ext._ViewRegistryLike）
        ui_host=None,  # 宿主 UIHost（duck-typed，见 ui_ext._UIHostLike）；无头时 None
        operations=None,  # 宿主主线程操作队列；无头只读上下文可不提供
        translations=None,  # 插件独立语言包，跟随宿主语言
        extension_host=None,
        storage: PluginStorage | None = None,
        configuration: ConfigurationFacade | None = None,
        task_runner=None,
    ) -> None:
        """按插件权限组装日志、存储、翻译及业务门面，绑定宿主服务和操作队列。"""
        self.meta = meta
        self.app_version = app_version
        self.api_version = api_version
        self.plugin_dir = plugin_dir
        self.subscriptions = DisposableStore()

        granted = frozenset(meta.permissions)

        self.logger = PluginLogger(meta.id)
        self.storage = storage if storage is not None else PluginStorage(data_dir, granted=granted)
        self.configuration = configuration if configuration is not None else ConfigurationFacade(self.storage, [])
        self.i18n = TranslationFacade(translations)
        self.events = EventSubscription(meta.id, event_bus, self.subscriptions)
        self.commands = CommandRegistry(meta.id, command_table, self.subscriptions)
        self.extensions = ExtensionDependencies(meta.id, extension_host)
        self.tasks = TaskFacade(meta.id, task_runner, granted)
        self.ui = UIExtensionPoints(meta.id, view_registry, ui_host, granted=granted, subscriptions=self.subscriptions)
        self.mods = ModFacade(mod_manager, granted, operations=operations, owner=meta.id)
        self.presets = PresetFacade(mod_manager, granted, operations=operations, owner=meta.id)
        self.variants = VariantFacade(mod_manager, granted, operations=operations, owner=meta.id)
        self.game = GameFacade(mod_manager, granted)

    @property
    def plugin_id(self) -> str:
        """返回清单中声明的插件唯一标识。"""
        return self.meta.id

    def _dispose(self) -> None:
        """使业务门面和扩展注册入口失效，防止插件停用后继续调用。"""
        self.mods._close()
        self.presets._close()
        self.variants._close()
        self.game._close()
        self.events._close()
        self.commands._close()
        self.ui._close()
        self.extensions._close()
        self.tasks._close()
        self.configuration._close()
        self.storage._close()
        self.subscriptions.dispose()

    def has_permission(self, perm: str) -> bool:
        """判断插件清单是否声明指定权限。"""
        return perm in self.meta.permissions
