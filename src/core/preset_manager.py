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
