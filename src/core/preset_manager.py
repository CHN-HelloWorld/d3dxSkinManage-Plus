"""管理各游戏的预设、启用状态和差量部署事务。

每个游戏保留一个不可删除的默认预设。Mod 启用、删除和移动操作同步更新
当前预设，删除 Mod 或类别时清理相关引用。

通过 preset_changed、preset_list_changed 和 preset_applied 信号通知
当前预设切换、预设列表变更及部署完成。
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

from src.core.game_config_store import GameConfigStore
from src.core.library_snapshot import library_mutation
from src.core.mod_identity import IdentityConflict, read_deployment_identity, validate_mod_id
from src.models.deployment import (
    DeploymentRecord,
    DeploymentState,
    PresetApplyJournal,
)
from src.models.mod import ModStatus
from src.models.preset import Preset
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_data_dir

if TYPE_CHECKING:
    from src.models.mod import Mod


@dataclass(frozen=True)
class _PresetMod:
    """提供预设规划需要的轻量 Mod 字段，并通过路径服务实时解析正文位置。"""

    mod_id: str
    node_id: str | None
    folder_name: str
    status: ModStatus
    body_path: Callable[[str], Path]

    @property
    def path(self) -> str:
        """按当前持久身份解析并返回正文路径字符串。"""
        return str(self.body_path(self.mod_id))

    @property
    def is_enabled(self) -> bool:
        """判断轻量记录的状态是否为已启用。"""
        return self.status == ModStatus.ENABLED


DEFAULT_PRESET_NAME = "默认"
# 预设版本标记用于诊断；读取时按字段校验，缺失字段使用默认值。
SCHEMA_VERSION = 2


class PresetManager(QObject):
    """预设管理器（非单例；ModManager 持有引用，UI 通过 ModManager 访问）"""

    preset_changed = Signal(str)  # game_id 当前预设切换
    preset_list_changed = Signal()  # 预设增删
    preset_applied = Signal(list)  # 切换完成，参数=受影响 mod_id

    def __init__(
        self,
        parent: QObject | None = None,
        mod_index=None,
        mod_storage=None,
    ) -> None:
        """初始化预设集合、持久与解析引用、事务钩子及修改计数，并加载预设文件。"""
        super().__init__(parent)
        self._library_epoch_lock = threading.RLock()
        self._library_epoch = 0
        self._library_mutations = 0
        self._game_store = GameConfigStore()
        self._presets: dict[str, Preset] = {}
        self._durable_refs: dict[str, dict[str, list[str]]] = {}
        self._resolved_refs: dict[str, dict[str, list[str]]] = {}
        self._presets_file: Path = get_data_dir() / "presets.json"
        self._mod_index = mod_index
        self._mod_storage = mod_storage
        self._before_plan_io = None
        self._before_reconcile = None
        self._after_plan_commit = None
        self._after_plan_rollback = None
        self._on_deployments_invalidated = None
        self._before_plan_commit = None
        self._on_runtime_rollback = None
        self._verify_configuration = None
        self._load()

    def _get_index(self):
        """返回注入的索引，尚未提供时延迟创建共享于该管理器的索引实例。"""
        if self._mod_index is None:
            from src.core.mod_index import ModIndex

            self._mod_index = ModIndex()
        return self._mod_index

    def _get_storage(self):
        """返回注入的存储服务，尚未提供时延迟创建实例。"""
        if self._mod_storage is None:
            from src.core.mod_storage import ModStorage

            self._mod_storage = ModStorage()
        return self._mod_storage

    def set_deployment_lifecycle_callbacks(
        self,
        *,
        before_io=None,
        after_commit=None,
        after_rollback=None,
        on_invalidated=None,
        before_commit=None,
        on_runtime_rollback=None,
        before_reconcile=None,
        verify_configuration=None,
    ) -> None:
        """注入主线程 INI 生命周期钩子，规划层不依赖 Qt。"""
        self._before_plan_io = before_io
        self._before_reconcile = before_reconcile
        self._after_plan_commit = after_commit
        self._after_plan_rollback = after_rollback
        self._on_deployments_invalidated = on_invalidated
        # 差量部署完成、数据库提交前执行的回调，用于保存运行时变体。
        self._before_plan_commit = before_commit
        #: 回滚时反向执行运行时补丁；返回问题列表（非空表示未能完整还原）
        self._on_runtime_rollback = on_runtime_rollback
        self._verify_configuration = verify_configuration

    def has_pending_apply(self, game_id: str = "") -> bool:
        """判断持久化预设事务是否仍占用部署状态。"""
        getter = getattr(self._get_index(), "get_journals", None)
        return bool(getter(game_id)) if callable(getter) else False

    # 持久化

    def _load(self) -> None:
        """加载预设，逐条过滤无效记录并保留其他有效项。

        JSON 无法解析或整体结构错误时备份原文件后重新初始化；
        单条无效记录写入诊断日志，未知字段保留，版本字段不阻止读取。
        """
        if not self._presets_file.exists():
            return
        try:
            with open(self._presets_file, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            self._invalidate(f"无法解析: {e}")
            return

        try:
            presets = self._parse_file(data)
        except ValueError as e:
            self._invalidate(str(e))
            return

        self._presets = presets
        for preset in presets.values():
            self._durable_refs[preset.id] = copy.deepcopy(preset.character_mods)
            preset.character_mods = {}
            self._resolve_references(preset)
        logger.info("预设已加载: %d 个", len(self._presets))

    @staticmethod
    def _parse_file(data) -> dict[str, Preset]:
        """把整份文件解析成 {id: Preset}；只有"整份读不动"才抛 ValueError。"""
        if not isinstance(data, dict):
            raise ValueError(tr("根节点是 {name__}，不是对象", name__=type(data).__name__))
        version = data.get("version")
        if version != SCHEMA_VERSION:
            logger.info(
                "预设文件 version=%r 与本程序的 %d 不同，按「缺失即默认」照常读取",
                version,
                SCHEMA_VERSION,
            )
        records = data.get("presets")
        if not isinstance(records, list):
            raise ValueError(tr("presets 字段是 {name__}，不是列表", name__=type(records).__name__))

        out: dict[str, Preset] = {}
        for record in records:
            try:
                preset = Preset.from_dict(record)
                for identities in preset.character_mods.values():
                    for identity in identities:
                        validate_mod_id(identity)
            except ValueError as e:
                logger.error("预设记录不合法，已跳过该条（其余预设不受影响）: %s；原始内容: %r", e, record)
                continue
            if preset.id in out:
                logger.error("预设 id 重复，已跳过后出现的这条: %s", preset.id)
                continue
            out[preset.id] = preset
        return out

    def _invalidate(self, reason: str) -> None:
        """整份文件判无效：改名保留原文件（用户可自行找回），预设从空开始。"""
        logger.error("预设文件无效（%s），已备份并从空开始: %s", reason, self._presets_file)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self._presets_file.with_name(f"{self._presets_file.name}.invalid-{stamp}")
        seq = 1
        while target.exists():
            target = self._presets_file.with_name(f"{self._presets_file.name}.invalid-{stamp}-{seq}")
            seq += 1
        try:
            os.replace(self._presets_file, target)
            logger.info("原预设文件已备份为: %s", target)
        except OSError as e:
            logger.error("备份无效预设文件失败: %s", e)
        self._presets = {}

    @library_mutation
    def _save(self) -> None:
        """采集必要的持久成员引用并原子保存预设文件。

        写入失败时恢复引用映射、清理临时文件并传播异常，避免内存引用被部分更新。"""
        temp_file = self._presets_file.with_name(self._presets_file.name + ".tmp")
        durable_before = self._durable_refs.copy()
        resolved_before = self._resolved_refs.copy()
        try:
            self._presets_file.parent.mkdir(parents=True, exist_ok=True)
            records = []
            for preset in self._presets.values():
                if (
                    preset.id not in self._durable_refs
                    or preset.id not in self._resolved_refs
                    or preset.character_mods != self._resolved_refs[preset.id]
                ):
                    self._capture_references(preset)
                record = {
                    "id": preset.id,
                    "name": preset.name,
                    "description": preset.description,
                    "game_id": preset.game_id,
                    "is_default": preset.is_default,
                    "character_mods": self._durable_refs[preset.id],
                    "mod_variants": {
                        mid: preset.mod_variants.get(mid)
                        for mids in self._durable_refs[preset.id].values()
                        for mid in mids
                    },
                    "created_at": preset.created_at,
                    "updated_at": preset.updated_at,
                }
                records.append(record)
            data = {
                "version": SCHEMA_VERSION,
                "presets": records,
            }
            with open(temp_file, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_file, self._presets_file)
        except Exception as e:
            self._durable_refs = durable_before
            self._resolved_refs = resolved_before
            logger.error("保存预设文件失败: %s", e)
            try:
                temp_file.unlink(missing_ok=True)
            except OSError:
                pass
            raise
