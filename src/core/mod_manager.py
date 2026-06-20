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


@dataclass
class RebuildReport:
    """索引重建的结果汇总。"""

    registered: int = 0  # 重新登记的 Mod 数
    classified: int = 0  # 成功归类的数
    unclassified: int = 0  # 归为未分类的项目数。
    orphans_removed: int = 0  # 对账清理的缺失资源数，由恢复流程汇总。
    hash_mismatch: int = 0  # 内容与记录版本不一致的项目数。
    invalid_metadata: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DisablePlanEntry:
    """单个 Mod 的禁用作业单，只包含可跨线程传递的值。

    主线程预先决定停放或删除，后台任务按指令执行，不访问数据库及界面。
    """

    mod_id: str
    display_name: str
    body_path: str  # mod.path（源本体，禁用全程只读不写）
    folder_name: str
    deploy_targets: tuple[str, ...]  # _known_deploy_paths 的字符串化结果
    park: bool  # True=停放（别的预设还引用它）；False=直接移除
    source_receipt: object = None


@dataclass
class DisableStepResult:
    """一个禁用作业单的执行结果（后台线程产出，主线程消费）。"""

    mod_id: str
    ok: bool = False
    parked_path: str = ""  # 非空 = 已停放到该路径（而不是删掉）
    error: str = ""
    preflight_error: object = None
    source_receipt: object = None
    original_path: str = ""


class ModManager:
    """管理 Mod 导入、启用、禁用和删除。

    所有操作按游戏编号隔离，源资源保存在应用资源库，启用内容通过链接或副本部署到目标目录。
    """

    _instance: ModManager | None = None

    def __new__(cls) -> ModManager:
        """返回进程内共享管理器实例，首次分配时标记为尚未初始化。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """首次构造时连接索引、存储和预设生命周期，建立资源保护并恢复分类发布事务。"""
        if self._initialized:
            return
        self._initialized = True
        self._library_epoch_lock = threading.RLock()
        self._library_epoch = 0
        self._library_mutations = 0
        self._library_probe_events = None
        self._config = Config()
        self._game_service = GameService()
        self._index = ModIndex()
        self._storage = ModStorage()
        self._archive = ArchiveService()
        #: 单 Mod 变体状态门面，惰性建（见 :attr:`variants`）
        self._variants = None

        # Mod 部署目录由 UI 按「当前游戏」**显式配置的 mods_path** 下发，
        # 见 MainWindow._apply_migoto_for_current_game → set_deploy_dir。
        # 不由 3DMigoto 路径推导 `<migoto>/Mods`（那只是 d3dx.ini 的默认值，可被改掉）。

        # 预设管理器（共享 ModIndex 与 ModStorage 实例）
        from src.core.preset_manager import PresetManager

        self._preset_manager = PresetManager(
            mod_index=self._index,
            mod_storage=self._storage,
        )
        self._preset_manager.set_deployment_lifecycle_callbacks(
            before_io=self._before_preset_deployment_io,
            after_commit=self._after_preset_deployment_commit,
            after_rollback=self._after_preset_deployment_rollback,
            on_invalidated=self._on_deployments_invalidated,
            before_commit=self._before_preset_deployment_commit,
            on_runtime_rollback=self._rollback_preset_variant_patch,
            verify_configuration=lambda plan: self.variants.verify_preset_configuration(plan),
            before_reconcile=lambda gid, **kwargs: self.reconcile_deployment_locations(gid, force=True, **kwargs),
        )
        # 组合仅级联引用；命名变体和私有工作状态独立保留。

        # 插件事件观察者：解耦的回调列表，core 不依赖 Qt 或插件层。
        # PluginManager 在构造时 add_event_listener 接入；动作点经 _emit 派发。
        self._event_listeners: list = []

        # ini 双向同步器（源 override ↔ 部署副本）。启用 Mod 时 register、
        # 禁用前 reconcile_mod 拉回再 unregister、退出 stop。惰性建，避免无部署时白起 Observer。
        self._ini_sync = None
        # 部署→源 override 同步发生时的通知回调（UI 用来刷新按键面板）。
        # 在 watchdog 后台线程被调用 → UI 侧回调必须自行跨线程 marshal 到主线程。
        self._ini_sync_notify = None
        self._ini_sync_refs: dict[str, tuple[str, ...]] = {}
        # 外部删除源容器后若部署文件暂被占用，Mod 记录会作为耐久清理凭据保留；UI 据此
        # 安排短间隔重试，应用重启后首次文件系统对账也会继续处理。
        self._pending_source_cleanup = False
        # 记录已准备但尚未提交的 Mod 身份，使孤立文件清理保留对应缓存。
        # 登记由纳管服务在准备、提交和回滚时维护，后台访问须加锁。
        self._inflight_mod_ids: set[str] = set()
        self._inflight_lock = threading.Lock()
        from src.core.library_protection import LibraryProtection

        self._library_protection = LibraryProtection()
        # 启动监控和导入前恢复分类发布，未解决的凭据继续保护对应路径。
        self.category_preset_recovery_errors = ()
        if self._index._conn is not None:
            from src.core.category_preset_service import CategoryPresetService

            self.category_preset_recovery_errors = CategoryPresetService(self).recover_pending()
            if self.category_preset_recovery_errors:
                logger.error("分类预设有未完成恢复的路径：%s", "、".join(self.category_preset_recovery_errors))
        # 重建索引本轮登记的在途 id（plan 分配 → apply 落库后注销）。重建也会在入库前
        # 把预览提取到 preview_cache/<new_id>/，与导入共用上面那套豁免。
        self._rebuild_inflight_ids: set[str] = set()

    @library_mutation
    def register_inflight_mod_id(self, mod_id: str) -> None:
        """标记某 mod_id 的受管文件已写入磁盘、尚未入库（孤儿清理须豁免）。"""
        if not mod_id:
            return
        with self._inflight_lock:
            self._inflight_mod_ids.add(str(mod_id))

    def apply_local_source_events(self, events):
        """将局部源库事件交给发布层验证并更新索引，返回该批事件的发布结果。"""
        from src.core.library_publication import source_events

        return source_events(self, events)

    def protect_library_operation(self, token, *, paths=(), mod_ids=(), exclusive=False):
        """按令牌登记操作涉及的路径和 Mod，可申请独占保护以阻止冲突操作。"""
        self._library_protection.protect(token, paths=paths, mod_ids=mod_ids, exclusive=exclusive)

    def release_library_operation(self, token):
        """释放令牌对应的资源保护，允许后续扫描或操作处理这些路径。"""
        self._library_protection.release(token)

    def is_library_path_protected(self, path):
        """按当前索引路径和保护快照判断目标是否与在途操作重叠。"""
        from src.core.library_protection import path_protected, protection_snapshot

        return path_protected(self._index.paths, path, protection_snapshot(self))
