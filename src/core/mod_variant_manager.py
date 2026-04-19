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
