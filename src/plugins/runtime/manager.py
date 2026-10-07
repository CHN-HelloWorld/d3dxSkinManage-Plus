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

    def _make_record(self, mf: Manifest) -> PluginRecord:
        """合并清单与已保存状态；权限增加或授权记录无效时撤销信任状态。"""
        saved = self._persisted.get(mf.id, {})
        requested = frozenset(mf.permissions) & KNOWN_PERMISSIONS
        raw_grants = saved.get("granted_permissions", [])
        valid_grants = isinstance(raw_grants, list) and all(isinstance(p, str) for p in raw_grants)
        grants = frozenset(raw_grants if valid_grants else ()) & KNOWN_PERMISSIONS
        return PluginRecord(
            manifest=mf,
            enabled=bool(saved.get("enabled", True)),
            trusted=saved.get("trusted") is True and valid_grants and requested <= grants,
            granted_permissions=grants,
            translations=PluginLocalizer(mf),
        )

    def discover(self) -> list[PluginRecord]:
        """重扫前完整停用旧实例，避免遗留订阅、命令和页面。"""
        manifests = self._scan_manifests()
        self._stop_all()
        self._records = {pid: self._make_record(mf) for pid, mf in manifests.items()}
        logger.info("发现 %d 个插件: %s", len(self._records), list(self._records))
        return list(self._records.values())

    # 加载 / 激活

    def _build_context(self, mf: Manifest, *, translations=None) -> PluginContext:
        """根据已知权限创建插件上下文，注入独立数据目录和宿主扩展服务。"""
        meta = PluginMeta(
            id=mf.id,
            name=mf.name,
            version=mf.version,
            api_version=mf.api_version,
            author=mf.author,
            description=mf.description,
            homepage=mf.homepage,
            permissions=tuple(p for p in mf.permissions if p in KNOWN_PERMISSIONS),
        )
        data_dir = get_data_dir() / "plugins_data" / mf.id
        record = self._records.get(mf.id)
        if record is not None:
            self._ensure_configuration(record)
        return PluginContext(
            meta=meta,
            app_version=APP_VERSION,
            api_version=API_VERSION,
            plugin_dir=mf.directory,
            data_dir=data_dir,
            mod_manager=self._m,
            event_bus=self._bus,
            command_table=self._commands,
            view_registry=self._views,
            ui_host=self._ui_host,
            operations=self._operations,
            translations=translations,
            extension_host=self,
            storage=record.storage if record is not None else None,
            configuration=record.configuration if record is not None else None,
            task_runner=self._task_runner,
        )

    def _ensure_configuration(self, rec: PluginRecord) -> None:
        """创建共享的存储和设置门面，待命界面与激活上下文使用同一实例。"""
        if rec.storage is None:
            rec.storage = PluginStorage(
                get_data_dir() / "plugins_data" / rec.id, granted=frozenset(rec.manifest.permissions)
            )
        if rec.configuration is None:

            def on_error(exc) -> None:
                """仅将当前记录拥有的设置回调异常送入插件故障处理。"""
                if self._records.get(rec.id) is rec:
                    self._on_plugin_error(rec.id, "configuration", exc)

            rec.configuration = ConfigurationFacade(
                rec.storage, rec.manifest.contributes.get("settings", []), error_sink=on_error
            )

    def get_configuration(self, plugin_id: str) -> ConfigurationFacade:
        """取得可用插件的设置，无需导入代码；不可用记录不能写入。"""
        rec = self._records.get(plugin_id)
        if rec is None or not self._prepare(rec):
            raise PluginError(tr("插件设置暂不可用：{plugin_id}", plugin_id=plugin_id))
        self._ensure_configuration(rec)
        assert rec.configuration is not None
        return rec.configuration

    @staticmethod
    def _compatibility_error(mf: Manifest) -> str:
        """在导入代码前检查宿主应用和 API 版本，返回可展示的失败说明。"""
        try:
            ensure_compatible(mf)
        except ManifestError as exc:
            return str(exc)
        return ""

    def _prepare(self, rec: PluginRecord) -> bool:
        """验证依赖并预登记声明式贡献，此过程不导入任何插件代码。"""
        if self._shutdown or rec.id in self._tearing_down:
            return False
        if not rec.enabled or not rec.trusted:
            rec.state = PluginState.DISCOVERED if rec.enabled else PluginState.DISABLED
            return False
        if rec.state in {PluginState.READY, PluginState.ACTIVE, PluginState.ACTIVATING}:
            return True
        if rec.state is PluginState.ERROR:
            return False
        error = self._compatibility_error(rec.manifest)
        if error:
            rec.error, rec.state = error, PluginState.ERROR
            return False
        try:
            order = dependency_order(rec.id, self._records)
            for plugin_id in order[:-1]:
                dependency = self._records[plugin_id]
                error = self._compatibility_error(dependency.manifest)
                if error or dependency.state is PluginState.ERROR:
                    raise DependencyError(
                        tr("依赖插件 {plugin_id} 不可用：{error}", plugin_id=plugin_id, error=error or dependency.error)
                    )
        except DependencyError as exc:
            rec.error, rec.state = str(exc), PluginState.BLOCKED
            return False
        try:
            for declaration in rec.manifest.contributes.get("commands", []):
                self._commands.declare(rec.id, declaration["id"])
            self._contrib.add(rec.manifest)
            if self._shortcut_host is not None:
                self._shortcut_host.register(rec.manifest, self._commands, self.translate)
            rec.state, rec.error = PluginState.READY, ""
            return True
        except Exception as exc:
            self._on_plugin_error(rec.id, "contributions", exc)
            return False

    def _prepare_all(self) -> None:
        """重新评估依赖受阻或尚未登记的插件，使新满足的贡献可立即使用。"""
        for rec in list(self._records.values()):
            self._prepare(rec)

    def _load(self, rec: PluginRecord) -> bool:
        """导入 entry 模块并实例化 Plugin（不激活）。"""
        mf = rec.manifest
        started = perf_counter()
        # 加载前扫描语法树，拒绝宿主内部导入、未授权的受限模块和动态执行调用。
        scan = scan_plugin(mf.directory, mf.permissions)
        if not scan.ok:
            rec.error = tr("安全扫描未通过: {details}", details=scan.summary())
            logger.error("插件 '%s' 安全扫描拦截：%s", mf.id, scan.summary())
            self._stop_tree(rec.id, PluginState.ERROR)
            return False
        if scan.gated_ok:
            logger.info("插件 '%s' 已授权危险模块: %s", mf.id, scan.gated_ok)

        try:
            rec.loader = PluginModuleLoader(mf)
            module = rec.loader.load()

            plugin_cls = getattr(module, "Plugin", None)
            if plugin_cls is None:
                raise ImportError(tr("entry 模块缺少 'Plugin' 类"))
            if not (isinstance(plugin_cls, type) and issubclass(plugin_cls, PluginBase)):
                raise TypeError(tr("'Plugin' 未继承 src.plugin_sdk.PluginBase"))

            rec.instance = plugin_cls()
            rec.context = self._build_context(mf, translations=rec.translations)
            rec.state = PluginState.LOADED
            rec.error = ""
            return True
        except Exception as e:
            rec.state = PluginState.ERROR
            rec.error = tr("加载失败: {error}", error=e)
            logger.exception("加载插件 '%s' 失败", mf.id)
            self._stop_tree(rec.id, PluginState.ERROR)
            return False
        finally:
            rec.load_duration_ms = (perf_counter() - started) * 1000

    def _activate(self, rec: PluginRecord, reason: str = "manual") -> bool:
        """加载并激活插件，登记贡献和快捷键；失败时清理资源并进入错误状态。"""
        if self._shutdown or rec.id in self._tearing_down:
            return False
        if rec.state is PluginState.ACTIVE:
            return True
        if rec.state is PluginState.ACTIVATING:
            raise PluginError(tr("插件激活发生重入：{plugin_id}", plugin_id=rec.id))
        if not self._prepare(rec):
            return False
        for plugin_id in rec.manifest.dependencies:
            if not self._activate(self._records[plugin_id], "dependency:" + rec.id):
                rec.error = tr("依赖插件激活失败：{plugin_id}", plugin_id=plugin_id)
                self._teardown(rec, state=PluginState.BLOCKED)
                return False
        rec.activation_reason = reason
        if rec.instance is None or rec.context is None:
            if not self._load(rec):
                return False
        assert rec.instance is not None and rec.context is not None
        rec.state = PluginState.ACTIVATING
        context = rec.context
        started = perf_counter()
        try:
            exports = rec.instance.activate(context)
            if (
                self._records.get(rec.id) is not rec
                or rec.context is not context
                or rec.state is not PluginState.ACTIVATING
            ):
                return False
            for declaration in rec.manifest.contributes.get("commands", []):
                if not self._commands.has_handler(declaration["id"]):
                    raise ValueError(tr("激活后未注册命令：{command_id}", command_id=declaration["id"]))
            rec.exports = exports
            rec.state = PluginState.ACTIVE
            rec.error = ""
            logger.info("插件已激活: %s v%s", rec.manifest.name, rec.manifest.version)
            return True
        except Exception as e:
            if (
                self._records.get(rec.id) is not rec
                or rec.context is not context
                or rec.state is not PluginState.ACTIVATING
            ):
                return False
            rec.error = tr("激活失败: {error}", error=e)
            logger.exception("激活插件 '%s' 失败", rec.id)
            self._stop_tree(rec.id, PluginState.ERROR)
            return False
        finally:
            rec.activation_duration_ms = (perf_counter() - started) * 1000

    def _teardown(self, rec: PluginRecord, *, state: PluginState) -> None:
        """先断开全部宿主回调，再关闭上下文并执行允许重入的资源清理。"""
        if rec.id in self._tearing_down:
            return
        self._tearing_down.add(rec.id)
        self._bus.unsubscribe_plugin(rec.id)
        self._commands.unregister_plugin(rec.id)
        self._views.unregister_plugin(rec.id)
        if self._task_runner is not None:
            self._task_runner.cancel_owner(rec.id)
        if self._operations is not None and rec.context is not None:
            try:
                self._operations.cancel_owner(rec.id)
            except Exception:
                logger.exception("取消插件 '%s' 的操作失败", rec.id)
        if self._shortcut_host is not None:
            self._shortcut_host.unregister(rec.id)
        if rec.context is not None:
            rec.context._dispose()
        if rec.configuration is not None:
            rec.configuration._close()
            rec.configuration = None
        if rec.storage is not None:
            rec.storage._close()
            rec.storage = None
        if rec.instance is not None:
            try:
                rec.instance.deactivate()
            except Exception:
                logger.exception("插件 '%s' deactivate 抛错", rec.id)
        self._contrib.remove(rec.id)
        # 移除插件注册的页面并关闭其对话框。
        if self._ui_host is not None:
            try:
                self._ui_host.cleanup_plugin(rec.id)
            except Exception:
                logger.exception("清理插件 '%s' 的 UI 失败", rec.id)
        rec.state = state
        rec.instance = None
        rec.context = None
        rec.exports = None
        if rec.loader is not None:
            rec.loader.unload()
            rec.loader = None
        self._tearing_down.discard(rec.id)

    def _stop_tree(self, plugin_id: str, state: PluginState) -> set[str]:
        """先停止全部传递消费者，再停止目标，返回之前活动的插件身份。"""
        active = set()
        for current in dependent_order(plugin_id, self._records):
            rec = self._records.get(current)
            if rec is None:
                continue
            if rec.state is PluginState.ACTIVE:
                active.add(current)
            target_state = state
            if current != plugin_id:
                target_state = (
                    PluginState.BLOCKED
                    if rec.enabled and rec.trusted
                    else PluginState.DISCOVERED
                    if rec.enabled
                    else PluginState.DISABLED
                )
                if target_state is PluginState.BLOCKED:
                    rec.error = tr("依赖插件暂不可用：{plugin_id}", plugin_id=plugin_id)
            self._teardown(rec, state=target_state)
        return active

    def _stop_all(self) -> None:
        """按反向依赖顺序停止全部插件，包括仅登记了声明的待命插件。"""
        stopped = set()
        for plugin_id in list(self._records):
            for current in dependent_order(plugin_id, self._records):
                if current not in stopped:
                    self._teardown(self._records[current], state=PluginState.DISCOVERED)
                    stopped.add(current)

    # 启动

    def activate_enabled(self) -> int:
        """登记可用插件并触发启动条件，返回当前活动插件数。"""
        self._prepare_all()
        for rec in self._records.values():
            if not rec.enabled:
                rec.state = PluginState.DISABLED
                continue
            if not rec.trusted:
                logger.info("插件 '%s' 未信任，跳过激活（待用户确认）", rec.id)
                continue
            if "onStartup" in rec.manifest.activation_events and rec.state is PluginState.READY:
                self._activate(rec, "onStartup")
        ok = sum(rec.state is PluginState.ACTIVE for rec in self._records.values())
        logger.info("插件启动激活: %d 个", ok)
        return ok

    def load_all(self) -> int:
        """发现 + 激活，一步到位（宿主启动入口）。"""
        self.discover()
        n = self.activate_enabled()
        self._notify_changed()
        return n

    # 用户操作（供管理 UI）

    def enable(self, plugin_id: str) -> bool:
        """持久化启用意愿，插件已获信任时尝试激活，并通知管理界面刷新。"""
        rec = self._records.get(plugin_id)
        if rec is None:
            return False
        rec.enabled = True
        if rec.state is PluginState.ERROR:
            rec.state, rec.error = PluginState.DISCOVERED, ""
        self._persist_record(rec)
        self.activate_enabled()
        ok = not rec.trusted or rec.state in {PluginState.READY, PluginState.ACTIVE}
        self._notify_changed()
        return ok

    def disable(self, plugin_id: str) -> bool:
        """保存停用意愿，释放插件运行资源并通知状态变更。"""
        rec = self._records.get(plugin_id)
        if rec is None:
            return False
        rec.enabled = False
        self._persist_record(rec)
        self._stop_tree(plugin_id, PluginState.DISABLED)
        self._notify_changed()
        return True

    def trust(self, plugin_id: str) -> bool:
        """用户信任插件（首次启用确认弹窗调用）。"""
        rec = self._records.get(plugin_id)
        if rec is None:
            return False
        rec.trusted = True
        rec.granted_permissions = frozenset(rec.manifest.permissions) & KNOWN_PERMISSIONS
        self._persist_record(rec)
        return True

    def reload(self, plugin_id: str) -> bool:
        """释放现有实例后重读清单，仅对仍存在且启用并受信任的插件重新激活。"""
        rec = self._records.get(plugin_id)
        if rec is None:
            return False
        active = self._stop_tree(plugin_id, PluginState.DISCOVERED)
        mf = self._scan_manifests().get(plugin_id)
        if mf is None:
            self._records.pop(plugin_id, None)
            self._prepare_all()
            self._notify_changed()
            return False
        rec = self._make_record(mf)
        self._records[plugin_id] = rec
        self.activate_enabled()
        for current in reversed(dependent_order(plugin_id, self._records)):
            if current in active:
                self._activate(self._records[current], "reload:" + plugin_id)
        ok = not rec.enabled or not rec.trusted or rec.state in {PluginState.READY, PluginState.ACTIVE}
        self._notify_changed()
        return ok

    def install_archive(self, archive_path) -> InstallResult:
        """从 ZIP、7z、RAR 安装/更新插件并登记。返回 InstallResult；失败抛 InstallError。

        首次安装或新增权限等待信任；已启用且授权仍满足的更新自动激活。
        """
        from src.plugins.runtime.installer import install_from_archive

        active = set()

        def before_replace(plugin_id: str) -> None:
            """验证包之后、替换源码之前停止旧实例及其全部消费者。"""
            active.update(self._stop_tree(plugin_id, PluginState.DISCOVERED))

        try:
            result = install_from_archive(archive_path, before_replace=before_replace)
        except Exception:
            self._prepare_all()
            for plugin_id in active:
                self._activate(self._records[plugin_id], "install-rollback")
            self._notify_changed()
            raise
        rec = self._make_record(parse_manifest(result.directory))
        self._records[result.plugin_id] = rec
        self.activate_enabled()
        for current in reversed(dependent_order(rec.id, self._records)):
            if current in active:
                self._activate(self._records[current], "update:" + rec.id)
        self._notify_changed()
        return result

    def uninstall(self, plugin_id: str) -> bool:
        """停用并删除插件目录。"""
        rec = self._records.get(plugin_id)
        if rec is None:
            return False
        self._stop_tree(plugin_id, PluginState.DISABLED)
        directory = rec.manifest.directory.resolve()
        try:
            if directory.parent != get_plugins_dir().resolve() or rec.manifest.directory.is_symlink():
                raise ValueError(tr("插件目录越出安装根目录"))
            if directory.exists():
                shutil.rmtree(directory)
        except (OSError, ValueError) as exc:
            rec.state, rec.error = PluginState.ERROR, tr("卸载插件失败：{error}", error=exc)
            self._notify_changed()
            return False
        self._records.pop(plugin_id, None)
        self._persisted.pop(plugin_id, None)
        self._save_state()
        self._prepare_all()
        self._notify_changed()
        return True

    def shutdown(self) -> None:
        """宿主退出时停用全部插件。"""
        if self._shutdown:
            return
        self._shutdown = True
        self._stop_all()
        if self._task_runner is not None:
            self._task_runner.shutdown()
        remove_listener = getattr(self._m, "remove_event_listener", None)
        if remove_listener is not None:
            remove_listener(self.on_host_event)
        self._change_listeners.clear()

    # 事件派发 / 熔断

    def on_host_event(self, event: str, **payload) -> None:
        """ModManager 观察者回调入口：转发到事件总线。"""
        self.emit(event, **payload)

    def emit(self, event: str, **payload) -> None:
        """供宿主其它子系统（如 preset 信号桥接）直接发事件。"""
        if self._shutdown:
            return
        if get_ident() != self._thread_id:
            if self._post_event is not None:
                self._post_event(partial(self.emit, event, **deepcopy(payload)))
            else:
                logger.warning("插件事件缺少主线程投递入口，已跳过：%s", event)
            return
        for rec in list(self._records.values()):
            if rec.state is PluginState.READY and "onEvent:" + event in rec.manifest.activation_events:
                self.activate(rec.id, "onEvent:" + event)
        self._bus.emit(event, **payload)

    def activate(self, plugin_id: str, reason: str = "manual") -> bool:
        """按需激活插件及其依赖；不隐式授予信任或更改启用意愿。"""
        rec = self._records.get(plugin_id)
        if rec is None:
            return False
        previous = rec.state
        result = self._activate(rec, reason)
        if rec.state is not previous:
            self._notify_changed()
        return result

    def get_exports(self, requester: str, plugin_id: str):
        """只向声明了依赖的插件提供已激活依赖的公开返回值。"""
        rec = self._records.get(requester)
        if rec is None or plugin_id not in rec.manifest.dependencies:
            raise PluginError(tr("未声明插件依赖：{plugin_id}", plugin_id=plugin_id))
        if not self.activate(plugin_id, "dependency:" + requester):
            raise PluginError(tr("依赖插件激活失败：{plugin_id}", plugin_id=plugin_id))
        return self._records[plugin_id].exports

    def create_view(self, plugin_id: str, view_id: str):
        """激活视图所属插件并隔离工厂异常，返回由图形宿主验证的控件。"""
        rec = self._records.get(plugin_id)
        if rec is None or view_id not in {item["id"] for item in rec.manifest.contributes.get("views", [])}:
            raise PluginError(tr("未声明的插件视图：{view_id}", view_id=view_id))
        if not self.activate(plugin_id, "onView:" + view_id):
            return None
        rec = self._records.get(plugin_id)
        if rec is None or rec.state is not PluginState.ACTIVE:
            return None
        context = rec.context
        factory = self._views.get(plugin_id, view_id)
        try:
            if factory is None:
                raise PluginError(tr("激活后未注册视图：{view_id}", view_id=view_id))
            return factory()
        except Exception as exc:
            if self._records.get(plugin_id) is rec and rec.context is context and rec.state is PluginState.ACTIVE:
                self._on_plugin_error(plugin_id, "view:" + view_id, exc)
            return None

    def execute_command(self, command_id: str, *args, **kwargs):
        """通过统一命令表执行插件命令，并返回处理器结果。"""
        return self._commands.execute(command_id, *args, **kwargs)

    def translate(self, plugin_id: str, source: str, /, **values) -> str:
        """使用指定插件的翻译服务；未发现插件时按原文格式化规则处理。"""
        record = self._records.get(plugin_id)
        if record is not None:
            return record.translate(source, **values)
        from src.plugin_sdk.i18n import TranslationFacade

        return TranslationFacade().t(source, **values)

    def _on_plugin_error(self, plugin_id: str, source: str, exc: BaseException) -> None:
        """停用发生事件或命令异常的插件并记录错误，隔离异常对宿主的影响。"""
        rec = self._records.get(plugin_id)
        if rec is None or plugin_id in self._tearing_down:
            return
        rec.error = tr("运行时熔断（{source}）: {error}", source=source, error=exc)
        self._stop_tree(rec.id, PluginState.ERROR)
        logger.error("插件 '%s' 已被熔断停用：%s", plugin_id, rec.error)
        self._notify_changed()

    # 查询（供 UI）

    @property
    def records(self) -> list[PluginRecord]:
        """返回当前插件记录列表的副本，记录对象仍由管理器持有。"""
        return list(self._records.values())

    @property
    def discovery_errors(self) -> dict[str, str]:
        """返回未进入运行时注册表的目录及清单错误，供管理界面诊断。"""
        return dict(self._discovery_errors)

    def get(self, plugin_id: str) -> PluginRecord | None:
        """按插件 ID 查询运行记录，不存在时返回 None。"""
        return self._records.get(plugin_id)

    @property
    def contributions(self) -> ContributionRegistry:
        """返回用于界面构建的贡献注册表。"""
        return self._contrib

    @property
    def views(self) -> ViewRegistry:
        """返回插件视图工厂注册表。"""
        return self._views

    @property
    def commands(self) -> CommandTable:
        """返回宿主共用的插件命令表。"""
        return self._commands
