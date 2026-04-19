"""管理独立命名变体、配置采集和可恢复的配置命令。"""

from __future__ import annotations

import copy
import json
import os
import stat
import uuid
from collections.abc import Iterable
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.core import migoto_user_ini as user_ini
from src.core import mod_runtime_prefs as prefs
from src.core.mod_package import (
    inspect_effective_inis,
    override_path_for,
    overrides_seeded,
)
from src.core.mod_variant_service import collect
from src.core.mod_variant_store import VARIANT_STORE_LOCK, ModVariantStore
from src.core.persist_vars import (
    AttributionIndex,
    ModIniSource,
    build_attribution_index,
    declaration_fingerprint,
    resolve_variant_values,
    snapshot_variables,
)
from src.core.variant_configuration import configuration_changes, default_values
from src.core.variant_journal import RuntimePatch
from src.core.variant_operation import FileChange, IniInventoryGuard, VariantOperation, digest, read_bytes
from src.models.mod_variant import ModVariant
from src.utils import fs
from src.utils.constants import MIGOTO_DISABLED_PREFIX
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_data_dir


@dataclass
class RestoreOutcome:
    """汇总配置恢复触及的 Mod、移除键、缺失引用和错误，供部署流程决定刷新提示。"""

    applied: tuple[str, ...] = ()
    removed_keys: int = 0
    needs_reload_hint: bool = False
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = field(default_factory=tuple)
    missing: tuple[str, ...] = ()

    @property
    def touched(self) -> bool:
        """报告是否应用了目标或移除了运行时键，跳过及错误本身不算配置变更。"""
        return bool(self.applied) or self.removed_keys > 0


@dataclass
class ConfigurationIndex(AttributionIndex):
    """变量声明及产生这些声明的精确源内容。"""

    source_checks: tuple[FileChange | IniInventoryGuard, ...] = ()


