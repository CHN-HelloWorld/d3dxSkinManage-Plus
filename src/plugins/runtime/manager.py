"""PluginManager —— 插件发现 / 加载 / 激活 / 停用 / 事件派发 / 熔断 / 持久化。

- 只从应用的 plugins/ 目录发现插件，解析 manifest。
- 启用态/信任态/授权持久化到 config/plugin_state.json。
- 在授权和依赖检查后登记声明，按命令、视图、宿主事件或启动条件激活。
- ModManager 观察者回调将事件转交 EventBus；事件处理器或命令异常时停用对应插件。
"""

from __future__ import annotations

import json
import shutil
from copy import deepcopy
from functools import partial
from threading import get_ident
from time import perf_counter
from typing import TYPE_CHECKING

from src.plugin_sdk import API_VERSION
from src.plugin_sdk.base import PluginBase, PluginMeta
from src.plugin_sdk.configuration import ConfigurationFacade
from src.plugin_sdk.context import PluginContext
from src.plugin_sdk.errors import PluginError
from src.plugin_sdk.storage import PluginStorage
from src.plugins.runtime.commands import CommandTable
from src.plugins.runtime.contributions import ContributionRegistry
from src.plugins.runtime.dependencies import DependencyError, dependency_order, dependent_order
from src.plugins.runtime.eventbus import EventBus
from src.plugins.runtime.installer import InstallResult
from src.plugins.runtime.localization import PluginLocalizer
from src.plugins.runtime.manifest import (
    KNOWN_PERMISSIONS,
    Manifest,
    ManifestError,
    parse_manifest,
)
from src.plugins.runtime.module_loader import PluginModuleLoader
from src.plugins.runtime.registry import PluginRecord, PluginState
from src.plugins.runtime.security import scan_plugin
from src.plugins.runtime.validation import ensure_compatible
from src.plugins.runtime.views import ViewRegistry
from src.utils.constants import APP_VERSION
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_config_dir, get_data_dir, get_plugins_dir

if TYPE_CHECKING:
    from src.core.mod_manager import ModManager

_STATE_FILE = "plugin_state.json"


class PluginManager:
    """插件子系统的总入口。宿主只持有一个实例（建议在 MainWindow 构造）。"""

    def __init__(self, mod_manager: ModManager, operations=None) -> None:
        """组装插件事件、命令及贡献注册表，读取启用状态并订阅宿主业务事件。"""
        self._m = mod_manager
        self._operations = operations
        self._shutdown = False
        self._bus = EventBus(error_sink=self._on_plugin_error)
        self._commands = CommandTable(error_sink=self._on_plugin_error, activate=self.activate)
        self._contrib = ContributionRegistry(translate=self.translate)
        self._views = ViewRegistry()
        self._ui_host = None  # 由 MainWindow 注入（无头时保持 None）
        self._shortcut_host = None
        self._records: dict[str, PluginRecord] = {}
        self._tearing_down: set[str] = set()
        self._discovery_errors: dict[str, str] = {}
        self._task_runner = None
        self._thread_id = get_ident()
        self._post_event = None
        self._change_listeners: list = []
        self._state_path = get_config_dir() / _STATE_FILE
        self._persisted = self._load_state()

        # 将宿主事件转交插件事件总线，由各插件订阅处理。
        mod_manager.add_event_listener(self.on_host_event)

    def set_ui_host(self, ui_host) -> None:
        """注入宿主 UIHost（供 ctx.ui.register_page/show_dialog/notify）。在 load_all 前调用。"""
        self._ui_host = ui_host

    def set_shortcut_host(self, shortcut_host) -> None:
        """在激活插件前注入宿主快捷键注册表。"""
        self._shortcut_host = shortcut_host

    def set_task_runner(self, task_runner) -> None:
        """在激活任何插件之前注入宿主线程交付的后台任务队列。"""
        if any(rec.context is not None for rec in self._records.values()):
            raise PluginError(tr("后台任务服务必须在插件激活前设置"))
        self._task_runner = task_runner

    def set_event_dispatcher(self, post) -> None:
        """注入宿主线程投递入口，用于来自后台服务的业务事件。"""
        self._post_event = post

    # 变更通知（供 UI 刷新）

    def add_change_listener(self, listener) -> None:
        """注册 UI 刷新回调: listener。插件激活/停用/启停/熔断后触发。"""
        if listener not in self._change_listeners:
            self._change_listeners.append(listener)

    def _notify_changed(self) -> None:
        """通知变更监听器，隔离单个监听器异常以继续通知其余订阅者。"""
        for fn in list(self._change_listeners):
            try:
                fn()
            except Exception:
                logger.exception("插件变更监听器抛错")

    # 持久化

    def _load_state(self) -> dict:
        """读取插件持久化状态并过滤非对象条目；缺失或读取失败时使用空状态。"""
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text("utf-8"))
                if isinstance(data, dict):
                    return {pid: saved for pid, saved in data.items() if isinstance(saved, dict)}
        except Exception:
            logger.exception("读取 plugin_state.json 失败，使用空状态")
        return {}

    def _save_state(self) -> None:
        """通过临时文件替换保存插件状态，写入失败时记录异常。"""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._persisted, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(self._state_path)
        except Exception:
            logger.exception("写入 plugin_state.json 失败")

    def _persist_record(self, rec: PluginRecord) -> None:
        """保存插件启用、信任、版本及已授予权限到状态文件。"""
        self._persisted[rec.id] = {
            "enabled": rec.enabled,
            "trusted": rec.trusted,
            "version": rec.manifest.version,
            "granted_permissions": sorted(rec.granted_permissions),
        }
        self._save_state()

    # 发现

    def _scan_manifests(self) -> dict[str, Manifest]:
        """扫描插件根目录中的有效清单，跳过隐藏目录、非法清单及重复 ID。"""
        manifests: dict[str, Manifest] = {}
        self._discovery_errors.clear()
        base = get_plugins_dir()
        if base.is_dir():
            for entry in sorted(base.iterdir()):
                if entry.name.startswith(".") or not entry.is_dir() or not (entry / "plugin.json").exists():
                    continue
                try:
                    mf = parse_manifest(entry)
                except ManifestError as e:
                    logger.error("插件 manifest 非法，跳过: %s", e)
                    self._discovery_errors[str(entry)] = str(e)
                    continue
                if mf.id in manifests:
                    logger.warning("插件 id 冲突 '%s'，忽略目录 %s", mf.id, entry)
                    continue
                manifests[mf.id] = mf
        return manifests
