"""Mod 管理器核心 - 按游戏隔离、角色 CRUD、预览图持久化

架构：
- 源 Mod 资源存放在 data/[game_id]/mods/[角色名]/ (自管理 library)
- 3DMigoto Mods 目录仅部署已启用的 Mod（独立副本：压缩档解压 / 目录复制）
- 启用 = 部署到 3DMigoto；禁用 = 从 3DMigoto 移除；源文件始终保留
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import stat
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from src.core.config import Config
from src.core.game_service import GameService
from src.core.library_protection import protect_mod_operation, protect_node_operation
from src.core.library_snapshot import (
    LibraryRow,
    ProbeApplyStep,
    ProbeRequest,
    ReconcileContext,
    ScanLocation,
    check_path,
    deployment_signature,
    library_mutation,
    path_key,
    protected_cache_names,
    remove_cache_entry,
    source_directory_signature,
    strict_is_dir,
)
from src.core.mod_identity import (
    IdentityConflict,
    read_deployment_identity,
    read_source_mod_id,
    validate_mod_id,
)
from src.core.mod_index import ModIndex
from src.core.mod_package import (
    MANIFEST_NAME,
    InvalidManagedMetadataError,
    build_local_manifest,
    iter_effective_inis,
    local_meta_from_mod,
    override_path_for,
    read_container_marker,
    read_sidecar,
    resolve_body_path,
    resolve_previews_dir,
    unknown_manifest_keys,
    write_container_marker,
    write_sidecar,
)
from src.core.mod_storage import ModStorage
from src.core.operation_preflight import (
    OperationPreflightError,
    access_file,
    confirm_deployment,
    confirm_metadata,
    confirm_source,
    failure,
    guarded_edit,
)
from src.core.preview_cache_index import (
    PreviewCacheIndex,
    build_preview_cache_index,
    cache_content_hash,
    capture_source_version,
    preview_cache_staging,
    publish_preview_cache,
    read_cached_previews,
)
from src.models.deployment import DeploymentRecord, DeploymentState
from src.models.mod import AgeRating, Mod
from src.models.node import Node
from src.services.archive import ArchiveService
from src.utils import fs
from src.utils.constants import (
    CATEGORY_MARKER_FILE,
    IMAGE_EXTENSIONS,
    META_DIR,
    MIGOTO_DISABLED_PREFIX,
    MOD_CONTAINER_MARKER,
    is_resident_archive,
)
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_data_dir

if TYPE_CHECKING:
    from src.core.ini_keys import KeyBinding
    from src.core.mod_variant_manager import ModVariantManager
    from src.services.gamebanana.models import GbInstallMetadata


# Windows 文件名非法字符（NTFS / FAT 共集）
_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# 分类版本标记用于诊断；读取时按字段校验，缺失字段使用默认值。
CATEGORY_SCHEMA_VERSION = 2
CATEGORY_KIND = "d3dxSkinManage-Plus.category"
_CATEGORY_NODE_TYPES = {"category", "character"}


def _sanitize_folder_name(name: str, fallback: str = "Mod") -> str:
    """将显示名转换为合法 Windows 文件夹名

    - 去除非法字符 `< > : " / \\ | ? *` 及控制字符
    - 去除尾部空格和点号（Windows 不允许）
    - 长度限制 200 字符（留余地给后缀和路径）
    - 空名回退到 fallback
    """
    if not name:
        return fallback
    cleaned = _INVALID_NAME_CHARS.sub("_", name).strip().rstrip(".")
    if not cleaned:
        return fallback
    return cleaned[:200]


def _is_valid_category_name(name: object) -> bool:
    """类别名必须已经是可直接使用的单层 Windows 目录名。"""
    return isinstance(name, str) and bool(name) and _sanitize_folder_name(name, fallback="") == name


class ResourceDirectoryMigrationError(OSError):
    """资源目录迁移未能完整回滚，调用方须使用实际位置处理配置和恢复提示。"""

    def __init__(self, actual_resource_dir: str, actual_path: Path, errors: list[str]):
        """保存仍然承载资源的目录及回滚错误，防止调用方把异常当作普通旧路径回退。"""
        self.actual_resource_dir = actual_resource_dir
        self.actual_path = str(actual_path)
        self.rollback_errors = tuple(errors)
        super().__init__(tr("本次操作失败：{reason}\n{path}", reason="; ".join(errors), path=self.actual_path))


@dataclass
class ReconcileReport:
    """统一库对账的结果报告。

    调用方依据报告发布一次界面刷新。intake_requests 是待执行计划，
    文件读写由后台任务完成，核心层只负责浅层扫描和规划。
    """

    game_id: str = ""
    trigger: str = ""
    skipped: str = ""  # 非空 = 本轮整体让开的原因（未完成事务在途等）
    claimed: int = 0  # 按 mod_id 认领（手动改名/移动）的记录数
    removed_sources: int = 0  # 源路径失效而清理掉的记录数
    disabled_deployments: list[str] = field(default_factory=list)
    intake_requests: list = field(default_factory=list)
    invalid_containers: list[str] = field(default_factory=list)
    pending_cleanup: bool = False  # 仍有清理未做完（文件占用/部署盘离线），需重试

    @property
    def changed(self) -> bool:
        """是否有已生效的库状态变化（决定要不要刷新视图）。"""
        return bool(self.claimed or self.removed_sources or self.disabled_deployments)
