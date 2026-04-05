"""按游戏规划分类预设，事务化发布并恢复未完成操作。

每次调用使用独立 SQLite 连接；工作线程只共享管理器的保护注册表和修改计数。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from src.core.category_preset import (
    MAX_FILE_BYTES,
    CategoryPresetCancelled,
    CategoryPresetDocument,
    CategoryPresetError,
    CategoryPresetNode,
    _validate_path,
)
from src.core.category_preset_journal import (
    StagingOwnership,
    anchor_errors,
    digest,
    directory_anchors,
    inventory,
    matches,
    published_errors,
    receipt,
    remove_inventory,
    rename_exclusive,
    rollback_actions,
    safe_path,
)
from src.core.library_paths import LibraryPaths
from src.core.library_schema import directory_key, register_connection
from src.services.category_preset_file import (
    copy_category_preset_image,
    read_category_preset,
    validate_category_image,
    write_category_preset,
)
from src.utils.constants import CATEGORY_MARKER_FILE, META_DIR
from src.utils.i18n import N_
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_data_dir, resolve_stored_path


@dataclass(frozen=True)
class CategoryPresetTarget:
    """保存分类预设操作所绑定的游戏身份、显示名称及资料库根目录。"""

    game_id: str
    name: str
    name_en: str
    root: Path


@dataclass(frozen=True)
class CategoryPresetRow:
    """记录单个分类的预览比较结果、图片补全决策与本地保留信息。"""

    path: str
    name: str
    name_en: str
    node_type: str
    status: str
    image_path: str = ""
    has_image: bool = False
    fill_image: bool = False
    detail: str = ""
    local_name: str = ""
    local_node_type: str = ""
    local_image_path: str = ""


@dataclass(frozen=True)
class ImportPlan:
    """保存分类导入的比较结果、暂存凭据和目标指纹，供执行前复核。"""

    target: CategoryPresetTarget
    source_name: str
    preset_name: str
    suggested_game_name: str
    rows: tuple[CategoryPresetRow, ...]
    new_count: int
    existing_count: int
    fill_count: int
    conflict_count: int
    new_image_count: int
    can_import: bool
    document: CategoryPresetDocument
    staging: Path
    fingerprint: tuple
    stage_entries: dict


@dataclass(frozen=True)
class ExportPlan:
    """保存分类导出的节点快照、图片暂存副本、源指纹和图片问题清单。"""

    target: CategoryPresetTarget
    rows: tuple[CategoryPresetRow, ...]
    include_images: bool
    omitted_images: tuple[str, ...]
    image_count: int
    image_bytes: int
    image_issues: object
    document: CategoryPresetDocument
    staging: Path
    fingerprint: tuple
    images: object
    image_sources: tuple
    stage_entries: dict


@dataclass(frozen=True)
class ImportResult:
    """汇总分类导入数量，并携带提交后尚待恢复或清理的路径。"""

    game_id: str
    new_count: int
    existing_count: int
    fill_count: int
    new_image_count: int
    recovery_paths: tuple[str, ...] = ()