class ModVariantManager:
    """管理变体文件与运行时偏好，以文件原件校验、操作日志和资源保护协调配置事务。"""

    def __init__(self, manager) -> None:
        """建立会话身份、声明缓存及事务记录，并订阅库事件以使部署上下文失效。"""
        self._manager = manager
        self._watcher = None
        self._watch_notify = None
        self._watch_context = None
        self._watch_sequence = 0
        self._last_event_sequence = 0
        self._epoch = uuid.uuid4().hex
        self._session = uuid.uuid4().hex
        self._ini_cache = {}
        self._default_cache = {}
        self._prepared = {}
        self._operations = {}
        self._recovering = False
        self._prepared_enable_context = None
        listener = getattr(manager, "add_event_listener", None)
        if callable(listener):
            listener(self._on_library_event)

    def _on_library_event(self, event, **payload):
        """根据库变化使声明缓存和监控身份失效；已准备的启用事件复用冻结上下文。"""
        prepared = self._prepared_enable_context
        if event == "mod_enabled" and prepared is not None and prepared[0] == payload.get("mod_id"):
            self._prepared_enable_context = None
            self._epoch = uuid.uuid4().hex
            self._ini_cache.pop(prepared[0], None)
            self._watch_context = (
                dict(prepared[1], deployment_generation=self._epoch) if prepared[1] is not None else None
            )
            return
        if event in {
            "mod_enabled",
            "mod_disabled",
            "mod_deleted",
            "mod_updated",
            "mod_moved",
            "mod_renamed",
            "index_rebuilt",
            "game_changed",
            "mod_imported",
        }:
            self.invalidate(payload.get("mod_id", ""))

    def invalidate(self, mod_id: str = "") -> None:
        """更新部署世代并清除指定或全部声明缓存，已有监控器时重新冻结采集上下文。"""
        self._prepared_enable_context = None
        self._epoch = uuid.uuid4().hex
        if mod_id:
            self._ini_cache.pop(mod_id, None)
        else:
            self._ini_cache.clear()
        self._watch_context = None
        if self._watcher is not None:
            self._refresh_watch_context()

    def _refresh_watch_context(self, game_id=""):
        """在数据库可访问的调用线程冻结部署身份，再为监控器设置运行时文件路径。"""
        gid = self._game_id(game_id)
        path = self.user_ini_path(gid)
        self._watch_context = (
            {
                "game_id": gid,
                "deployment_generation": self._epoch,
                "mods": tuple(
                    {"mod_id": mod.mod_id, "game_id": gid, "deployment_generation": self._deployment_generation(mod)}
                    for mod in self._capture_mods(gid)
                ),
            }
            if path is not None
            else None
        )
        if self._watcher is not None:
            return self._watcher.watch(path)
        return False

    def set_change_notify(self, cb) -> None:
        """设置文件变更通知接收者；回调会在监控线程收到冻结身份，应自行安排后续线程切换。"""
        self._watch_notify = cb

    def refresh_watch(self, game_id: str = "") -> bool:
        """恢复遗留事务后启动用户配置监控；路径不可用或恢复有问题时返回 False，读取异常继续抛出。"""
        gid = self._game_id(game_id)
        path = self.user_ini_path(gid)
        try:
            if path is None or self.recover_pending(gid):
                if self._watcher is not None:
                    self._watcher.watch(None)
                self._watch_context = None
                return False
            self.load_prefs(gid)
        except (OSError, ValueError):
            self.stop_watch()
            raise
        if self._watcher is None:
            from src.core.user_ini_watcher import UserIniWatcher

            self._watcher = UserIniWatcher(self._on_user_ini_changed)
        return self._refresh_watch_context(gid)

    def stop_watch(self) -> None:
        """停止并释放持有的文件监控器，同时丢弃其冻结上下文。"""
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        self._watch_context = None

    def _on_user_ini_changed(self) -> None:
        """在监控线程发送带递增采集世代的冻结事件，不查询数据库或直接采集配置。"""
        # 监控线程只发送冻结的身份，不查询数据库。
        context = self._watch_context
        if self._watch_notify is None or context is None:
            return
        self._watch_sequence += 1
        event = dict(
            context,
            capture_generation=self._watch_sequence,
            mods=tuple(dict(mod, capture_generation=self._watch_sequence) for mod in context["mods"]),
        )
        self._watch_notify(event)

    def _game_id(self, game_id: str = "") -> str:
        """将空游戏参数解析为管理器当前选择，显式游戏 ID 保持原值。"""
        return game_id or self._manager.current_game_id

    def _migoto_root(self, game_id: str = "") -> Path | None:
        """解析并校验游戏的加载器目录，未配置或目录不可访问时返回 None。"""
        game = self._manager._game_service.get_game(self._game_id(game_id))
        if game is None or not game.migoto_path:
            return None
        root = Path(fs.normal_path(game.migoto_path))
        return root if fs.is_dir(root) else None

    def user_ini_path(self, game_id: str = "") -> Path | None:
        """按加载器配置解析实际用户 INI 路径，无法定位时返回 None。"""
        return user_ini.resolve_user_config_path(self._migoto_root(game_id))

    def prefs_file(self, game_id: str = "") -> Path | None:
        """按游戏资源目录定位私有工作状态文件，不在此处创建或加载文件。"""
        return prefs.prefs_path(self._manager._res_dir(self._game_id(game_id)), get_data_dir())

    def has_deployed_mods(self, game_id: str = "") -> bool:
        """检查索引中是否有启用且记录部署路径的 Mod，不执行部署目录扫描。"""
        return any(
            m.is_enabled and m.deployed_path for m in self._manager._index.get_enabled_mods(self._game_id(game_id))
        )

    def _get_mod(self, mod_id):
        """按 ID 获取当前游戏条目，缺失或属于其他游戏时抛出 ValueError。"""
        mods = self._manager._index.get_mods_by_ids([mod_id])
        mod = next(iter(mods), None)
        if mod is None or mod.game_id != self._game_id():
            raise ValueError(tr("Mod 不存在或不属于当前游戏"))
        return mod

    def _capture_mods(self, game_id):
        """筛选指定游戏中启用且部署目录实际存在的 Mod，作为运行时采集范围。"""
        return [
            mod
            for mod in self._manager._index.get_enabled_mods(game_id)
            if mod.deployed_path and fs.is_dir(mod.deployed_path)
        ]

    def _deploy_root_prefix(self, game_id: str) -> str:
        """计算部署目录相对加载器根目录的规范化命名空间前缀，不在根目录内时返回空串。"""
        root = self._migoto_root(game_id)
        deployment = self._manager._storage.get_deploy_dir()
        if root is None or deployment is None:
            return ""
        try:
            relative = Path(fs.normal_path(deployment)).relative_to(root)
        except ValueError:
            return ""
        return str(relative).replace("/", "\\").strip("\\").casefold()

    def _runtime_mapping_issue(self, game_id: str) -> str:
        """检查加载器和部署目录的相对关系，返回无法映射偏好的原因，无问题时返回空串。"""
        game = self._manager._game_service.get_game(game_id)
        if game is None or not game.migoto_path:
            return tr("未配置 3DMigoto 目录，无法定位外观偏好")
        if self._migoto_root(game_id) is None:
            return tr("3DMigoto 目录不存在或无法访问：{migoto_path}", migoto_path=game.migoto_path)
        if not self._deploy_root_prefix(game_id):
            deployment = self._manager._storage.get_deploy_dir()
            return tr(
                "部署目录 {deployment} 无法相对于 3DMigoto 目录 {migoto_path} 定位外观偏好",
                deployment=deployment,
                migoto_path=game.migoto_path,
            )
        return ""

    @staticmethod
    def _implicit_prefix(mod, deploy_root_prefix: str) -> str:
        """根据实际活动目录名构造隐式变量命名空间，停放目录先还原名称，无有效名称时返回空串。"""
        if not deploy_root_prefix:
            return ""
        raw = mod.folder_name or Path(fs.normal_path(mod.path)).stem
        if mod.deployed_path:
            from src.core.mod_storage import ModStorage

            actual = Path(fs.normal_path(mod.deployed_path))
            raw = (
                ModStorage.active_name_from_parked(actual, mod.mod_id)
                if actual.name.upper().startswith(MIGOTO_DISABLED_PREFIX.upper())
                else actual.name
            )
        name = str(raw).strip()
        if not name or name.upper().startswith(MIGOTO_DISABLED_PREFIX.upper()):
            return ""
        return f"{deploy_root_prefix}\\{name}".casefold()

    def _deployment_generation(self, mod) -> str:
        """摘要会话、资源内容及部署位置，标识采集状态所对应的精确部署身份。"""
        location = self._implicit_prefix(mod, self._deploy_root_prefix(mod.game_id))
        return digest(f"{self._session}|{mod.game_id}|{mod.mod_id}|{mod.content_hash}|{location}".encode())

    def _source_signature(self, mod):
        """组合资源哈希与有效 INI 清单版本，作为声明缓存复用的校验依据。"""
        return (mod.content_hash, inspect_effective_inis(mod.path))
