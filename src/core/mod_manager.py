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

    def confirm_local_missing(self, *, purge=None):
        """复核局部发布中缺失的源资源，并按清理策略处理可确认的缺失记录。"""
        from src.core.library_publication import confirm_missing

        return confirm_missing(self, purge=purge)

    def apply_local_deployment_events(self, events):
        """将局部部署变化交给发布层复核，更新部署状态并返回发布结果。"""
        from src.core.library_publication import deployment_events

        return deployment_events(self, events)

    def get_nodes_for_ui(self, ids):
        """按输入顺序去重并读取节点，将存在节点转换为含计数和缩略图的界面数据字典。"""
        nodes = [self.get_character(nid) for nid in dict.fromkeys(ids)]
        return [
            dict(
                id=node.id,
                name=node.name,
                name_en=node.name_en,
                game_id=node.game_id,
                parent_id=node.parent_id or "",
                thumbnail=node.thumbnail,
                node_type=node.node_type,
                mod_count=node.mod_count,
                enabled_count=node.enabled_count,
            )
            for node in nodes
            if node is not None
        ]

    @library_mutation
    def unregister_inflight_mod_id(self, mod_id: str) -> None:
        """撤销在途标记（commit 后由索引接管保护，回滚后产物已删）。"""
        if not mod_id:
            return
        with self._inflight_lock:
            self._inflight_mod_ids.discard(str(mod_id))

    def inflight_mod_ids(self) -> set[str]:
        """在锁内复制尚未提交的 Mod 身份集合，供缓存清理排除正在准备的资源。"""
        with self._inflight_lock:
            return set(self._inflight_mod_ids)

    # 插件事件

    def add_event_listener(self, listener) -> None:
        """注册事件观察者: listener(event: str, **payload)。由 PluginManager 调用。"""
        if listener not in self._event_listeners:
            self._event_listeners.append(listener)

    def remove_event_listener(self, listener) -> None:
        """运行时关闭时释放观察者，可重复调用。"""
        if listener in self._event_listeners:
            self._event_listeners.remove(listener)

    def _emit(self, event: str, **payload) -> None:
        """向所有观察者派发事件（在主线程同步调用；观察者自身已熔断保护）。"""
        if self._library_probe_events is not None:
            self._library_probe_events.append((event, payload))
            return
        for listener in list(self._event_listeners):
            try:
                listener(event, **payload)
            except Exception:
                logger.exception("事件观察者处理 '%s' 抛错", event)

    @property
    def preset_manager(self):
        """暴露给 UI 层的预设管理器"""
        return self._preset_manager

    # Library 路径

    def _res_dir(self, game_id: str | None = None) -> str:
        """返回指定游戏在 data 下使用的资源目录名。

        未单独配置时使用游戏编号，资源目录重命名后更新映射；所有游戏资源路径均通过此入口解析。
        """
        gid = game_id or self.current_game_id
        return self._game_service.get_resource_dir(gid)

    def _get_library_root(self, game_id: str | None = None) -> Path:
        """获取指定游戏的 Mod library 根目录: data/[resource_dir]/mods/"""
        library = get_data_dir() / self._res_dir(game_id) / "mods"
        fs.ensure_dir(library)
        return library

    def _get_character_library(self, node_id: str, game_id: str | None = None, create: bool = True) -> Path:
        """解析指定游戏的节点或库根目录，可按需创建；节点跨游戏时抛出 ValueError。"""
        gid = game_id or self.current_game_id
        if node_id:
            row = self._index.paths._node(node_id)
            if row[1] != gid:
                raise ValueError(tr("节点不属于指定游戏"))
            path = self._index.paths.node_path(node_id)
        else:
            path = self._index.paths.roots[gid]
        if create:
            fs.ensure_dir(path)
        return path

    # 游戏隔离

    @library_mutation
    def set_game(self, game_id: str) -> None:
        """切换当前游戏"""
        self.invalidate_operation_trust()
        self._deferred_source_events = {}
        self._deferred_deployment_events = {}
        self._local_source_events = None
        # 索引打开后仍可新建游戏，通知监听者前更新资源根，供分类、图片和后台扫描解析路径。
        if self._index._conn is not None:
            self._index.refresh_paths()
        self._game_service.current_game_id = game_id
        self._index.game_id = game_id
        logger.info("已切换游戏: %s", game_id)
        self._emit("game_changed", game_id=game_id)

    @property
    def current_game_id(self) -> str:
        """读取游戏服务当前选择的游戏 ID，作为省略游戏参数时的操作范围。"""
        return self._game_service.current_game_id

    # 角色/对象 Mod 限制

    def find_character_ancestor(self, char_id: str, game_id: str | None = None) -> Node | None:
        """向上查找最近的 node_type='character' 祖先节点"""
        gid = game_id or self.current_game_id
        all_chars = self.get_characters(gid)
        char_map = {c.id: c for c in all_chars}
        current = char_id
        while current:
            c = char_map.get(current)
            if not c:
                break
            if c.node_type == "character":
                return c
            current = c.parent_id
        return None

    def get_subtree_ids(self, root_id: str, game_id: str | None = None) -> list[str]:
        """获取以 root_id 为根的所有子孙节点 ID（包括 root_id 本身）"""
        gid = game_id or self.current_game_id
        all_chars = self.get_characters(gid)
        children_map: dict[str, list[str]] = {}
        for c in all_chars:
            children_map.setdefault(c.parent_id, []).append(c.id)
        result = [root_id]
        queue = [root_id]
        while queue:
            parent = queue.pop(0)
            for child_id in children_map.get(parent, []):
                result.append(child_id)
                queue.append(child_id)
        return result

    def enforce_single_mod(self, mod_id: str) -> list[str]:
        """角色/对象下启用 Mod 时，禁用同一角色/对象子树下的其他所有 Mod。
        返回被禁用的 Mod ID 列表。"""
        mod = self._index.get_mod(mod_id)
        if not mod or not mod.node_id:
            return []
        # 查找最近的 character 祖先
        char_ancestor = self.find_character_ancestor(mod.node_id)
        if not char_ancestor:
            return []
        # 找到角色子树下所有 node_id
        subtree_ids = self.get_subtree_ids(char_ancestor.id)
        # 禁用子树中其他所有已启用的 Mod
        disabled = []
        for cid in subtree_ids:
            sibling_mods = self.get_mods(node_id=cid)
            for m in sibling_mods:
                if m.mod_id != mod_id and m.is_enabled:
                    if not self._remove_known_deployments(m, capture_changes=True):
                        logger.warning("单角色互斥时取消部署失败，保留启用: %s", m.display_name)
                        continue
                    m.disable()
                    self._persist_mod_deployment_state(m)
                    disabled.append(m.mod_id)

        # 同步到当前预设
        try:
            if disabled:
                self._preset_manager.strip_mod_ids(disabled, self.current_game_id)
        except Exception as e:
            logger.warning("preset strip_mod_ids 失败: %s", e)

        return disabled

    # 角色 CRUD

    def get_character(self, node_id: str, game_id: str | None = None) -> Node | None:
        """查询指定游戏节点及直属 Mod 计数，并解析缩略图逻辑引用；节点缺失时返回 None。"""
        gid = game_id or self.current_game_id
        row = (
            self._index._db()
            .execute(
                "SELECT n.*,COUNT(m.mod_id) AS mod_count,"
                "COALESCE(SUM(d.state='active'),0) AS enabled_count "
                "FROM nodes n LEFT JOIN mods m ON m.node_id=n.id "
                "LEFT JOIN deployment_records d ON d.mod_id=m.mod_id "
                "WHERE n.id=? AND n.game_id=? GROUP BY n.id",
                (node_id, gid),
            )
            .fetchone()
        )
        if row is None:
            return None
        node = Node.from_row(row)
        node.mod_count, node.enabled_count = row["mod_count"], row["enabled_count"]
        if node.thumbnail:
            node.thumbnail = str(self._index.paths.resolve_image(node.id, json.loads(node.thumbnail)))
        return node

    def get_characters(self, game_id: str | None = None) -> list[Node]:
        """按排序值和名称列出游戏节点，附带直属 Mod 及活动部署计数和实际缩略图路径。"""
        gid = game_id or self.current_game_id
        rows = (
            self._index._db()
            .execute(
                "SELECT n.*,COUNT(m.mod_id) AS mod_count,"
                "COALESCE(SUM(d.state='active'),0) AS enabled_count "
                "FROM nodes n LEFT JOIN mods m ON m.node_id=n.id "
                "LEFT JOIN deployment_records d ON d.mod_id=m.mod_id "
                "WHERE n.game_id=? GROUP BY n.id ORDER BY n.sort_order,n.name",
                (gid,),
            )
            .fetchall()
        )
        result = []
        for row in rows:
            node = Node.from_row(row)
            node.mod_count, node.enabled_count = row["mod_count"], row["enabled_count"]
            if node.thumbnail:
                node.thumbnail = str(self._index.paths.resolve_image(node.id, json.loads(node.thumbnail)))
            result.append(node)
        return result

    @library_mutation
    def add_character(
        self, name: str, name_en: str = "", game_id: str | None = None, parent_id: str = "", node_type: str = "category"
    ) -> Node | None:
        """同时创建分类记录、目录和标记；失败回滚记录及本次新建空目录，返回 None。"""
        gid = game_id or self.current_game_id
        if not _is_valid_category_name(name) or node_type not in _CATEGORY_NODE_TYPES:
            return None
        conn = self._index._db()
        destination = None
        created = False
        try:
            parent = self._get_character_library(parent_id, gid, create=False)
            destination = parent / name
            if fs.exists(destination):
                raise FileExistsError(destination)
            node = Node(
                id=str(uuid.uuid4()),
                game_id=gid,
                name=name,
                name_en=name_en,
                parent_id=parent_id or None,
                directory_name=name,
                node_type=node_type,
            )
            conn.execute(
                "INSERT INTO nodes(id,game_id,parent_id,directory_name,name,name_en,node_type) VALUES(?,?,?,?,?,?,?)",
                (node.id, gid, node.parent_id, name, name, name_en, node_type),
            )
            fs.ensure_dir(destination)
            created = True
            if not self._ensure_category_marker(node.id, gid):
                raise OSError(tr("类别 marker 写入失败"))
            conn.commit()
            return node
        except Exception as error:
            conn.rollback()
            if created and destination is not None:
                try:
                    marker = destination / CATEGORY_MARKER_FILE
                    if fs.is_file(marker):
                        fs.unlink(marker)
                    if fs.is_empty_dir(destination):
                        fs.rmdir(destination)
                except OSError:
                    logger.exception("恢复类别创建失败")
            logger.error("创建类别失败: %s", error)
            return None

    def _ensure_category_marker(self, node_id: str, game_id: str | None = None) -> bool:
        """由节点记录生成并校验标记，以同目录临时文件原子替换，失败返回 False 并清理临时文件。"""
        row = (
            self._index._db()
            .execute("SELECT * FROM nodes WHERE id=? AND game_id=?", (node_id, game_id or self.current_game_id))
            .fetchone()
        )
        if row is None:
            return False
        temporary = None
        try:
            payload = {
                "schema": CATEGORY_SCHEMA_VERSION,
                "kind": CATEGORY_KIND,
                "id": node_id,
                "name": row["name"],
                "name_en": row["name_en"],
                "node_type": row["node_type"],
                "sort_order": row["sort_order"],
                "thumbnail": json.loads(row["thumbnail"]) if row["thumbnail"] else None,
            }
            self._validate_category_marker(payload)
            target = self._get_character_library(node_id, game_id) / CATEGORY_MARKER_FILE
            temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
            fs.write_text(temporary, json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(fs.native_path(temporary), fs.native_path(target))
            return True
        except Exception as error:
            logger.error("写入类别 marker 失败: %s", error)
            return False
        finally:
            if temporary is not None:
                try:
                    fs.unlink(temporary, missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _read_category_marker(dir_path: str | Path) -> dict | None:
        """读取并校验分类标记；文件缺失返回 None，读取或格式无效时抛出元数据错误。"""
        marker = Path(dir_path) / CATEGORY_MARKER_FILE
        try:
            data = json.loads(fs.read_text(marker, encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise InvalidManagedMetadataError(
                tr("无法读取类别 marker: {marker}: {error}", marker=marker, error=error)
            ) from error
        return ModManager._validate_category_marker(data)

    @staticmethod
    def _validate_category_marker(data: object) -> dict:
        """校验分类身份、类型及节点内缩略图引用，补齐可选字段；无效结构抛出元数据错误。"""
        from src.core.library_schema import valid_relative

        if not isinstance(data, dict) or data.get("kind") != CATEGORY_KIND:
            raise InvalidManagedMetadataError(tr("类别 marker kind 无效"))
        if not isinstance(data.get("id"), str) or not data["id"].strip():
            raise InvalidManagedMetadataError(tr("类别 marker id 无效"))
        if not isinstance(data.get("node_type"), str) or data.get("node_type") not in _CATEGORY_NODE_TYPES:
            raise InvalidManagedMetadataError(tr("类别 marker node_type 无效"))
        if not isinstance(data.get("name"), str):
            raise InvalidManagedMetadataError(tr("类别 marker name 无效"))
        if not isinstance(data.get("name_en"), str):
            data["name_en"] = ""
        if type(data.get("sort_order")) is not int:
            data["sort_order"] = 0
        image = data.get("thumbnail")
        if image is not None and (
            not isinstance(image, dict) or image.get("scope") != "node" or not valid_relative(image.get("path"))
        ):
            raise InvalidManagedMetadataError(tr("类别 thumbnail 必须是节点内逻辑引用"))
        return data

    def _refresh_markers_from_db(self, game_id: str | None = None) -> int:
        """以 DB 当前角色树为真相源，刷新全部类别 marker。"""
        gid = game_id or self.current_game_id
        n = 0
        for c in self.get_characters(gid):
            if self._ensure_category_marker(c.id, gid):
                n += 1
        return n

    @library_mutation
    def rebuild_character_tree_from_markers(self, game_id: str | None = None) -> int:
        """通过显式审计恢复物理层级与唯一标记身份。"""
        gid = game_id or self.current_game_id
        root = self._get_character_library("", gid, create=False)
        found = []
        for directory, children, files in fs.walk(root):
            children[:] = sorted(c for c in children if not c.startswith("."))
            path = Path(directory)
            if path == root:
                continue
            if CATEGORY_MARKER_FILE not in files:
                children[:] = []
                continue
            try:
                marker = self._read_category_marker(path)
                if marker:
                    found.append((path, marker))
            except InvalidManagedMetadataError as error:
                children[:] = []
                logger.warning("节点 marker 无法核验: %s", error)
        counts = {}
        for path, marker in found:
            counts[marker["id"]] = counts.get(marker["id"], 0) + 1
        parent_ids = {root: None}
        conn = self._index._db()
        added = 0
        for path, marker in sorted(found, key=lambda pair: len(pair[0].parts)):
            identity = marker["id"]
            if counts[identity] != 1 or path.parent not in parent_ids:
                logger.warning("节点身份重复或父级未确认: %s", path)
                continue
            existing = conn.execute("SELECT game_id FROM nodes WHERE id=?", (identity,)).fetchone()
            if existing is not None and existing["game_id"] != gid:
                logger.warning("跨游戏节点身份冲突: %s", identity)
                continue
            try:
                conn.execute(
                    (
                        "INSERT INTO nodes(id,game_id,parent_id,directory_name,name,name_en,node_type,sor"
                        "t_order,thumbnail) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                        "parent_id=excluded.parent_id,directory_name=excluded.directory_name"
                    ),
                    (
                        identity,
                        gid,
                        parent_ids[path.parent],
                        path.name,
                        marker["name"],
                        marker["name_en"],
                        marker["node_type"],
                        marker["sort_order"],
                        json.dumps(marker["thumbnail"], ensure_ascii=False) if marker.get("thumbnail") else "",
                    ),
                )
                parent_ids[path] = identity
                added += existing is None
            except Exception as error:
                logger.warning("恢复节点位置失败 (%s): %s", identity, error)
        conn.commit()
        return added

    @protect_node_operation
    @library_mutation
    def update_character(self, char_id: str, name: str, name_en: str = "", node_type: str | None = None) -> bool:
        """更新分类名称和类型并同步目录及标记，失败恢复原件；提交后修复后代部署引用。"""
        if not self.check_node_operation(char_id):
            return False
        gid = self.current_game_id
        current = self.get_character(char_id, gid)
        if current is None or not _is_valid_category_name(name):
            return False
        kind = current.node_type if node_type is None else node_type
        if kind not in _CATEGORY_NODE_TYPES:
            return False
        conn = self._index._db()
        old_path = self._get_character_library(char_id, gid, create=False)
        new_path = old_path.parent / name
        moved = False
        marker = old_path / CATEGORY_MARKER_FILE
        marker_bytes = None
        try:
            marker_bytes = fs.read_bytes(marker)
            if old_path != new_path and fs.exists(new_path):
                raise FileExistsError(new_path)
            conn.execute(
                "UPDATE nodes SET name=?,name_en=?,directory_name=?,node_type=? WHERE id=?",
                (name, name_en, name, kind, char_id),
            )
            if old_path != new_path:
                os.rename(fs.native_path(old_path), fs.native_path(new_path))
                moved = True
            if not self._ensure_category_marker(char_id, gid):
                raise OSError(tr("刷新类别 marker 失败"))
            conn.commit()
        except Exception as error:
            conn.rollback()
            try:
                if moved:
                    os.rename(fs.native_path(new_path), fs.native_path(old_path))
                if marker_bytes is not None:
                    fs.write_bytes(marker, marker_bytes)
            except OSError:
                logger.exception("类别改名恢复失败")
            logger.error("类别更新失败: %s", error)
            return False
        return self._repair_deployment_links_for_subtree(char_id)

    @library_mutation
    def update_character_thumbnail(self, char_id: str, thumbnail_path: str) -> bool:
        """将外部缩略图复制为节点资产并保存逻辑引用，空路径清除选择；失败尝试恢复标记及新增文件。"""
        if not self.check_node_operation(char_id, children=False):
            return False
        conn = self._index._db()
        node = self.get_character(char_id)
        if node is None:
            return False
        copied = None
        marker = self._get_character_library(char_id, create=False) / CATEGORY_MARKER_FILE
        old_marker = None
        try:
            old_marker = fs.read_bytes(marker)
            reference = None
            if thumbnail_path:
                root = self._get_character_library(char_id, create=False)
                image = Path(thumbnail_path)
                try:
                    relative = image.relative_to(root)
                except ValueError:
                    # 将外部选择的资源保存为节点拥有的资产。
                    copied = root / META_DIR / ("thumbnail-" + uuid.uuid4().hex + image.suffix)
                    fs.ensure_dir(copied.parent)
                    shutil.copy2(fs.native_path(image), fs.native_path(copied))
                    relative = copied.relative_to(root)
                from src.core.library_schema import valid_relative

                if not valid_relative(relative.as_posix()):
                    raise ValueError(tr("缩略图路径越界"))
                reference = {"scope": "node", "path": relative.as_posix()}
            conn.execute(
                "UPDATE nodes SET thumbnail=? WHERE id=?",
                (json.dumps(reference, ensure_ascii=False) if reference else "", char_id),
            )
            if not self._ensure_category_marker(char_id):
                raise OSError(tr("缩略图 marker 写入失败"))
            conn.commit()
            return True
        except Exception as error:
            conn.rollback()
            if old_marker is not None:
                fs.write_bytes(marker, old_marker)
            if copied is not None:
                fs.unlink(copied, missing_ok=True)
            logger.error("更新缩略图失败: %s", error)
            return False

    @protect_node_operation
    @library_mutation
    def delete_character(self, char_id: str) -> bool:
        """递归删除子分类及 Mod 后清除组合引用和目录，任一步失败返回 False，已完成的子删除不回退。"""
        node = self.get_character(char_id)
        if node is None:
            return False
        if not self.check_node_operation(char_id, deleting=True):
            return False
        try:
            path = self._get_character_library(char_id, create=False)
            children = self._index._db().execute("SELECT id FROM nodes WHERE parent_id=?", (char_id,)).fetchall()
            for child in children:
                if not self.delete_character(child["id"]):
                    return False
            for mod in self._index.get_mods_by_character(char_id):
                if not self.delete_mod(mod.mod_id):
                    return False
            self._preset_manager.cascade_delete_character(char_id, self.current_game_id)
            if fs.exists(path):
                fs.rmtree(path)
            self._index._db().execute("DELETE FROM nodes WHERE id=?", (char_id,))
            self._index._db().commit()
            return True
        except Exception as error:
            self._index._db().rollback()
            logger.error("删除类别失败: %s", error)
            return False

    def get_characters_for_ui(self, game_id: str | None = None) -> list[dict]:
        """获取角色数据 (适配 UI)"""
        chars = self.get_characters(game_id)
        return [
            {
                "id": c.id,
                "name": c.name,
                "name_en": c.name_en,
                "game_id": c.game_id,
                "thumbnail": c.thumbnail,
                "mod_count": c.mod_count,
                "enabled_count": c.enabled_count,
                "parent_id": c.parent_id,
                "node_type": c.node_type,
            }
            for c in chars
        ]

    # Mod 操作

    def get_mods(self, node_id: str | None = None, game_id: str | None = None) -> list[Mod]:
        """获取 Mod 列表"""
        gid = game_id or self.current_game_id
        old_gid = self._index.game_id
        self._index.game_id = gid
        if node_id:
            result = self._index.get_mods_by_character(node_id)
        else:
            result = self._index.get_all_mods(gid)
        self._index.game_id = old_gid
        return result

    def get_mod(self, mod_id: str) -> Mod | None:
        """按全局 Mod ID 查询索引，缺失时返回 None；需要游戏隔离的调用方须另行校验归属。"""
        return self._index.get_mod(mod_id)

    def get_enabled_mod_ids(self, parent_id: str = "", game_id: str | None = None) -> list[str]:
        """查询目录子树中的已启用标识，不构造界面数据。"""
        gid = self.current_game_id if game_id is None else game_id
        if not gid:
            return []
        sql = (
            "SELECT m.mod_id FROM mods m JOIN deployment_records d ON d.mod_id=m.mod_id "
            "WHERE m.game_id=? AND d.state='active'"
        )
        params = [gid]
        if parent_id:
            node_ids = self.get_subtree_ids(parent_id, gid)
            sql += " AND m.node_id IN (" + ",".join("?" for _ in node_ids) + ")"
            params.extend(node_ids)
        cursor = self._index._db().execute(sql + " ORDER BY m.name,m.mod_id", params)
        try:
            return [row[0] for row in cursor]
        finally:
            cursor.close()

    def get_character_conflict_mods(
        self,
        character_ids,
        *,
        deploying_ids=(),
        excluding_mod_id: str = "",
    ) -> list[Mod]:
        """当前游戏角色子树中的已启用/在途兄弟，供互斥提示及禁用。"""
        return self._index.get_character_conflict_mods(
            self.current_game_id, character_ids, deploying_ids=deploying_ids, excluding_mod_id=excluding_mod_id
        )

    def get_mods_all_games(self) -> list[Mod]:
        """查询全部游戏的 Mod，供共享下载文件的安装状态判断使用。

        临时取消索引的游戏筛选，查询完成后恢复原筛选条件。
        """
        old_gid = self._index.game_id
        self._index.game_id = ""
        try:
            return self._index.get_all_mods("")
        finally:
            self._index.game_id = old_gid

    @library_mutation
    def import_mod(
        self,
        source_path: str,
        node_id: str = "",
        name: str = "",
        auto_preview: bool = True,
        *,
        gb_meta: GbInstallMetadata | None = None,
        force_age_rating: AgeRating | None = None,
        import_format: str | None = None,
        strict_format: bool | None = None,
    ) -> Mod | None:
        """通过纳管事务导入文件夹或归档，成功返回默认禁用的 Mod。

        node_id 指定目标类别，name 和 auto_preview 控制名称与预览。
        gb_meta 保存在线安装来源及版本，force_age_rating 仅覆盖默认分级。
        import_format 和 strict_format 可覆盖全局格式设置。
        """
        if not source_path:
            logger.error("import_mod 缺少 source_path 参数")
            return None
        src = Path(fs.normal_path(source_path))
        if not fs.exists(src):
            logger.error("源文件不存在: %s", src)
            return None

        from src.core.mod_intake import (
            DuplicateIntakeError,
            IntakeError,
            ModIntakeService,
        )

        service = ModIntakeService(self)
        try:
            request = service.plan_request(
                source_path=src,
                game_id=self.current_game_id,
                node_id=node_id,
                display_name=name,
                target_format=import_format,
                strict_format=strict_format,
                auto_preview=auto_preview,
                gamebanana_metadata=(gb_meta.to_dict() if gb_meta is not None else None),
                force_age_rating=(force_age_rating.value if force_age_rating else ""),
            )
            return service.commit(service.prepare(request))
        except DuplicateIntakeError as e:
            logger.warning("内容一致，已跳过: %s", e)
            return None
        except (IntakeError, IdentityConflict, InterruptedError) as e:
            logger.error("导入 Mod 失败 (%s): %s", src, e)
            return None

    def _apply_share_envelope(self, mod: Mod, manifest: dict | None, staged_previews: list[Path]) -> None:
        """用分享信封还原元数据与预览图。

        元数据采取「仅在本地为空时填充」策略，避免覆盖用户自动检测/手填的值；
        标签做合并去重；信封中的补充预览图直接写入新容器的
        ``.d3dxmeta/previews`` 后追加到 ``mod.preview_images``。
        """
        if manifest:
            sname = (manifest.get("name") or "").strip()
            # 仅当用户未自定义显示名（仍等于文件夹名）时，采用信封里的名称
            if sname and mod.name == mod.folder_name:
                mod.name = sname
            info = manifest.get("info") or {}
            if info.get("author") and not mod.info.author:
                mod.info.author = info["author"]
            if info.get("description") and not mod.info.description:
                mod.info.description = info["description"]
            if info.get("version") and not mod.info.version:
                mod.info.version = info["version"]
            if info.get("url") and not mod.info.url:
                mod.info.url = info["url"]
            if info.get("tags"):
                existing = set(mod.info.tags)
                for tag in info["tags"]:
                    if isinstance(tag, str) and tag not in existing:
                        mod.info.tags.append(tag)
                        existing.add(tag)
            ar = info.get("age_rating")
            # 仅当本地仍是默认分级 G 时采用信封值，避免覆盖用户导入时显式所选分级
            if ar and mod.info.age_rating == AgeRating.G:
                try:
                    mod.info.age_rating = AgeRating(ar)
                except ValueError:
                    logger.debug("信封 age_rating 非法，忽略: %s", ar)

            # 恢复 GameBanana 更新来源（仅当本地尚无关联时），使重新导入的 Mod 仍可检测更新
            cd = info.get("custom_data") or {}
            gb = cd.get("gamebanana") or {}
            if gb.get("mod_id") and not (mod.info.custom_data.get("gamebanana") or {}).get("mod_id"):
                mod.info.custom_data["gamebanana"] = dict(gb)

        if staged_previews:
            preview_dir = resolve_previews_dir(mod.path)
            if preview_dir is None:
                logger.error("导入信封预览图失败：Mod 未处于当前格式容器中: %s", mod.path)
                return
            preview_dir.mkdir(parents=True, exist_ok=True)
            for sp in staged_previews:
                try:
                    dst = preview_dir / sp.name
                    k = 1
                    while dst.exists():
                        dst = preview_dir / f"{sp.stem}_{k}{sp.suffix}"
                        k += 1
                    shutil.copy2(str(sp), str(dst))
                    if str(dst) not in mod.preview_images:
                        mod.preview_images.append(str(dst))
                except OSError as e:
                    logger.warning("还原分享预览图失败: %s", e)

    def report_operation_failure(self, error):
        """记录预检错误并撤销库信任，再发布带 Mod 和当前游戏身份的操作失败事件。"""
        logger.warning("%s", error)
        self.invalidate_operation_trust()
        self._emit("operation_failed", mod_id=error.mod_id, game_id=self.current_game_id, error=error)

    def invalidate_operation_trust(self):
        """撤销当前库的快速预检资格，使后续默认操作重新进行完整确认。"""
        self._operation_trusted = False

    def accept_operation_trust(self, game_id):
        """仅为仍是当前游戏的库根目录登记已验证身份，游戏已切换时忽略。"""
        if game_id != self.current_game_id:
            return
        self._operation_trusted = (game_id, str(self._index.paths.roots.get(game_id, "")))

    def require_mod_operation(self, mod_id, *, metadata=False, deployment=False, full=None, expected=None):
        """确认源身份及可选元数据和部署，返回访问凭据；完整检查后重读索引以拒绝并发游戏或路径变化。"""
        mod = self._index.get_mod(mod_id)
        if mod is None:
            raise OperationPreflightError(mod_id, "", tr("Mod 记录已不存在"))
        if full is None:
            full = getattr(self, "_operation_trusted", False) != (
                self.current_game_id,
                str(self._index.paths.roots.get(self.current_game_id, "")),
            )

        def check():
            """对冻结的源路径、身份及内容哈希执行预检，供可选的后台检查执行器调用。"""
            return confirm_source(mod.path, mod.mod_id, mod.content_hash, full=full, expected=expected)

        runner = getattr(self, "operation_check_runner", None)
        receipt = runner(check) if full and runner is not None else check()
        current = self._index.get_mod(mod_id)
        if (
            current is None
            or current.game_id != self.current_game_id
            or current.path != mod.path
            or current.content_hash != mod.content_hash
        ):
            raise OperationPreflightError(mod_id, mod.path, tr("确认期间 Mod 或游戏已变化"))
        if metadata:
            confirm_metadata(receipt, expected=mod)
        if deployment and mod.deployed_path:
            confirm_deployment(mod.mod_id, mod.deployed_path)
        return receipt

    def check_mod_operation(self, mod_id, **options):
        """返回有效预检凭据；预检失败时发布操作错误并返回 None。"""
        try:
            return self.require_mod_operation(mod_id, **options)
        except OperationPreflightError as error:
            self.report_operation_failure(error)
            return None

    def check_node_operation(self, node_id, *, children=True, deleting=False):
        """祖先移动改变子路径前检查整个子树。"""
        from src.core.operation_preflight import confirm_delete_target, file_version

        path = self._index.paths.node_path(node_id)
        try:
            for identity in self.get_subtree_ids(node_id) if children else (node_id,):
                path = self._index.paths.node_path(identity)
                try:
                    file_version(path)
                except (FileNotFoundError, NotADirectoryError):
                    if deleting:
                        continue
                    raise
                with access_file(path, write=True, directory=True):
                    data = json.loads(fs.read_text(path / CATEGORY_MARKER_FILE, encoding="utf-8"))
                    if not isinstance(data, dict) or data.get("id") != identity:
                        raise ValueError(tr("类别身份与记录不一致"))
                for mod in self.get_mods(node_id=identity) if children else ():
                    if deleting:
                        confirm_delete_target(mod)
                    elif self.check_mod_operation(mod.mod_id, metadata=True, deployment=True) is None:
                        return False
            return True
        except (OSError, ValueError) as error:
            issue = failure("", path, error)
            issue.node_id = node_id
            self.report_operation_failure(issue)
            return False

    @protect_mod_operation(failure_value=None)
    @library_mutation
    def toggle_mod(self, mod_id: str, progress_cb=None) -> Mod | None:
        """预检并固定源文件后切换启用状态，文件操作失败时报告错误并返回 None。"""
        receipt = self.check_mod_operation(mod_id, metadata=True, deployment=True)
        if receipt is None:
            return None
        try:
            with access_file(receipt.path, pin=True):
                receipt.recheck()
                return self._toggle_mod(mod_id, progress_cb)
        except OSError as error:
            self.report_operation_failure(failure(mod_id, receipt.path, error))
            return None

    def _toggle_mod(self, mod_id: str, progress_cb=None) -> Mod | None:
        """切换 Mod 启用状态

        启用 → 部署到 3DMigoto Mods 目录 (junction/symlink/copy)
        禁用 → 从 3DMigoto Mods 目录移除 (源文件不动)

        progress_cb: 可选 ``progress_cb(frac: float)``，启用压缩档时上报解压进度
            （卡片环形进度）；供后台部署线程驱动。
        """
        if self._preset_manager.has_pending_apply(self.current_game_id):
            logger.warning("预设切换期间忽略 Mod 启停请求: %s", mod_id)
            return None
        mod = self._index.get_mod(mod_id)
        if not mod:
            return None

        receipt = self.check_mod_operation(mod_id, metadata=True, deployment=True)
        if receipt is None:
            return None

        if mod.is_enabled:
            # 禁用前先采集运行时变体，再将部署 INI 同步到覆盖文件并注销同步关系。
            # 采集必须先于部署移除，否则变量声明消失后无法确认用户 INI 中的键归属。
            self.variants.collect(trigger="mod_disable")
            try:
                self._ensure_ini_sync().reconcile_mod(mod.mod_id)
            except Exception as e:
                logger.warning("禁用前 ini 删前拉回失败: %s", e)
            self._ensure_ini_sync().unregister_mod(mod.mod_id)
            parked = None
            if self._deployment_has_preset_refs(mod.mod_id, excluding_mod_id=mod.mod_id):
                active = next(iter(self._known_deploy_paths(mod, force=True)), None)
                if active is not None:
                    parked = self._storage.park_deployment(active, mod.mod_id)
            if parked is not None:
                mod.remember_deployment(parked)
            elif not self._remove_known_deployments(mod, capture_changes=True):
                logger.error("取消部署失败，Mod 保持启用: %s", mod.name)
                target = next(iter(self._known_deploy_paths(mod)), None)
                if target is not None:
                    self._register_ini_sync(mod.mod_id, mod.path, target, (mod.mod_id,))
                return None
            mod.disable()
        else:
            # 优先恢复已停放的部署，否则部署到 3DMigoto；未配置部署目录时拒绝启用。
            if self._storage.get_deploy_dir() is None:
                logger.warning("未配置「Mod 文件夹」，拒绝启用: %s", mod.name)
                return None
            deploy_path = self._restore_parked_deployment(mod)
            if deploy_path is None:
                try:
                    deploy_path = self._storage.materialize_deployment(
                        mod.path,
                        mod.mod_id,
                        mod.folder_name or Path(mod.path).stem,
                        progress_cb=progress_cb,
                    )
                except Exception as e:
                    logger.error("部署 Mod 到 3DMigoto 失败: %s", e)
                    return None
            if deploy_path is None:
                logger.error("部署 Mod 到 3DMigoto 失败: %s", mod.name)
                return None
            if self.check_mod_operation(mod_id, full=False, expected=receipt) is None:
                self._storage.park_deployment(deploy_path, mod_id)
                return None
            self._remember_deployment(mod, deploy_path)
            mod.enable()
            self._register_ini_sync(mod.mod_id, mod.path, deploy_path, (mod.mod_id,))

        self._persist_mod_deployment_state(mod)

        # 启用后恢复该 Mod 的变体：写容器 override 的 persist 初始值 + 从 d3dx_user.ini
        # 删掉对应键（留着会盖掉初始值），再把 override 推到部署副本。必须在 update_mod
        # 之后 —— 隐式 namespace 依赖 mod.deployed_path 已落库。
        if mod.is_enabled:
            if not self._restore_variants_after_enable(mod):
                return None
        else:
            self._preset_manager.sync_mod_state(mod, game_id=self.current_game_id)

        self._emit("mod_enabled" if mod.is_enabled else "mod_disabled", mod_id=mod_id, game_id=self.current_game_id)
        return mod

    # 拆分启用（后台解压 + 主线程提交）

    @property
    def variants(self) -> ModVariantManager:
        """单 Mod 变体状态门面（``d3dx_user.ini`` ↔ 容器 override ↔ mod_preferences.json）。

        惰性创建。未配 3DMigoto 路径或 resource_dir 为空时其方法全部安静 no-op。
        """
        if self._variants is None:
            from src.core.mod_variant_manager import ModVariantManager

            self._variants = ModVariantManager(self)
        return self._variants

    def _restore_variants_after_enable(self, mod: Mod) -> bool:
        """同时发布配置、成员和选择状态，启用失败时停放部署副本。"""
        if not mod.mod_id:
            return False
        try:
            registry = getattr(self, "_library_protection", None)
            owner_token = registry.current_mod_operation_token(mod.mod_id) if registry else ""
            outcome = self.variants.restore(
                [mod.mod_id], operation_token="deploy:" + mod.mod_id, owner_token=owner_token, include_members=True
            )
        except Exception as error:
            if self._ini_sync is not None:
                self._ini_sync.unregister_mod(mod.mod_id)
                self._ini_sync_refs.pop(mod.mod_id, None)
            parked = self._storage.park_deployment(mod.deployed_path, mod.mod_id)
            if parked is not None:
                mod.remember_deployment(parked)
                mod.disable()
                self._persist_mod_deployment_state(mod)
            self.variants.invalidate(mod.mod_id)
            self.report_operation_failure(
                OperationPreflightError(mod.mod_id, mod.path, tr("启用时配置未完成：") + str(error))
            )
            return False
        if outcome.needs_reload_hint:
            self._emit(
                "variant_restored",
                mod_id=mod.mod_id,
                game_id=self.current_game_id,
                applied=len(outcome.applied),
                removed_keys=outcome.removed_keys,
            )
        return True

    def needs_async_deploy(self, mod_id: str) -> bool:
        """判断启用操作是否需要后台部署，避免主线程执行耗时文件操作。"""
        mod = self._index.get_mod(mod_id)
        if not mod or mod.is_enabled or not mod.path:
            return False
        if self._storage.get_deploy_dir() is None:
            return False
        # 首次部署和停放副本恢复均由工作线程定位身份，调度时不枚举部署目录。
        return is_resident_archive(Path(mod.path))

    def _is_parked_deployment_available(self, mod: Mod) -> bool:
        """确认记录路径是可访问的停放目录，且部署标记仍属于该 Mod。"""
        if not mod.deployed_path:
            return False
        parked = Path(mod.deployed_path)
        if not parked.name.upper().startswith(MIGOTO_DISABLED_PREFIX.upper()):
            return False
        if self._storage.deployment_path_state(parked) is not True:
            return False
        marker = read_deployment_identity(parked)
        return marker is not None and marker["mod_id"] == mod.mod_id

    def _restore_parked_deployment(self, mod: Mod) -> Path | None:
        """恢复预设停放缓存；非缓存路径和离线/缺失缓存均返回 None。"""
        self.require_mod_operation(mod.mod_id, deployment=True)
        if not self._is_parked_deployment_available(mod):
            return None
        name = self._storage.active_name_from_parked(mod.deployed_path, mod.mod_id)
        target = self._storage.active_deployment_path(mod.mod_id, name)
        return self._storage.restore_parked_deployment(mod.deployed_path, target.name)

    def _deployment_has_preset_refs(
        self,
        mod_id: str,
        *,
        excluding_mod_id: str = "",
    ) -> bool:
        """检查当前游戏是否仍有组合引用该部署，可排除当前组合即将移除的指定成员。"""
        ids = {mod_id}
        current_id = self._preset_manager.get_current(self.current_game_id).id
        for preset in self._preset_manager.get_all_presets(self.current_game_id):
            refs = set(preset.enabled_mod_ids)
            if preset.id == current_id and excluding_mod_id:
                refs.discard(excluding_mod_id)
            if ids.intersection(refs):
                return True
        return False

    def _persist_mod_deployment_state(
        self,
        mod: Mod,
        *,
        commit: bool = True,
        game_id: str = "",
    ) -> None:
        """将路径及启用状态投影为部署记录，可由调用方延迟数据库提交。"""
        validate_mod_id(mod.mod_id)
        state = DeploymentState.ABSENT
        if mod.deployed_path:
            state = DeploymentState.ACTIVE if mod.is_enabled else DeploymentState.DISABLED
        self._index.upsert_deployment(
            DeploymentRecord(
                mod_id=mod.mod_id,
                state=state,
                actual_path=mod.deployed_path,
            ),
            commit=commit,
        )

    @protect_mod_operation(failure_value=None)
    @library_mutation
    def finalize_enable(
        self, mod_id: str, deploy_ok: bool, *, deploy_path: str = "", source_receipt=None, preparation=None
    ) -> Mod | None:
        """复核后台部署对应的源凭据及准备上下文，再提交启用；部署失败或文件预检失败返回 None。"""
        if not deploy_ok:
            return None
        try:
            receipt = self.require_mod_operation(mod_id, full=source_receipt is None, expected=source_receipt)
            with access_file(receipt.path, pin=True):
                receipt.recheck()
                if preparation is not None:
                    preparation.validate(self, mod_id, deploy_path)
                return self._finalize_enable(mod_id, deploy_ok, deploy_path=deploy_path, preparation=preparation)
        except OSError as error:
            self.report_operation_failure(failure(mod_id, deploy_path, error))
            return None

    def _finalize_enable(self, mod_id: str, deploy_ok: bool, *, deploy_path: str = "", preparation=None) -> Mod | None:
        """在主线程提交已完成的部署，更新索引、预设和事件。

        后台部署失败或未配置部署目录时返回 None，不改变启用状态。
        """
        mod = self._index.get_mod(mod_id)
        if not mod:
            return None
        if not deploy_ok:
            logger.error("后台部署失败，Mod 保持禁用: %s", mod.name)
            return None
        if self.check_mod_operation(mod_id, full=False) is None:
            return None
        if self._storage.get_deploy_dir() is None:
            logger.warning("未配置「Mod 文件夹」，拒绝启用: %s", mod.name)
            return None
        try:
            if deploy_path:
                target = Path(deploy_path)
                if self._storage._path_key(target.parent) != self._storage._path_key(
                    self._storage.get_deploy_dir()
                ) or target.name.upper().startswith(MIGOTO_DISABLED_PREFIX.upper()):
                    raise IdentityConflict(tr("后台部署返回了无效的启用目录：{target}", target=target))
            else:
                location = self._storage.scan_deployments(force=True).get(mod.mod_id)
                if location is None or not location.active:
                    logger.error("后台部署未产生匹配的 mod_id：%s", mod.mod_id)
                    return None
                target = location.path
            # 工作线程已完成全量清单扫描，发布前只重新验证返回的身份。
            self._remember_deployment(mod, target)
        except (OSError, IdentityConflict) as error:
            logger.error("后台部署身份校验失败：%s", error)
            return None
        mod.enable()
        # 后台部署已完成, 登记 ini 双向同步 (deploy target 由 storage 按 INV-1 推算)
        if preparation is None:
            self._persist_mod_deployment_state(mod)
            self._register_ini_sync(mod.mod_id, mod.path, target, (mod.mod_id,))
        # 异步部署提交后也须恢复变体状态，与同步启用流程保持一致。
        if preparation is not None:
            preparation.publish(self, mod)
        elif not self._restore_variants_after_enable(mod):
            return None
        self._emit("mod_enabled", mod_id=mod_id, game_id=self.current_game_id)
        return mod

    # 批量禁用：一次性预计算 + 后台文件 IO + 单事务落库

    def create_deployment_preparation(self, *, trigger: str, mod_ids=None, plan=None):
        """冻结部署前所需的配置准备上下文，供工作线程处理和提交时重新验证。"""
        from src.core.deployment_preparation import DeploymentPreparation

        return DeploymentPreparation(self, trigger=trigger, mod_ids=mod_ids, plan=plan)

    @library_mutation
    def prepare_disable_batch(self, mod_ids, *, preparation=None) -> list[DisablePlanEntry]:
        """在主线程准备整批禁用操作，返回后台可消费的作业单。

        运行变量只采集一次，预设引用预先汇总。按处理顺序更新引用集合，
        据此确定各项是停放部署还是删除，避免重复扫描和无效停放。
        """
        entries: list[DisablePlanEntry] = []
        if self._preset_manager.has_pending_apply(self.current_game_id):
            logger.warning("预设切换期间忽略批量禁用请求")
            return entries

        mods = []
        for mod_id in mod_ids:
            mod = self._index.get_mod(str(mod_id))
            if mod is not None and mod.is_enabled:
                mods.append(mod)
        if not mods:
            return entries
        receipts = {}
        for mod in mods:
            # 工作线程已计算当前源文件的摘要，提交前核对版本与身份，避免再次打开阻塞整个窗口的文件检查。
            previous = preparation.receipts.get(mod.mod_id) if preparation is not None else None
            options = dict(metadata=True, deployment=True)
            if previous is not None:
                options.update(full=False, expected=previous)
            receipt = self.check_mod_operation(mod.mod_id, **options)
            if receipt is None:
                return []
            receipts[mod.mod_id] = receipt

        # 整批部署移除前只采集一次运行时变体，保留采集时的变量归属。
        if preparation is None:
            self.variants.collect(trigger="mod_disable")
        # 同步器在**主线程**惰性建好，后台线程只用不建（_ensure_ini_sync 无锁，
        # 后台建 watchdog Observer 会和主线程抢）。
        self._ensure_ini_sync()

        gid = self.current_game_id
        current_preset = self._preset_manager.get_current(gid)
        current_refs = set(current_preset.enabled_mod_ids)
        other_refs: set[str] = set()
        for preset in self._preset_manager.get_all_presets(gid):
            if preset.id != current_preset.id:
                other_refs |= set(preset.enabled_mod_ids)

        for mod in mods:
            siblings = {mod.mod_id}
            park = bool(siblings & other_refs) or bool(siblings & (current_refs - {mod.mod_id}))
            entries.append(
                DisablePlanEntry(
                    mod_id=mod.mod_id,
                    display_name=mod.display_name,
                    body_path=str(mod.path),
                    folder_name=mod.folder_name or "",
                    deploy_targets=((mod.deployed_path,) if mod.deployed_path else ())
                    if preparation is None
                    else (
                        (str(preparation.locations[mod.mod_id].path),) if mod.mod_id in preparation.locations else ()
                    ),
                    park=park,
                    source_receipt=receipts[mod.mod_id],
                )
            )
            # 与顺序执行时 sync_mod_state 的副作用对齐：本 Mod 已不再被当前预设引用。
            current_refs.discard(mod.mod_id)
        return entries

    @library_mutation
    def disable_batch_step(self, entry: DisablePlanEntry) -> DisableStepResult:
        """在后台固定并复核源文件，执行单项禁用后返回主线程所需凭据，文件异常封装为失败结果。"""
        try:
            receipt = entry.source_receipt or confirm_source(entry.body_path, entry.mod_id)
            with access_file(receipt.path, pin=True):
                receipt.recheck()
                result = self._disable_batch_step(entry)
                receipt.recheck()
                result.source_receipt = receipt
                result.original_path = entry.deploy_targets[0] if entry.deploy_targets else ""
                return result
        except OSError as error:
            issue = failure(entry.mod_id, entry.body_path, error)
            return DisableStepResult(entry.mod_id, error=str(issue), preflight_error=issue)

    def _disable_batch_step(self, entry: DisablePlanEntry) -> DisableStepResult:
        """在工作线程执行单项禁用的文件操作。

        先保存部署 INI 的最新修改，再注销同步并停放或移除部署。
        不访问数据库、预设或界面控件，不修改源本体；同步管理器通过内部锁协调访问。
        """
        result = DisableStepResult(mod_id=entry.mod_id)
        try:
            if entry.source_receipt is not None:
                entry.source_receipt.recheck()
            else:
                confirm_source(entry.body_path, entry.mod_id)
            # 作业在队列中等待时，路径可能已被替换。同步 INI 前需确认目录仍属于目标 Mod。
            for target in entry.deploy_targets:
                state = self._storage.deployment_path_state(target)
                if state is None:
                    raise OSError(tr("部署所在磁盘/共享当前不可用：{target}", target=target))
                if state:
                    try:
                        self._storage._check_deployment_identity(target, entry.mod_id)
                    except ValueError:
                        # 身份失效后撤销旧同步关系，避免后续回调写入其他 Mod 的配置。
                        self._on_deployments_invalidated((entry.mod_id,))
                        raise
        except Exception as e:
            if isinstance(e, OperationPreflightError):
                result.preflight_error = e
            result.error = tr("部署身份无法确认：{e}", e=e)
            logger.warning("取消批量禁用 (%s): %s", entry.display_name, e)
        if not result.error:
            try:
                self._ensure_ini_sync().reconcile_mod(entry.mod_id)
            except Exception as e:
                logger.warning("禁用前 ini 删前拉回失败 (%s): %s", entry.display_name, e)
        if result.error:
            return result
        try:
            self._ensure_ini_sync().unregister_mod(entry.mod_id)
        except Exception as e:
            logger.warning("注销 ini 同步失败 (%s): %s", entry.display_name, e)
        if result.error:
            return result

        try:
            if entry.park and entry.deploy_targets:
                parked = self._storage.park_deployment(entry.deploy_targets[0], entry.mod_id)
                if parked is not None:
                    result.ok = True
                    result.parked_path = str(parked)
                    return result
                # 停放失败时执行直接移除，与 toggle_mod 的处理一致。
            for target in entry.deploy_targets:
                if not self._storage.undeploy_mod(
                    entry.body_path,
                    deploy_name=entry.folder_name,
                    capture_changes=True,
                    deploy_path=target,
                    mod_id=entry.mod_id,
                ):
                    result.error = tr("部署副本移除失败（身份变化 / 磁盘不可用 / 文件被占用 / ini 回收失败）")
                    return result
            result.ok = True
        except Exception as e:
            logger.error("禁用文件 IO 失败 (%s): %s", entry.display_name, e)
            result.error = str(e)
        return result

    @library_mutation
    def commit_disable_batch(self, results) -> list[str]:
        """在主线程批量提交已完成的禁用结果，返回实际禁用的 Mod 编号。

        共用一个数据库事务及一次预设写入；失败项目保留原状态，供后续重试。
        """
        results = list(results)
        for result in results:
            if result is not None and result.ok and result.source_receipt is not None:
                try:
                    result.source_receipt.recheck()
                except OperationPreflightError as error:
                    result.ok = False
                    result.preflight_error = error
                    result.error = str(error)
                    if result.parked_path and result.original_path:
                        try:
                            self._storage._check_deployment_identity(result.parked_path, result.mod_id)
                            if not os.path.lexists(result.original_path):
                                os.replace(result.parked_path, result.original_path)
                        except (OSError, ValueError) as rollback_error:
                            logger.error("禁用回滚失败: %s", rollback_error)
            if result is not None and result.preflight_error is not None:
                self.report_operation_failure(result.preflight_error)
        pending = [r for r in results if r is not None and r.ok]
        disabled: list[str] = []
        if pending:
            with self._index.transaction():
                for r in pending:
                    mod = self._index.get_mod(r.mod_id)
                    if mod is None:
                        continue
                    if r.parked_path:
                        mod.remember_deployment(r.parked_path)
                    else:
                        mod.forget_deployment()
                    mod.disable()
                    self._index.add_mod(mod, commit=False)
                    self._persist_mod_deployment_state(mod, commit=False)
                    disabled.append(mod.mod_id)
        if disabled:
            try:
                self._preset_manager.strip_mod_ids(disabled, self.current_game_id)
            except Exception as e:
                logger.warning("批量禁用同步预设失败: %s", e)
            for mod_id in disabled:
                self._emit("mod_disabled", mod_id=mod_id, game_id=self.current_game_id)
        return disabled

    def reconcile_missing_deployments(
        self, game_id: str | None = None, *, context: ReconcileContext | None = None
    ) -> list[str]:
        """确认物理身份后再判断部署是否消失。"""
        gid = game_id or self.current_game_id
        try:
            if self._preset_manager.has_pending_apply(gid):
                return []
            return self.reconcile_deployment_locations(gid, context=context)
        except Exception as error:
            logger.warning("部署身份对账未完成，保留现有记录：%s", error)
            return []

    @library_mutation
    def reconcile_deployment_locations(
        self,
        game_id: str | None = None,
        *,
        force: bool = False,
        context: ReconcileContext | None = None,
        locations=None,
        rows=None,
        records=None,
    ) -> list[str]:
        """刷新实际路径和状态，保留用户的预设选择。

        每个游戏共用一次目录清单；身份冲突或根目录离线时，在写入数据库前停止。
        """
        gid = game_id or self.current_game_id
        from src.core.deployment_reconcile import reconcile_deployment_records

        result = reconcile_deployment_records(
            self._index,
            self._storage,
            gid,
            force=force,
            rows=tuple(context.rows.values()) if context is not None else rows,
            locations=locations,
            records=records,
        )
        for mod in result.changed:
            if context is not None:
                context.update(mod)
            if self._ini_sync is not None:
                self._ini_sync.unregister_mod(mod.mod_id)
                self._ini_sync_refs.pop(mod.mod_id, None)
            if mod.is_enabled and gid == self.current_game_id:
                self._register_ini_sync(mod.mod_id, mod.path, mod.deployed_path, (mod.mod_id,))
            self._emit("mod_enabled" if mod.is_enabled else "mod_disabled", mod_id=mod.mod_id, game_id=gid)
        return list(result.disabled_ids)

    def _remember_deployment(self, mod: Mod, deploy_path: str | Path) -> None:
        """只为真实部署目录记录路径；无部署目录的“仅索引启用”不记录源路径。"""
        if self._storage.get_deploy_dir() is None:
            mod.forget_deployment()
            return
        marker = read_deployment_identity(deploy_path)
        if marker is None or marker["mod_id"] != mod.mod_id:
            raise IdentityConflict(tr("部署目录身份不符：{deploy_path}", deploy_path=deploy_path))
        mod.remember_deployment(deploy_path)

    def _known_deploy_paths(self, mod: Mod, *, force: bool = False) -> list[Path]:
        """按持久身份定位，不将同名的无关目录视为受管对象。"""
        locations = self._storage.scan_deployments((mod.deployed_path,) if mod.deployed_path else (), force=force)
        location = locations.get(mod.mod_id)
        return [location.path] if location else []

    def _remove_known_deployments(self, mod: Mod, *, capture_changes: bool, locations=None) -> bool:
        """移除 Mod 所有已知部署位置；任一离线/占用即保留记录等待重试。"""
        try:
            targets = (
                self._known_deploy_paths(mod, force=True)
                if locations is None
                else [locations[mod.mod_id].path]
                if mod.mod_id in locations
                else []
            )
        except (OSError, IdentityConflict) as error:
            logger.warning("部署身份扫描失败，保留清理凭据 (%s): %s", mod.mod_id, error)
            return False
        for target in targets:
            if not self._storage.undeploy_mod(
                mod.path,
                deploy_name=mod.folder_name,
                capture_changes=capture_changes,
                deploy_path=target,
                mod_id=mod.mod_id,
            ):
                return False
        mod.forget_deployment()
        return True

    def set_ini_sync_notify(self, cb) -> None:
        """注册「部署→源 override 同步」通知回调 ``cb(mod_id: str)``（UI 刷按键面板用）。

        在 **watchdog 后台线程**触发（游戏运行期把变量写回部署 ini → 同步落 override）。
        回调实现须自行把工作 marshal 回主线程（见 main_window 的 Qt 信号桥）。
        """
        self._ini_sync_notify = cb
        if self._ini_sync is not None:
            self._ini_sync.on_synced = self._on_deployment_ini_synced

    def _on_deployment_ini_synced(self, mod_id: str) -> None:
        """在同步器回调线程通知受影响的 Mod，界面接收者负责将刷新切换到主线程。"""
        if self._ini_sync_notify is None:
            return
        for mod_id in self._ini_sync_refs.get(mod_id, (mod_id,)):
            self._ini_sync_notify(mod_id)

    def _ensure_ini_sync(self):
        """惰性创建 ini 双向同步器（首个 Mod 启用时才建，避免无部署时白起 Observer）。"""
        if self._ini_sync is None:
            from src.core.ini_sync import IniSyncManager

            self._ini_sync = IniSyncManager()
            self._ini_sync.on_synced = self._on_deployment_ini_synced
        return self._ini_sync

    def _register_ini_sync(
        self,
        mod_id,
        body_path,
        deploy_target,
        mod_ids=(),
        *,
        prepared_pairs=None,
    ) -> None:
        """登记 ini 双向同步 (best-effort, 失败不阻断启用)。deploy_target 为 None/不存在则跳过。"""
        if deploy_target is None:
            return
        try:
            marker = read_deployment_identity(deploy_target)
            if marker is None or marker["mod_id"] != mod_id:
                raise IdentityConflict(tr("INI 同步的部署身份不符：{deploy_target}", deploy_target=deploy_target))
            refs = tuple(dict.fromkeys(str(mid) for mid in mod_ids if mid))
            self._ini_sync_refs[str(mod_id)] = refs or (str(mod_id),)
            deploy_root = self._storage.get_deploy_dir()
            self._ensure_ini_sync().register_mod(
                str(mod_id),
                str(body_path),
                str(deploy_target),
                source_watch_root=self._get_library_root(self.current_game_id),
                # 监控部署根目录，避免停放、恢复或迁移重建子目录后监控句柄失效。
                deploy_watch_root=deploy_root,
                prepared_pairs=prepared_pairs,
            )
        except Exception as e:
            logger.warning("登记 ini 同步失败 (%s): %s", mod_id, e)

    def _register_existing_deployments(self) -> None:
        """为当前游戏已有的启用部署恢复 INI 同步注册，跳过没有部署路径的条目。"""
        for mod in self._index.get_enabled_mods(self.current_game_id):
            if mod.deployed_path:
                self._register_ini_sync(mod.mod_id, mod.path, mod.deployed_path, (mod.mod_id,))

    @library_mutation
    def _before_preset_deployment_io(self, plan) -> None:
        """冻结变体配置并同步将停放的部署，然后解除即将变更路径的 INI 监控。"""
        # 差量部署前采集源预设的运行时变体；停放或部署会改变路径和隐式命名空间。
        self.variants.on_preset_apply_before_io(plan)
        if self._ini_sync is None:
            return
        for action in plan.actions:
            if action.action not in ("park", "restore", "materialize_active"):
                continue
            if action.action == "park":
                self._ini_sync.reconcile_mod(action.mod_id)
            self._ini_sync.unregister_mod(action.mod_id)
            self._ini_sync_refs.pop(action.mod_id, None)

    def _on_deployments_invalidated(self, deployment_ids) -> None:
        """解除失效部署的 INI 同步及引用映射，避免监控继续处理旧路径。"""
        for mod_id in deployment_ids:
            if self._ini_sync is not None:
                self._ini_sync.unregister_mod(mod_id)
            self._ini_sync_refs.pop(mod_id, None)

    def _before_preset_deployment_commit(self, plan) -> None:
        """差量部署完成、DB 提交前：一次性写回目标预设各 Mod 的变体值。"""
        outcome = self.variants.on_preset_apply_before_commit(plan)
        self._preset_variant_outcome = (plan.operation_id, outcome)

    def _rollback_preset_variant_patch(self, plan) -> tuple[str, ...]:
        """回滚/崩溃恢复时反向执行运行时补丁；返回未能还原的原因。"""
        return self.variants.on_preset_apply_rollback(plan)

    @library_mutation
    def _after_preset_deployment_commit(self, plan) -> None:
        """为新活动路径注册同步，完成关联变体事务，并按恢复结果发布加载器刷新提示。"""
        # 仅活动部署需要源对象来注册 INI 同步。
        ids = tuple(action.mod_id for action in plan.actions if action.action != "park")
        mods = {mod.mod_id: mod for mod in self._index.get_mods_by_ids(ids)}
        for action in plan.actions:
            if action.action == "park":
                continue
            representatives = [mods[action.mod_id]] if action.mod_id in mods else []
            if not representatives:
                continue
            mod = representatives[0]
            self._register_ini_sync(
                action.mod_id,
                mod.path,
                action.active_path,
                tuple(item.mod_id for item in representatives),
            )
        # 数据库与当前预设指针提交后才完成收尾。
        try:
            self.variants.on_preset_apply_after_commit(plan)
        except Exception as e:
            logger.warning("预设切换后变体采集失败: %s", e)
        result = getattr(self, "_preset_variant_outcome", None)
        if result is not None and result[0] == plan.operation_id:
            self._preset_variant_outcome = None
            outcome = result[1]
            if outcome.needs_reload_hint:
                self._emit(
                    "variant_restored",
                    mod_id="",
                    game_id=plan.game_id,
                    applied=len(outcome.applied),
                    removed_keys=outcome.removed_keys,
                )

    @library_mutation
    def _after_preset_deployment_rollback(self, plan) -> None:
        """恢复被停放后回滚的部署监控，并释放组合操作持有的变体资源保护。"""
        mods = {
            m.mod_id: m
            for m in self._index.get_mods_by_ids([action.mod_id for action in plan.actions if action.action == "park"])
        }
        for action in plan.actions:
            if action.action != "park":
                continue
            representatives = [mods[action.mod_id]] if action.mod_id in mods else []
            if not representatives:
                continue
            mod = representatives[0]
            self._register_ini_sync(
                action.mod_id,
                mod.path,
                action.active_path,
                tuple(item.mod_id for item in representatives),
            )
        self.variants.release_preset_operation(plan)

    def stop_ini_sync(self) -> None:
        """应用退出时停止 ini 双向同步 watchdog Observer。"""
        sync = self._ini_sync
        self._ini_sync = None
        self._ini_sync_refs.clear()
        try:
            if sync is not None:
                sync.stop()
        except Exception as e:
            logger.warning("停止 ini 同步失败: %s", e)

    @staticmethod
    def _remove_managed_path(path: Path) -> bool:
        """删除软件管理的文件/目录并验证结果；兼容只读文件与断链。"""
        path = Path(path)
        if not os.path.lexists(str(path)):
            return True

        def _onerror(func, failed_path, _exc_info):
            """清除删除失败路径的只读限制后重试原删除函数，重试失败继续抛出。"""
            os.chmod(failed_path, stat.S_IWRITE)
            func(failed_path)

        try:
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(str(path), onerror=_onerror)
            else:
                path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("清理 Mod 管理文件失败 (%s): %s", path, e)
            return False
        return not os.path.lexists(str(path))

    def _cleanup_mod_managed_artifacts(
        self,
        mod: Mod,
        game_id: str,
        *,
        remove_invalid_container: bool,
        defer_cache: bool = False,
    ) -> bool:
        """清除除部署/索引外的软件管理产物。

        预览缓存有独立目录作为耐久重试凭据，暂时被占用时不能阻断
        Mod 的逻辑删除；损坏的源容器仍属于库内容，删除失败时则保留索引重试。
        """
        data_root = get_data_dir() / self._res_dir(game_id)
        cache_dir = data_root / "preview_cache" / mod.mod_id
        if not defer_cache:
            self._remove_managed_path(cache_dir)

        # 仅删除“marker 仍在、本体已不存在且没有其他内容”的损坏空壳。
        # marker 失配可能只是用户在资源管理器里改了 body 名称；此时不得把仍含
        # 新名称本体的容器递归删除。
        container_ok = True
        if remove_invalid_container and mod.path:
            body = Path(mod.path)
            container: Path | None = None
            if (body.parent / MOD_CONTAINER_MARKER).is_file():
                container = body.parent
            elif (body / MOD_CONTAINER_MARKER).is_file():
                container = body
            invalid_container = False
            if container is not None:
                try:
                    invalid_container = resolve_body_path(container) is None
                except InvalidManagedMetadataError:
                    invalid_container = True
            if container is not None and invalid_container:
                try:
                    unmanaged_entries = [
                        entry
                        for entry in fs.list_children(container)
                        if entry.name not in (MOD_CONTAINER_MARKER, META_DIR)
                    ]
                except OSError as e:
                    logger.warning("无法确认损坏 Mod 容器是否为空，已保留 (%s): %s", container, e)
                    container_ok = False
                else:
                    if unmanaged_entries:
                        logger.warning(
                            "无效 Mod 容器仍含源内容，已保留而不自动删除: %s",
                            container,
                        )
                    elif not self._remove_managed_path(container):
                        container_ok = False

        return container_ok

    def _cleanup_orphaned_managed_artifacts(self, game_id: str, *, context: ReconcileContext | None = None) -> bool:
        """重试删除已无索引所有者的预览缓存。"""
        try:
            rows = tuple(context.rows.values()) if context is not None else self._index.get_library_rows(game_id)
        except Exception as e:
            logger.warning("收集受管文件所有者失败，跳过孤儿清理: %s", e)
            return False

        data_root = get_data_dir() / self._res_dir(game_id)
        cache_root = data_root / "preview_cache"
        # 在途 id 与已入库 id 同等受保护：前者的受管文件已写入磁盘、只是还没 add_mod。
        inflight = self.inflight_mod_ids()
        protected_cache_names = {row.mod_id for row in rows} | inflight

        # 保护仍被有效记录引用的预览目录，避免清理时删除正在使用的图片。
        cache_key = os.path.normcase(os.path.abspath(str(cache_root)))
        for row in rows:
            for image in row.preview_images:
                if not image:
                    continue
                image_key = os.path.normcase(os.path.abspath(image))
                try:
                    if os.path.commonpath([cache_key, image_key]) != cache_key:
                        continue
                    relative = os.path.relpath(image_key, cache_key)
                except (OSError, ValueError):
                    continue
                parts = Path(relative).parts
                if parts and parts[0] not in (".", ".."):
                    protected_cache_names.add(parts[0])

        ok = True
        removed = 0
        if not cache_root.is_dir():
            return True
        try:
            children = list(cache_root.iterdir())
        except OSError as e:
            logger.warning("枚举预览缓存目录失败 (%s): %s", cache_root, e)
            return False
        for child in children:
            if child.name in protected_cache_names:
                continue
            if self._remove_managed_path(child):
                removed += 1
            else:
                ok = False

        if removed:
            logger.info("已清理 %d 个孤儿预览缓存", removed)
        return ok

    def _purge_missing_source_mod(self, mod: Mod, game_id: str, *, locations=None, defer_cache=False) -> bool:
        """源容器被外部删除后的幂等清理事务。

        任一步失败都保留 Mod 索引记录，使其成为跨重启的耐久重试凭据；只有部署、缓存、
        全部预设引用均清理成功后才删记录并派发 ``mod_deleted``。
        """
        if not self._cleanup_missing_source_artifacts(mod, game_id, locations=locations, defer_cache=defer_cache):
            return False
        return self._finalize_missing_source_mod(mod, game_id)

    def _cleanup_missing_source_artifacts(self, mod: Mod, game_id: str, *, locations=None, defer_cache=False) -> bool:
        """在所有者线程清理物理文件，保留索引和预设选择。"""
        # 残留标记不能证明目录仍属于待删除 Mod；移除部署前重新检查，
        # 保留改名、替换、不可访问或归属其他对象的容器。
        container = Path(mod.path).parent

        def still_missing():
            """清理前再次核对库可访问、源仍缺失及残余容器身份；无法确认时返回 False。"""
            from src.core.mod_package import read_missing_container_marker

            try:
                if not strict_is_dir(self._index.paths.roots[game_id]) or check_path(mod.path).state != "missing":
                    return False
                container_state = check_path(str(container)).state
                if container_state != "exists":
                    return container_state == "missing"
                marker = read_missing_container_marker(container)
                return bool(
                    marker
                    and marker["mod_id"] == mod.mod_id
                    and marker["body"] == Path(mod.path).name
                    and marker["content_hash"] == mod.content_hash
                )
            except (OSError, ValueError):
                return False

        if not still_missing():
            return False
        if self._ini_sync is not None:
            try:
                self._ini_sync.unregister_mod(mod.mod_id)
                self._ini_sync_refs.pop(mod.mod_id, None)
            except Exception as e:
                logger.warning("外删源容器后注销 ini 同步失败 (%s): %s", mod.mod_id, e)
                return False

        # 源已不存在，部署副本属于派生数据：不再尝试把 ini 回写到消失的 override。
        try:
            if not self._remove_known_deployments(mod, capture_changes=False, locations=locations):
                logger.warning("外删源容器后部署清理待重试: %s", mod.display_name)
                return False
        except Exception as e:
            logger.warning("外删源容器后部署清理异常 (%s): %s", mod.mod_id, e)
            return False

        if not still_missing():
            return False
        if not self._cleanup_mod_managed_artifacts(
            mod, game_id, remove_invalid_container=True, defer_cache=defer_cache
        ):
            logger.warning("外删源容器后损坏空壳清理待重试: %s", mod.display_name)
            return False

        return True

    def _finalize_missing_source_mod(self, mod: Mod, game_id: str, *, preset_candidate=None) -> bool:
        """在所有者线程持久化预设，最后移除索引记录。"""
        if check_path(mod.path).state != "missing":
            return False

        try:
            if preset_candidate is None:
                self._preset_manager.cascade_delete_mod(mod.mod_id, game_id)
            else:
                if preset_candidate.mod_id != mod.mod_id or preset_candidate.game_id != game_id:
                    raise ValueError("Preset deletion candidate belongs to another Mod or game")
                self._preset_manager.commit_cascade_delete(preset_candidate)
        except Exception as e:
            logger.warning("外删源容器后预设级联清理待重试 (%s): %s", mod.mod_id, e)
            return False

        if not self._index.delete_mod(mod.mod_id):
            logger.warning("外删源容器后索引删除待重试: %s", mod.mod_id)
            return False

        # 源 Mod 已消失 → 变体偏好不再有合法恢复目标，同步清理
        self.variants.on_mod_removed(mod.mod_id, game_id)

        self._emit("mod_deleted", mod_id=mod.mod_id, game_id=game_id)
        logger.info("检测到源 Mod 容器已删除，关联产物已清零: %s", mod.display_name)
        return True

    @property
    def library_probe_stamp(self):
        """覆盖同一连接上的数据库操作、回滚、文件读写和独立预设修改。"""
        objects = (self, self._storage, self._preset_manager)
        return (self.current_game_id, self._index.change_stamp, tuple(obj._library_epoch for obj in objects))

    def library_probe_busy(self) -> bool:
        """判断库、存储、预设变更或数据库事务是否正在进行，以避免冻结不一致探测快照。"""
        return (
            any(obj._library_mutations for obj in (self, self._storage, self._preset_manager))
            or self._index.in_transaction
            or self._preset_manager.has_pending_apply(self.current_game_id)
        )

    def create_library_probe_request(self, token) -> ProbeRequest:
        """同步耗尽分批快照生成器并返回冻结探测请求，生成期间的环境变化异常向上传递。"""
        iterator = self.iter_library_probe_request(token)
        while True:
            try:
                next(iterator)
            except StopIteration as finished:
                return finished.value

    def iter_library_probe_request(self, token, *, batch_size=64):
        """在所有者线程分批采集输入，返回探测请求。"""
        batch_size = max(1, min(128, int(batch_size)))
        gid = token.game_id
        base = get_data_dir() / self._res_dir(gid)
        deploy = self._storage.get_deploy_dir()

        def version():
            """采集库世代和路径解析器及根目录映射，作为分批快照的一致性标记。"""
            paths = self._index.paths
            return (self.library_probe_stamp, id(paths), tuple(paths.roots.items()), tuple(paths.preview_roots.items()))

        stamp = version()

        def check():
            """核对分批采集期间库及路径映射未变化，变化时终止快照并抛出 RuntimeError。"""
            if version() != stamp:
                raise RuntimeError("Library changed during probe snapshot capture")

        key = (self._index._db(), gid, stamp, str(base), str(deploy) if deploy is not None else None)
        cached = getattr(self, "_library_probe_template", None)
        if not self.library_probe_busy() and cached is not None and cached[0] == key:
            return replace(cached[1], token=token, inventory=self._storage.deployment_inventory_snapshot())
        self._library_probe_template = None
        rows = yield from self._index.iter_library_rows(gid, batch_size=batch_size)
        check()
        root, _characters, locations = yield from self.iter_library_scan_layout(gid, batch_size=batch_size)
        check()
        roots = {}
        if deploy is not None:
            roots[path_key(str(deploy))] = str(deploy)
        for number, row in enumerate(rows, 1):
            if row.deployed_path:
                parent = str(Path(row.deployed_path).parent)
                roots[path_key(parent)] = parent
            if number % batch_size == 0:
                yield None
                check()
        records = []
        cursor = self._index._db().execute(
            "SELECT d.mod_id,d.actual_path,d.state,d.mod_id "
            "FROM deployment_records d JOIN mods m ON m.mod_id=d.mod_id WHERE m.game_id=?",
            (gid,),
        )
        try:
            for number, row in enumerate(cursor, 1):
                records.append(tuple(row))
                if number % batch_size == 0:
                    yield None
                    check()
        finally:
            cursor.close()
        check()
        request = ProbeRequest(
            token,
            rows,
            str(root),
            tuple(roots.values()),
            str(base / "preview_cache"),
            tuple(locations),
            self._storage.deployment_inventory_snapshot(),
            tuple(records),
        )
        if not self.library_probe_busy():
            self._library_probe_template = (key, request)
        return request

    def library_scan_layout(self, game_id: str):
        """同步生成指定游戏的库根、分类及位置拓扑，供无需分片调度的扫描调用方使用。"""
        iterator = self.iter_library_scan_layout(game_id)
        while True:
            try:
                next(iterator)
            except StopIteration as finished:
                return finished.value

    def iter_library_scan_layout(self, game_id: str, *, batch_size=64):
        """分批采集目录结构，避免在一次界面调度内解析全部节点路径。"""
        batch_size = max(1, min(128, int(batch_size)))
        root = self._index.paths.roots[game_id]
        stamp = (self.library_probe_stamp, id(self._index.paths), str(root))

        def check():
            """复核拓扑采集所依赖的库世代及根目录，变化时拒绝继续拼接快照。"""
            if (self.library_probe_stamp, id(self._index.paths), str(self._index.paths.roots[game_id])) != stamp:
                raise RuntimeError("Library changed during topology snapshot capture")

        nodes, children = [], {}
        cursor = self._index._db().execute(
            "SELECT id,parent_id,directory_name,name_en FROM nodes WHERE game_id=? ORDER BY sort_order,name", (game_id,)
        )
        try:
            for number, raw in enumerate(cursor, 1):
                nid, parent, directory, name_en = tuple(raw)
                nodes.append((nid, parent, directory, name_en))
                children.setdefault(parent or "", []).append(directory)
                if number % batch_size == 0:
                    yield None
                    check()
        finally:
            cursor.close()
        locations = []
        for number, (nid, *_rest) in enumerate(nodes, 1):
            locations.append(ScanLocation(str(self._index.paths.node_path(nid)), nid, tuple(children.get(nid, ()))))
            if number % batch_size == 0:
                yield None
                check()
        check()
        locations.append(ScanLocation(str(root), "", tuple(children.get("", ()))))
        return str(root), tuple(nodes), tuple(locations)

    def apply_library_probe_result(self, result):
        """逐项返回原子变更，令牌与批次校验由界面负责。

        未变化记录无需构造对象或检查文件。缺失观察仅筛选候选，每次修改仍须重新确认资源和身份。
        """
        from src.core.deployment_reconcile import reconcile_deployment_records

        if result.error or result.cancelled or result.token.game_id != self.current_game_id:
            return
        request = result.request
        gid = result.token.game_id
        rows = {row.mod_id: row for row in request.rows}
        missing = [row for row, check in zip(request.rows, result.checks) if check.state == "missing"]
        locations = {location.mod_id: location for location in result.inventory.locations}
        candidates = {candidate.mod_id: candidate for candidate in result.relocated}
        occupied = set()
        if candidates:
            occupied.update(
                path_key(row.path) for row, check in zip(request.rows, result.checks) if check.state != "missing"
            )
            # 提交时重新确认目标归属，避免认领已被新记录占用的路径。
            occupied.update(path_key(row.path) for row in self._index.get_library_rows(gid))
        self._storage.adopt_deployment_inventory(result.inventory)
        self._pending_source_cleanup = False

        def current(row):
            """重新读取探测条目并核对路径及游戏身份，过期或已删除时返回 None。"""
            mod = self._index.get_mod(row.mod_id)
            if (
                mod is None
                or mod.path != row.path
                or mod.mod_id != row.mod_id
                or self._index.get_mod_game_id(row.mod_id) != gid
            ):
                return None
            return mod

        for row in missing:
            candidate = candidates.get(row.mod_id)
            if candidate is None or path_key(candidate.body) in occupied:
                continue
            mod = current(row)
            if mod is None:
                continue
            if self._claim_container_move(
                mod, Path(candidate.body), candidate.node_id, game_id=gid, locations=locations
            ):
                rows[row.mod_id] = LibraryRow.from_mod(mod)
                occupied.add(path_key(mod.path))
                yield ProbeApplyStep("claim", row.mod_id, True)

        # 扫描后的外部目录改名会使删除授权失效。
        # 缓存目录签名并在自身删除后更新，不在此处重新枚举标记。
        expected_signature = result.inventory.signature
        for row in missing:
            if rows[row.mod_id] != row or row.mod_id in candidates:
                continue
            mod = current(row)
            if mod is None or check_path(mod.path).state != "missing":
                continue
            if source_directory_signature(item[0] for item in result.source_signature) != result.source_signature:
                self._pending_source_cleanup = True
                return
            if deployment_signature(request.deploy_roots) != expected_signature:
                self._pending_source_cleanup = True
                return
            if self._purge_missing_source_mod(mod, gid, locations=locations, defer_cache=True):
                rows.pop(row.mod_id, None)
                locations.pop(row.mod_id, None)
                expected_signature = deployment_signature(request.deploy_roots)
                yield ProbeApplyStep("purge", row.mod_id, True, 1)
            else:
                self._pending_source_cleanup = True
                yield ProbeApplyStep("purge", row.mod_id)

        # 复用已采集的不可变记录，避免未变化的探测在界面线程反复切片和分配元组。
        records = {record[0]: record for record in request.records}
        if request.deploy_roots:
            for row in tuple(rows.values()):
                location = locations.get(row.mod_id)
                new_path = str(location.path) if location else ""
                enabled = bool(location and location.active)
                state = "active" if enabled else "disabled" if location else "absent"
                previous_record = records.get(row.mod_id)
                if (
                    row.deployed_path == new_path
                    and row.enabled == enabled
                    and previous_record is not None
                    and previous_record[1] == new_path
                    and previous_record[2] == state
                    and previous_record[3] == row.mod_id
                ):
                    continue
                mod = current(row)
                if mod is None:
                    continue
                if location:
                    self._storage._check_deployment_identity(location.path, row.mod_id)
                elif row.deployed_path and check_path(row.deployed_path).state != "missing":
                    continue
                elif deployment_signature(request.deploy_roots) != expected_signature:
                    return
                record = self._index.get_deployment(row.mod_id)
                changes = reconcile_deployment_records(
                    self._index,
                    self._storage,
                    gid,
                    rows=(row,),
                    locations=locations,
                    records={row.mod_id: record} if record else {},
                )
                for changed in changes.changed:
                    rows[row.mod_id] = LibraryRow.from_mod(changed)
                    if self._ini_sync is not None:
                        self._ini_sync.unregister_mod(changed.mod_id)
                        self._ini_sync_refs.pop(changed.mod_id, None)
                    if changed.is_enabled:
                        self._register_ini_sync(changed.mod_id, changed.path, changed.deployed_path, (changed.mod_id,))
                    self._emit(
                        "mod_enabled" if changed.is_enabled else "mod_disabled", mod_id=changed.mod_id, game_id=gid
                    )
                yield ProbeApplyStep("deployment", row.mod_id, bool(changes.changed))

        if result.cache_candidates:
            # 枚举已在工作线程完成，此处仅查询轻量的归属和引用信息。
            protected = protected_cache_names(self._index.get_library_rows(gid), request.cache_root)
            root = path_key(request.cache_root)
            for candidate in result.cache_candidates:
                child = Path(candidate.path)
                if path_key(str(child.parent)) != root:
                    continue
                ancestors = {
                    path_key(entry.path): entry for entry in candidate.entries if entry.directory and not entry.reparse
                }
                for entry in candidate.entries:
                    entry_key = path_key(entry.path)
                    if os.path.commonpath((path_key(candidate.path), entry_key)) != path_key(candidate.path):
                        continue
                    # 删除文件期间持有在途任务锁，准备操作须先注册再写入。
                    # 文件间可让出执行，此处不递归扫描。
                    with self._inflight_lock:
                        inflight = {os.path.normcase(value) for value in self._inflight_mod_ids}
                        if os.path.normcase(child.name) in protected | inflight:
                            break
                        if not remove_cache_entry(entry, ancestors):
                            self._pending_source_cleanup = True
                    yield ProbeApplyStep("cache", child.name)

    def has_pending_source_cleanup(self) -> bool:
        """是否存在因文件占用等原因尚未完成的外部删除清理。"""
        return bool(self._pending_source_cleanup)

    @protect_mod_operation()
    @library_mutation
    def delete_mod(self, mod_id: str, *, delete_method: str = "permanent") -> bool:
        """删除 Mod，并按请求永久删除源文件或将其移入回收站。"""
        mod = self._index.get_mod(mod_id)
        if not mod:
            return False
        from src.core.operation_preflight import confirm_delete_target

        try:
            confirm_delete_target(mod)
        except OperationPreflightError as error:
            logger.warning("%s", error)
            return False
        if delete_method not in ("permanent", "recycle_bin"):
            raise ValueError(tr("未知 Mod 删除方式: {delete_method}", delete_method=delete_method))
        # Windows Shell 回收站不可靠地支持长路径。必须在注销同步和取消部署之前预检，
        # 否则用户拒绝“改为直接删除”时会留下索引仍启用、实际部署已移除的半完成状态。
        if delete_method == "recycle_bin":
            self._storage.ensure_source_recycle_compatible(mod.path)
        # 删除前注销 ini 同步 (源将被删, 无需拉回)
        if self._ini_sync is not None:
            self._ini_sync.unregister_mod(mod.mod_id)
            self._ini_sync_refs.pop(mod.mod_id, None)
        if not self._remove_known_deployments(mod, capture_changes=False):
            logger.warning("Mod 部署删除失败，保留索引以便重试: %s", mod.display_name)
            return False
        try:
            confirm_delete_target(mod)
        except OperationPreflightError as error:
            logger.warning("%s", error)
            return False
        if not self._storage.delete_source(mod.path, delete_method=delete_method):
            logger.warning("Mod 部署/源文件删除失败，保留索引以便重试: %s", mod.display_name)
            return False
        self._cleanup_mod_managed_artifacts(mod, self.current_game_id, remove_invalid_container=False)

        try:
            self._preset_manager.cascade_delete_mod(mod_id, self.current_game_id)
        except Exception as e:
            logger.warning("preset cascade_delete_mod 失败，保留索引以便重试: %s", e)
            return False
        if not self._index.delete_mod(mod_id):
            return False

        # 源 Mod 已删 → 清其变体偏好（最近状态 + 全部预设绑定）
        self.variants.on_mod_removed(mod.mod_id, self.current_game_id)

        logger.info("Mod 已删除: %s", mod.display_name)
        self._emit("mod_deleted", mod_id=mod_id, game_id=self.current_game_id)
        return True

    def get_tag_service(self):
        """使用当前游戏和索引，草稿保留自身的游戏身份。"""
        from src.core.tag_service import TagService

        def begin_check(mod_id):
            """为标签编辑取得源访问凭据和已验证元数据原件，交给草稿保存以供提交时复核。"""
            receipt = self.require_mod_operation(mod_id, metadata=True, full=False)
            return receipt, confirm_metadata(receipt)

        return TagService(self._index, begin_check=begin_check)

    @library_mutation
    def commit_tag_edit(self, draft, fields: dict):
        """批量准备独立文件工作，数据库、校验和发布仍由所有者处理。"""
        from contextlib import ExitStack
        from functools import partial

        from src.core.tag_file_batch import (
            TagFileTarget,
            check_tag_files,
            restore_tag_files,
            write_tag_files,
        )

        index = self._index
        connection = index._db()
        game_id = draft.game_id
        library_root = str(index.paths.roots.get(game_id, ""))
        full = getattr(self, "_operation_trusted", False) != (game_id, library_root)
        runner = getattr(self, "operation_check_runner", None)
        snapshots, written, node_snapshot = {}, [], {}

        def run(work):
            """通过注入执行器运行文件检查或写入，未配置执行器时在调用线程直接执行。"""
            return runner(work) if runner is not None else work()

        def read_node_rows(node_ids):
            """分批读取节点路径所需字段并按 ID 返回，限制每次查询参数数量。"""
            rows = {}
            ids = tuple(node_ids)
            for offset in range(0, len(ids), 500):
                chunk = ids[offset : offset + 500]
                for row in connection.execute(
                    "SELECT id,game_id,parent_id,directory_name FROM nodes WHERE id IN ("
                    + ",".join("?" for _ in chunk)
                    + ")",
                    chunk,
                ):
                    rows[row[0]] = tuple(row)
            return rows

        def capture_nodes(mod_ids):
            """冻结标签涉及条目的节点及全部祖先记录，节点缺失或跨游戏时中止提交。"""
            pending, captured = set(), {}
            ids = tuple(mod_ids)
            for offset in range(0, len(ids), 500):
                chunk = ids[offset : offset + 500]
                pending.update(
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT node_id FROM mods WHERE game_id=? AND mod_id IN ("
                        + ",".join("?" for _ in chunk)
                        + ")",
                        (game_id, *chunk),
                    )
                    if row[0]
                )
            while pending:
                rows = read_node_rows(pending)
                if rows.keys() != pending or any(row[1] != game_id for row in rows.values()):
                    raise OperationPreflightError(draft.mod_id, "", tr("确认期间 Mod 或游戏已变化"))
                captured.update(rows)
                pending = {row[2] for row in rows.values() if row[2]} - captured.keys()
            return captured

        def validate_environment():
            """复核索引连接、当前游戏、库根及祖先快照，防止确认期间移动资源后写入旧目标。"""
            if (
                self._index is not index
                or self.current_game_id != game_id
                or index._db() is not connection
                or index.game_id != game_id
                or str(index.paths.roots.get(game_id, "")) != library_root
            ):
                raise OperationPreflightError(draft.mod_id, "", tr("确认期间 Mod 或游戏已变化"))
            # 标签服务校验完整 Mod 记录与顺序关系，祖先记录覆盖它们继承的路径。
            if read_node_rows(node_snapshot) != node_snapshot:
                raise OperationPreflightError(draft.mod_id, "", tr("确认期间 Mod 或游戏已变化"))

        def before_write(ids):
            """在解析路径前冻结祖先，再固定并校验所有目标元数据，返回数据库写入前复核结果。"""
            nonlocal node_snapshot
            # 构造对象解析路径前先采集祖先信息。
            node_snapshot = capture_nodes(ids)
            mods = {mod.mod_id: mod for mod in index.get_mods_by_ids(ids)}
            missing = set(ids) - mods.keys()
            if missing:
                raise OperationPreflightError(sorted(missing)[0], "", tr("Mod 记录已不存在"))
            for mod in mods.values():
                if mod.game_id != game_id:
                    raise OperationPreflightError(mod.mod_id, mod.path, tr("确认期间 Mod 或游戏已变化"))
            targets = tuple(TagFileTarget.from_mod(mods[mid]) for mid in sorted(ids))
            opened = getattr(draft, "file_receipt", None)
            previous = {draft.mod_id: opened} if opened is not None else {}
            snapshots.update(run(partial(check_tag_files, targets, full=full, previous=previous, pins=pins)))
            validate_environment()

        def write_sidecars(result):
            """按标签提交结果写入需要更新的旁车文件并保留预览引用，写后再次复核环境身份。"""
            mods = [mod for mod in result.mods if mod.mod_id in result.sidecar_mod_ids]
            references = {}
            for offset in range(0, len(mods), 500):
                ids = [mod.mod_id for mod in mods[offset : offset + 500]]
                rows = index._db().execute(
                    "SELECT mod_id,preview_images_json FROM mods WHERE mod_id IN (" + ",".join("?" for _ in ids) + ")",
                    ids,
                )
                references.update((row[0], row[1]) for row in rows)
            if mods:
                targets = {mod.mod_id: TagFileTarget.from_mod(mod) for mod in result.mods if mod.mod_id in snapshots}
                writes = [(snapshots[mod.mod_id], targets[mod.mod_id], references[mod.mod_id]) for mod in mods]
                run(partial(write_tag_files, tuple(writes), written))
                validate_environment()

        with ExitStack() as pins:
            try:
                result = self.get_tag_service().commit_edit(
                    draft, fields, before_write=before_write, write_sidecars=write_sidecars
                )
            except Exception as error:
                if written:
                    restored = run(partial(restore_tag_files, tuple(written)))
                    if draft.mod_id in restored:
                        # 原子回滚会替换元数据文件，重试凭据须跟随恢复后的内容；
                        # 恢复失败时保留过期凭据并继续阻止写入。
                        draft.file_receipt = restored[draft.mod_id]
                if isinstance(error, OperationPreflightError):
                    self.report_operation_failure(error)
                raise
        for mod_id in sorted(result.sidecar_mod_ids):
            self._emit("mod_metadata_changed", mod_id=mod_id, game_id=draft.game_id)
        if draft.is_dirty:
            self._emit("tags_changed", affected_ids=sorted(result.affected_mod_ids), game_id=draft.game_id)
        return result

    @protect_mod_operation()
    @library_mutation
    @guarded_edit
    def update_mod(self, mod: Mod) -> bool:
        """同步更新 Mod 元数据文件和索引。

        编辑、排序和更新操作共用此入口，两种存储须同时成功，索引失败时恢复元数据原文。
        """
        receipt = self.check_mod_operation(mod.mod_id, metadata=True)
        if receipt is None:
            return False
        if str(Path(mod.path)) != receipt.path:
            self.report_operation_failure(OperationPreflightError(mod.mod_id, mod.path, tr("编辑目标位置已变化")))
            return False
        return self._commit_mod_metadata(mod)

    def _commit_mod_metadata(self, mod):
        """调用方验证事务来源后持久化元数据。"""
        sidecar = Path(mod.path).parent / META_DIR / MANIFEST_NAME
        try:
            previous = sidecar.read_bytes() if sidecar.exists() else None
        except OSError as error:
            logger.error("读取原元数据失败 mod=%s path=%s reason=%s", mod.mod_id, sidecar, error)
            return False
        if not self._write_mod_sidecar(mod):
            return False
        ok = self._index.update_mod(mod)
        if not ok:
            try:
                if previous is None:
                    sidecar.unlink(missing_ok=True)
                else:
                    temporary = sidecar.with_name(".info-restore-" + uuid.uuid4().hex + ".tmp")
                    temporary.write_bytes(previous)
                    os.replace(temporary, sidecar)
            except OSError as error:
                logger.error("恢复元数据失败 mod=%s path=%s reason=%s", mod.mod_id, sidecar, error)
        if ok:
            self._emit(
                "mod_metadata_changed",
                mod_id=mod.mod_id,
                game_id=self._index.get_mod_game_id(mod.mod_id) or self.current_game_id,
            )
        return ok

    @protect_mod_operation(failure_value=None)
    @library_mutation
    def rename_mod(self, mod_id: str, new_display_name: str, sync_folder: bool = True) -> Mod | None:
        """修改显示名，并按需同步物理目录名称。

        显示名可重复；物理名称冲突时追加数字后缀。成功返回更新后的 Mod，失败返回 None。
        """
        mod = self._index.get_mod(mod_id)
        if not mod:
            return None

        if self.check_mod_operation(mod_id, metadata=True, deployment=True) is None:
            return None
        new_display_name = (new_display_name or "").strip()
        if not new_display_name:
            return None

        # 仅修改显示名
        if not sync_folder:
            mod.name = new_display_name
            return mod if self.update_mod(mod) else None

        # 同步物理目录
        old_path = Path(mod.path)
        if not old_path.exists():
            # 物理路径不存在，只能更新显示名，不能改物理目录
            logger.warning("Mod 物理路径不存在，仅更新显示名: %s", mod.path)
            mod.name = new_display_name
            return mod if self.update_mod(mod) else None

        if fs.is_file(old_path.parent / MOD_CONTAINER_MARKER):
            return self._rename_managed_mod(mod, new_display_name)

        return None  # 未受管的源文件须先导入再编辑。

    def _rename_managed_mod(self, mod: Mod, new_display_name: str) -> Mod | None:
        """同步重命名容器、本体、标记、元数据及索引路径。"""
        from src.core.mod_package import restore_container_marker

        try:
            expected_identity = validate_mod_id(mod.mod_id)
            if read_source_mod_id(mod.path) != expected_identity:
                raise IdentityConflict(tr("源 Mod 身份与索引不一致"))
        except (IdentityConflict, OSError) as error:
            logger.warning("改名前源 Mod 身份校验失败，已取消改名: %s", error)
            return None

        original_mod = copy.deepcopy(mod)
        old_body = Path(mod.path)
        old_container = old_body.parent
        parent = old_container.parent
        is_arc = fs.is_file(old_body) and is_resident_archive(old_body)
        suffix = old_body.suffix if is_arc else ""
        base = _sanitize_folder_name(
            new_display_name,
            fallback=mod.folder_name or "Mod",
        )
        target_container = parent / base
        counter = 1
        while target_container.exists() and target_container != old_container:
            target_container = parent / f"{base}_{counter}"
            counter += 1
        if target_container == old_container:
            mod.name = new_display_name
            return mod if self.update_mod(mod) else None
        target_body = target_container / f"{target_container.name}{suffix}"

        marker_path = old_container / MOD_CONTAINER_MARKER
        sidecar_path = old_container / META_DIR / MANIFEST_NAME
        try:
            marker_bytes = marker_path.read_bytes()
            snapshot = json.loads(marker_bytes)
            if not isinstance(snapshot, dict):
                raise IdentityConflict(tr("读取改名快照时源 Mod 标记损坏"))
            snapshot_identity = validate_mod_id(snapshot.get("mod_id"))
            if snapshot_identity != expected_identity:
                raise IdentityConflict(tr("读取改名快照时源 Mod 身份发生变化"))
            sidecar_bytes = sidecar_path.read_bytes()
        except (OSError, ValueError) as error:
            logger.error("读取容器元数据快照失败，已取消改名: %s", error)
            return None

        was_enabled = mod.is_enabled
        if was_enabled:
            try:
                if not self._remove_known_deployments(mod, capture_changes=True):
                    logger.warning("改名前撤部署失败，已取消改名: %s", mod.display_name)
                    return None
            except Exception as e:
                logger.warning("改名前撤部署异常，已取消改名: %s", e)
                return None

        moved_old_body = target_container / old_body.name
        db_updated = False

        def _rollback() -> None:
            """尽力恢复容器、标记、旁车及内存记录；恢复启用部署前重新核对源身份，失败记录日志。"""
            try:
                if target_container.exists():
                    current_body = target_body if target_body.exists() else moved_old_body
                    restored_body = target_container / old_body.name
                    if current_body.exists() and current_body != restored_body:
                        current_body.rename(restored_body)
                    # 即使标记因身份被重新分配而不能恢复，也恢复原有索引路径。
                    restored_container = target_container
                    if not old_container.exists():
                        target_container.rename(old_container)
                        restored_container = old_container
                    restore_container_marker(restored_container, marker_bytes)
                    restored_sidecar = restored_container / META_DIR / MANIFEST_NAME
                    restored_sidecar.parent.mkdir(parents=True, exist_ok=True)
                    restored_sidecar.write_bytes(sidecar_bytes)
                elif old_container.exists():
                    restore_container_marker(old_container, marker_bytes)
                    restored_sidecar = old_container / META_DIR / MANIFEST_NAME
                    restored_sidecar.parent.mkdir(parents=True, exist_ok=True)
                    restored_sidecar.write_bytes(sidecar_bytes)
            except (OSError, IdentityConflict) as rollback_error:
                logger.error("当前容器改名回滚失败: %s", rollback_error)

            mod.__dict__.clear()
            mod.__dict__.update(copy.deepcopy(original_mod.__dict__))
            if db_updated:
                self._index.update_mod(original_mod)
            if was_enabled:
                try:
                    if read_source_mod_id(original_mod.path) != expected_identity:
                        raise IdentityConflict(tr("回滚后的源 Mod 身份已变更，拒绝恢复部署"))
                    restored = self._storage.deploy_mod(
                        original_mod.path,
                        deploy_name=original_mod.folder_name,
                    )
                    if restored is not None:
                        self._remember_deployment(mod, restored)
                except Exception as deploy_error:
                    logger.warning("改名回滚后恢复部署失败: %s", deploy_error)

        try:
            old_container.rename(target_container)
            if moved_old_body != target_body:
                moved_old_body.rename(target_body)
            if not write_container_marker(
                target_container,
                target_body.name,
                mod.content_hash,
                mod_id=expected_identity,
                imported_at=mod.imported_at,
            ):
                raise OSError(tr("写入改名后的容器 marker 失败"))
        except Exception as e:
            logger.error("当前容器改名失败，正在回滚: %s", e)
            _rollback()
            return None

        old_body_prefix = str(old_body)
        old_container_prefix = str(old_container)
        new_body_prefix = str(target_body)
        new_container_prefix = str(target_container)
        updated_previews: list[str] = []
        for preview in mod.preview_images:
            if preview.startswith(old_body_prefix):
                updated_previews.append(
                    preview.replace(old_body_prefix, new_body_prefix, 1),
                )
            elif preview.startswith(old_container_prefix):
                updated_previews.append(
                    preview.replace(old_container_prefix, new_container_prefix, 1),
                )
            else:
                updated_previews.append(preview)
        mod.preview_images = updated_previews
        mod.name = new_display_name
        mod.folder_name = target_container.name
        mod.path = str(target_body)

        if not self._write_mod_sidecar(mod):
            logger.error("改名后的 sidecar 写入失败，正在回滚: %s", target_container)
            _rollback()
            return None
        if not self._index.update_mod(mod):
            logger.error("改名后的索引写入失败，正在回滚: %s", target_container)
            _rollback()
            return None
        db_updated = True

        try:
            if read_source_mod_id(mod.path) != expected_identity:
                raise IdentityConflict(tr("改名后的源 Mod 身份已变更，拒绝重新部署"))
        except (IdentityConflict, OSError) as error:
            logger.error("改名身份校验失败，正在回滚: %s", error)
            _rollback()
            return None

        if was_enabled:
            try:
                deployed = self._storage.deploy_mod(
                    mod.path,
                    deploy_name=mod.folder_name,
                )
                if deployed is None:
                    mod.disable()
                    logger.warning("改名后重新部署失败，已置为禁用: %s", mod.path)
                else:
                    self._remember_deployment(mod, deployed)
            except Exception as e:
                mod.disable()
                logger.warning("改名后重新部署异常: %s", e)

        if was_enabled:
            self._persist_mod_deployment_state(mod)
        logger.info(
            "Mod 当前容器已重命名: %s -> %s (folder=%s)",
            mod.mod_id[:8],
            new_display_name,
            mod.folder_name,
        )
        self._emit(
            "mod_metadata_changed",
            mod_id=mod.mod_id,
            game_id=self._index.get_mod_game_id(mod.mod_id) or self.current_game_id,
        )
        return mod

    @library_mutation
    @guarded_edit
    def save_preview(self, mod_id: str, image_path: str) -> bool:
        """将用户补充预览保存到容器元数据目录。

        补充图随容器复制，本体及内容哈希保持不变。数据库保存相对引用，
        读取时解析为运行路径；非容器源须先通过导入规范化。
        """
        from src.core.operation_preflight import file_version

        mod = self._index.get_mod(mod_id)
        if not mod:
            return False
        if self.check_mod_operation(mod_id, metadata=True, deployment=True) is None:
            return False

        src = Path(image_path)
        if not src.exists():
            self.report_operation_failure(failure(mod_id, src, FileNotFoundError(str(src))))
            return False

        # 预览图存入容器级 .d3dxmeta/previews/，随容器移动并保持可移植性。
        prev_dir = resolve_previews_dir(mod.path) if mod.path else None
        if prev_dir is None:
            logger.error("保存预览图失败：Mod 未处于当前格式容器中: %s", mod.path)
            return False
        file_version(prev_dir.parent)
        created_dir = not prev_dir.exists()
        if not created_dir:
            file_version(prev_dir)
        prev_dir.mkdir(exist_ok=True)
        dest = prev_dir / src.name
        k = 1
        while dest.exists() and str(dest) not in mod.preview_images:
            dest = prev_dir / f"{src.stem}_{k}{src.suffix}"
            k += 1
        previous = None
        if dest.exists():
            file_version(dest)
            with access_file(dest, write=True):
                pass
            previous = dest.read_bytes()
        temporary = dest.with_name(".preview-" + uuid.uuid4().hex + ".tmp")
        published = False
        committed = False
        try:
            shutil.copy2(str(src), str(temporary))
            os.replace(temporary, dest)
            published = True
            if str(dest) not in mod.preview_images:
                mod.preview_images.append(str(dest))
            # 同时提交 sidecar 的 local.preview_references 和索引。
            if not self.update_mod(mod):
                self.report_operation_failure(OperationPreflightError(mod_id, dest, tr("预览信息保存失败")))
                return False
            committed = True
        finally:
            if published and not committed:
                if previous is None:
                    dest.unlink(missing_ok=True)
                else:
                    temporary.write_bytes(previous)
                    os.replace(temporary, dest)
            temporary.unlink(missing_ok=True)
            if created_dir and not committed and not any(prev_dir.iterdir()):
                prev_dir.rmdir()
        logger.info("预览图已保存: %s -> %s", src.name, mod.display_name)
        return True

    # 容器内 ini 按键编辑（override 层，本体零改动）

    def parse_mod_key_bindings(self, mod: Mod) -> list[KeyBinding]:
        """从全部有效 INI 解析按键绑定，覆盖文件优先于本体。

        绑定中的 ini_path 和 rel 均为本体相对路径，供分组显示及定位覆盖文件。
        文件按目录深度和同层名称保持稳定顺序。选择卡片只读取内容，不初始化或修改覆盖文件。
        """
        from src.core.ini_keys import parse_ini_text

        if not mod or not mod.path:
            return []
        # 选择卡片仅执行读取，不重建缺失的覆盖文件。
        pairs = iter_effective_inis(fs.normal_path(mod.path))  # [(rel, text)]

        def _depth_then_name(item):
            """先按 INI 相对路径深度再按忽略大小写的路径排序，使根级声明先参与按键解析。"""
            rel = item[0]
            parts = rel.split("/")
            return (len(parts),) + tuple(p.lower() for p in parts)

        result: list[KeyBinding] = []
        for rel, text in sorted(pairs, key=_depth_then_name):
            for b in parse_ini_text(text, rel):
                b.rel = rel  # 路由 override 用（parse_ini_text 已把 ini_path 设为 rel）
                result.append(b)
        return result

    @protect_mod_operation()
    @library_mutation
    @guarded_edit
    def edit_mod_key(
        self,
        mod_id: str,
        rel: str,
        line_no: int,
        field: str,
        new_token: str,
    ) -> bool:
        """修改指定 INI 行的 key 或 back 值，并保存到容器覆盖目录。

        已启用 Mod 同步更新部署副本；目标无效、非容器或写入失败时返回 False。
        """
        from src.core.ini_keys import IniWriteError, rewrite_key_in_text
        from src.core.mod_package import (
            override_path_for,
            resolve_effective_ini_text,
            write_override,
        )

        mod = self._index.get_mod(mod_id)
        if not mod or not mod.path:
            return False
        if self.check_mod_operation(mod_id, metadata=True, deployment=True) is None:
            return False
        override = override_path_for(mod.path, rel)
        if override is None:
            # 非容器路径不支持覆盖文件编辑。
            logger.warning("edit_mod_key: 非容器本体，无 override 能力: %s", mod.path)
            return False
        override = Path(override)
        previous_override = override.read_bytes() if override.exists() else None
        if override.exists():
            with access_file(override, write=True):
                pass
        if mod.is_enabled:
            if not mod.deployed_path:
                raise OperationPreflightError(mod_id, mod.path, tr("启用记录没有对应部署文件"))
            with access_file(Path(mod.deployed_path) / rel, write=True):
                pass
        base_text = resolve_effective_ini_text(mod.path, rel)
        if base_text is None:
            logger.warning("edit_mod_key: 读不到有效 ini: %s / %s", mod.path, rel)
            return False
        try:
            new_text = rewrite_key_in_text(base_text, line_no, field, new_token)  # type: ignore[arg-type]
        except IniWriteError as e:
            logger.warning("edit_mod_key: 行改写失败: %s", e)
            return False
        if not write_override(mod.path, rel, new_text):
            return False
        # 启用态：即时把新 override 同步到部署副本对应 ini（源→部署方向），无需重启用即生效。
        if mod.is_enabled:
            try:
                self._sync_override_to_deploy(mod, rel, new_text)
            except Exception as e:
                if previous_override is None:
                    override.unlink(missing_ok=True)
                else:
                    override.write_bytes(previous_override)
                self.report_operation_failure(failure(mod_id, mod.deployed_path, e))
                return False
        logger.info("已改键并写 override: %s [%s] line=%d field=%s", mod.display_name, rel, line_no, field)
        return True

    def _sync_override_to_deploy(self, mod: Mod, rel: str, text: str) -> None:
        """核对已记录部署身份，将 override 写入该部署的对应 ini；失败向调用者抛出。"""
        confirm_deployment(mod.mod_id, mod.deployed_path)
        deploy_dir = Path(mod.deployed_path)
        target = deploy_dir / rel
        if not target.exists():
            raise FileNotFoundError(str(target))

        # ① 已与 override 同一份物理数据 → 无需任何写入
        op = override_path_for(mod.path, rel)
        if op is not None and self._is_same_physical_file(target, Path(op)):
            return

        # ② 独立副本（当前唯一形态）→ 就地写
        try:
            tmp = target.with_name(target.name + ".tmp")
            with open(tmp, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
                fh.write(text)
            os.replace(str(tmp), str(target))
        except OSError as e:
            logger.warning("写部署副本 ini 失败: %s", e)
            raise

    @staticmethod
    def _is_same_physical_file(a: Path, b: Path) -> bool:
        """两个路径是否指向同一份物理数据（hardlink 同 inode / symlink 跟随后同一文件）。"""
        try:
            return os.path.samefile(str(a), str(b))
        except OSError:
            return False

    def effective_ini_disk_path(self, mod: Mod, rel: str) -> str | None:
        """「打开文件」用的可点开磁盘路径：override 优先（已物化在容器内），
        否则文件夹本体内的真实 ini；归档本体且无 override → None（包内无磁盘文件可开）。
        """
        if not mod or not mod.path:
            return None
        op = override_path_for(mod.path, rel)
        if op is not None and fs.is_file(op):
            return str(op)
        body = Path(fs.normal_path(mod.path))
        if fs.is_dir(body):
            f = body / rel
            if fs.is_file(f):
                return str(f)
        return None

    @library_mutation
    def disable_all_mods(self, node_id: str | None = None) -> int:
        """同步禁用当前游戏的 Mod，复用 UI/插件任务相同的批量事务。

        失败项保持启用及预设引用；成功项同步部署记录、ini、变体和事件。
        交互调用应使用宿主任务服务，将作业单的文件 IO 放到后台执行。
        """
        if not self.current_game_id:
            return 0
        mods = self._index.get_all_mods(self.current_game_id)
        entries = self.prepare_disable_batch(
            [mod.mod_id for mod in mods if mod.is_enabled and (node_id is None or mod.node_id == node_id)]
        )
        results = [self.disable_batch_step(entry) for entry in entries]
        return len(self.commit_disable_batch(results))

    def search_mods(self, query: str, game_id: str | None = None) -> list[Mod]:
        """搜索 Mod"""
        return self._index.search_mods(query, game_id)

    # 可再生索引：分类解析 + 发现

    def _match_character_chain(self, names_chain: list[str], game_id: str | None = None, *, chars=None) -> str:
        """按从根到叶的名称链匹配分类树，返回叶节点编号；失配时返回空字符串。"""
        if not names_chain:
            return ""
        gid = game_id or self.current_game_id
        if chars is None:
            chars = self.get_characters(gid)
        by_parent: dict[str, list] = {}
        for c in chars:
            by_parent.setdefault(c.parent_id or "", []).append(c)
        cur_parent = ""
        leaf_id = ""
        for seg in names_chain:
            match = next(
                (c for c in by_parent.get(cur_parent, []) if (c.name or c.name_en or c.id) == seg),
                None,
            )
            if match is None:
                return ""
            leaf_id = match.id
            cur_parent = match.id
        return leaf_id

    def _resolve_character_id_for_path(self, mod_path: str | Path, game_id: str | None = None, *, chars=None) -> str:
        """根据 Mod 物理位置确定所属分类。

        优先通过目录名称链匹配分类树，失败时读取父目录的分类标记；
        无法确定归属时返回空字符串。
        """
        gid = game_id or self.current_game_id
        root = self._get_library_root(gid)
        p = Path(mod_path)
        # 容器内本体以容器为分类锚点，排除本体名和容器名；独立源以自身路径为锚点。
        anchor = p.parent if (p.parent / MOD_CONTAINER_MARKER).is_file() else p
        try:
            rel = anchor.resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            return ""
        segments = list(rel.parts[:-1])  # 去掉锚点自身那一段，剩下分类链
        if not segments:
            return ""
        if chars is None:
            chars = self.get_characters(gid)

        leaf = self._match_character_chain(segments, gid, chars=chars)
        if leaf:
            return leaf

        # 回退处理：锚点父目录的合法当前 marker（外部改名等导致名链失配时仍可恢复）
        try:
            data = self._read_category_marker(anchor.parent)
        except InvalidManagedMetadataError as e:
            logger.warning("忽略无效类别 marker %s: %s", anchor.parent, e)
            return ""
        if data is not None:
            cid = data["id"]
            if cid and any(c.id == cid for c in chars):
                return cid
        return ""

    def _character_breadcrumb(self, node_id: str, game_id: str | None = None, *, chars=None) -> list[str]:
        """角色 id → 从根到该节点的名称链（写入 sidecar.local.classification，跨机稳定）。"""
        if not node_id:
            return []
        gid = game_id or self.current_game_id
        if chars is None:
            chars = self.get_characters(gid)
        by_id = {c.id: c for c in chars}
        chain: list[str] = []
        cur = by_id.get(node_id)
        guard = 0
        while cur is not None and guard < 100:
            chain.append(cur.name or cur.name_en or cur.id)
            cur = by_id.get(cur.parent_id or "")
            guard += 1
        chain.reverse()
        return chain

    def _write_mod_sidecar(self, mod: Mod, *, chars=None, repair_invalid=False) -> bool:
        """持久化完整恢复元数据，并将失败报告给当前操作。"""
        if not mod or not mod.path:
            return False
        try:
            try:
                existing = read_sidecar(mod.path)
            except InvalidManagedMetadataError:
                if not repair_invalid:
                    raise
                existing = {}
            extra = unknown_manifest_keys(existing) if existing else {}
            container = Path(mod.path).parent
            local = dict((existing or {}).get("local") or {})
            local.pop("preview_order", None)
            local.pop("classification", None)
            local.pop("status_hint", None)
            local["preview_references"] = [self._index._image_reference(p, container, mod) for p in mod.preview_images]
            manifest = build_local_manifest(local_meta_from_mod(mod), local, extra=extra)
            return write_sidecar(mod.path, manifest)
        except Exception as error:
            logger.error(
                "保存 Mod 元数据失败 game=%s mod=%s path=%s reason=%s", mod.game_id, mod.mod_id, mod.path, error
            )
            return False

    def _is_body_in_container(self, body_path: str | Path) -> bool:
        """该本体是否已在容器内（父目录含 .d3dxmod 正向 marker）。"""
        try:
            return fs.is_file(Path(body_path).parent / MOD_CONTAINER_MARKER)
        except OSError:
            return False

    def _discover_library_mods(
        self,
        game_id: str | None = None,
        *,
        invalid_metadata: list[str] | None = None,
        changed_paths: set[Path] | None = None,
        cancel_cb=None,
        known_paths: frozenset[str] | None = None,
        source_inventory=None,
        audit_owns_known_checks: bool = False,
    ) -> list[dict]:
        """按类别发现库内 Mod，返回包含 folder_name、path、node_id 的记录列表。

        遍历类别目录的直接子项，跳过类别标记或已知子类别名称对应的目录。
        再扫描不属于已知类别的直接成员，将其归为未分类。
        changed_paths 指定变化范围时，只枚举与变化路径处于同一分支的目录及候选。
        启动、游戏切换或手动全量刷新时不传该参数。
        """
        gid = game_id or self.current_game_id
        chars = self.get_characters(gid)

        child_names: dict[str, set] = {}
        for c in chars:
            if cancel_cb is not None:
                cancel_cb()
            child_names.setdefault(c.parent_id or "", set()).add(c.name or c.name_en or c.id)

        def _norm(p) -> str:
            """将发现路径转换为平台规范化绝对路径，供已知源及扫描范围比较。"""
            return os.path.normcase(os.path.abspath(str(p)))

        known = set()
        for path in known_paths or ():
            if cancel_cb is not None:
                cancel_cb()
            known.add(_norm(path))
        cached_markers = {}
        if source_inventory is not None and source_inventory.root == _norm(self._get_library_root(gid)):
            for entry in source_inventory.entries:
                if cancel_cb is not None:
                    cancel_cb()
                cached_markers[_norm(entry.container)] = entry

        def _read_discovery_marker(container):
            """复用满足审计或版本约束的已知普通本体标记；链接、缺失及类型变化走完整校验。"""
            if not known:
                return read_container_marker(container)
            from src.core.mod_identity import DEPLOYMENT_IDENTITY_KIND
            from src.core.mod_package import (
                _CONTENT_HASH_RE,
                CONTAINER_KIND,
                _read_container_marker_data,
                _validate_container_marker,
            )

            cached = cached_markers.get(_norm(container))
            data = None
            touched = bool(scope) and _in_scope(container)
            if (
                (audit_owns_known_checks or not touched)
                and cached is not None
                and cached.source is not None
                and not cached.error
                and cached.body_type in {"folder", "archive"}
                and cached.content_hash
                and _norm(cached.source.body) in known
            ):
                if audit_owns_known_checks:
                    # 此处只准备清单；控制器须接受匹配的完整审计后才能发布。
                    # 标记、本体及链接校验由该审计负责。
                    return dict(
                        kind=CONTAINER_KIND,
                        body=Path(cached.source.body).name,
                        body_type=cached.body_type,
                        content_hash=cached.content_hash,
                    )
                from src.core.library_inventory import marker_version

                try:
                    unchanged = marker_version(container) == cached.version
                except OSError as error:
                    raise InvalidManagedMetadataError(str(error)) from error
                if unchanged:
                    data = dict(
                        kind=CONTAINER_KIND,
                        body=Path(cached.source.body).name,
                        body_type=cached.body_type,
                        content_hash=cached.content_hash,
                    )
            reused = data is not None
            if not reused:
                data = _read_container_marker_data(container)
            if data is None or data.get("kind") == DEPLOYMENT_IDENTITY_KIND:
                return None
            name, content_hash = data.get("body"), data.get("content_hash")
            if (
                data.get("kind") == CONTAINER_KIND
                and isinstance(name, str)
                and name
                and name not in {".", ".."}
                and "/" not in name
                and "\\" not in name
                and not Path(name).is_absolute()
                and not Path(name).drive
                and isinstance(content_hash, str)
                and _CONTENT_HASH_RE.fullmatch(content_hash) is not None
            ):
                body = container / name
                if _norm(body) in known:
                    try:
                        body_stat = os.stat(fs.native_path(body), follow_symlinks=False)
                    except OSError:
                        # 使用统一校验器，保持本体缺失和不可读错误与完整扫描一致。
                        pass
                    else:
                        reparse = getattr(body_stat, "st_file_attributes", 0) & getattr(
                            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
                        )
                        ordinary = not reparse and not stat.S_ISLNK(body_stat.st_mode)
                        body_type = data.get("body_type")
                        matches = (body_type == "folder" and stat.S_ISDIR(body_stat.st_mode)) or (
                            body_type == "archive" and stat.S_ISREG(body_stat.st_mode) and is_resident_archive(body)
                        )
                        if ordinary and matches:
                            # 新读取的普通直接子项不能越出父目录，无需为已发现的本体重复解析真实路径。
                            return data
            # 缓存仅用于已知普通本体；链接、本体缺失或类型改变时重读完整标记。
            return read_container_marker(container) if reused else _validate_container_marker(container, data)

        scope = {_norm(p) for p in changed_paths} if changed_paths else set()

        def _in_scope(path) -> bool:
            """无 scope 时恒真；否则只认与变化路径同分支（互为祖先/后代或相等）的项。"""
            if not scope:
                return True
            key = _norm(path)
            return any(key == c or key.startswith(c + os.sep) or c.startswith(key + os.sep) for c in scope)

        discovered: list[dict] = []
        seen: set[str] = set(known)
        scanned_roots: set[str] = set()

        for c in chars:
            try:
                char_lib = self._get_character_library(c.id, gid, create=False)
            except Exception:
                continue
            if not _in_scope(char_lib):
                continue
            if not strict_is_dir(char_lib):
                continue
            scanned_roots.add(_norm(char_lib))
            skip = child_names.get(c.id, set())
            with os.scandir(fs.native_path(char_lib)) as entries:
                children = sorted(entries, key=lambda child: os.path.normcase(child.name))
            for child in children:
                if cancel_cb is not None:
                    cancel_cb()
                if child.name.startswith("."):
                    continue
                entry = Path(fs.normal_path(child.path))
                if not _in_scope(entry):
                    continue
                if child.is_dir():
                    # 容器目录（含 .d3dxmod 正向 marker）→ path 指向容器内本体、
                    # folder_name = 容器名。正向 marker 优先于「非分类=Mod」排除法（INV-4）。
                    try:
                        marker = _read_discovery_marker(entry)
                    except InvalidManagedMetadataError as e:
                        # 结构只校验一次，缺失或损坏的描述交给导入层处理。
                        logger.warning("跳过无效 Mod 容器 %s: %s", entry, e)
                        if invalid_metadata is not None and str(entry) not in invalid_metadata:
                            invalid_metadata.append(str(entry))
                        continue
                    except OSError:
                        marker = None
                    if marker is not None:
                        body = entry / marker["body"]
                        key = _norm(body)
                        if key in seen:
                            continue
                        seen.add(key)
                        discovered.append(
                            {
                                "folder_name": entry.name,
                                "path": str(body),
                                "node_id": c.id,
                                "content_hash": marker["content_hash"],
                            }
                        )
                        continue
                    if fs.exists(entry / CATEGORY_MARKER_FILE) or entry.name in skip:
                        continue  # 子分类节点，非 Mod
                    key = _norm(entry)
                    if key in seen:
                        continue
                    seen.add(key)
                    discovered.append(
                        {
                            "folder_name": entry.name,
                            "path": str(entry),
                            "node_id": c.id,
                        }
                    )
                elif child.is_file() and is_resident_archive(entry):
                    key = _norm(entry)
                    if key in seen:
                        continue
                    seen.add(key)
                    discovered.append(
                        {
                            "folder_name": entry.stem,
                            "path": str(entry),
                            "node_id": c.id,
                        }
                    )

        # 扫描资源库根下未被认领且不属于分类目录的直接子项。
        library_root = self._get_library_root(gid)
        unclassified = 0

        def _fallback_scope(path):
            """检查取消并排除已扫描分类根，仅让仍在目标范围内的路径参与补充发现。"""
            if cancel_cb is not None:
                cancel_cb()
            # 直接成员清单已经覆盖这些分类根，包括无效容器和子分类。
            return _norm(path) not in scanned_roots and _in_scope(path)

        for d in self._storage.scan_library_directory(
            str(library_root),
            invalid_metadata=invalid_metadata,
            scope_filter=_fallback_scope,
            marker_reader=_read_discovery_marker,
        ):
            if cancel_cb is not None:
                cancel_cb()
            p = Path(d["path"])
            if not _in_scope(p):
                continue
            key = _norm(p)
            if key in seen:
                continue
            invalid_keys = {_norm(path) for path in (invalid_metadata or [])}
            if key in invalid_keys or _norm(p.parent) in invalid_keys:
                continue
            if fs.is_dir(p) and fs.exists(p / CATEGORY_MARKER_FILE):
                continue  # 分类节点，跳过
            seen.add(key)
            item = {
                "folder_name": d["folder_name"],
                "path": d["path"],
                "node_id": "",
            }
            if d.get("content_hash"):
                item["content_hash"] = d["content_hash"]
            discovered.append(item)
            unclassified += 1
        if unclassified:
            logger.info("发现 %d 个未归类 Mod（路径未匹配任何角色）", unclassified)
        return discovered

    def rebuild_index_from_filesystem(self, game_id=None, progress_cb=None) -> RebuildReport:
        """同步调用方使用相同的受保护发布流程。"""
        from src.core.rebuild_session import RebuildSession

        session = RebuildSession(self, game_id or self.current_game_id)

        def progress(phase, done, total):
            """将重建阶段回调适配为调用方接受的完成数和总数，不转发阶段名称。"""
            if progress_cb is not None:
                progress_cb(done, total)

        try:
            session.prepare(progress)
            while not session.stage_batch():
                pass
            session.validate(progress)
            return session.commit()
        finally:
            session.close()

    def _release_rebuild_inflight_ids(self) -> None:
        """注销本轮重建登记的在途 id（落库后由索引接管保护）。"""
        for mod_id in tuple(self._rebuild_inflight_ids):
            self.unregister_inflight_mod_id(mod_id)
        self._rebuild_inflight_ids.clear()

    def _hash_rebuild_descriptors(
        self,
        descriptors,
        progress_cb=None,
        unreadable=None,
        *,
        resource_dir: str | None = None,
        cancel_cb=None,
    ):
        """在后台计算内容指纹、部署状态并查找预览，不访问数据库。

        每完成一项，通过 progress_cb 报告计数。归档优先使用成员表指纹，
        目录本体及缺少 CRC 的成员仍需读取内容，因此使用线程池执行。
        预览缓存命中时无需重新打开归档。数据库读写由主线程的规划和提交步骤完成。

        损坏、无法打开或规划后消失的 Mod 记入 unreadable 并跳过，
        避免中断整次重建或写入无效指纹。
        """
        total = len(descriptors)
        if total == 0:
            return []
        # 恢复/跨游戏重建由调用方传目标目录，不借尚未激活或属于另一游戏的当前状态。
        # 直接执行当前游戏的哈希阶段时，仍可省略这个参数。
        if resource_dir is None:
            resource_dir = self._res_dir()
        cache_index = build_preview_cache_index(get_data_dir() / resource_dir / "preview_cache")
        # 工作线程仅处理文件和解压，不访问绑定所有者线程的 SQLite 连接。
        # 压缩库的原生计算可释放 GIL，允许多个 Mod 并行处理；单个 Mod 直接串行执行。
        workers = min(8, max(2, os.cpu_count() or 4), total)
        results: list = [None] * total
        failures: list = []  # (序号, path, 原因)
        lock = threading.Lock()
        done = 0

        def _one(i: int, d: dict) -> None:
            """在线程池计算单项指纹并核对源版本，锁内收集结果及递增进度，取消异常继续抛出。"""
            nonlocal done
            if cancel_cb is not None:
                cancel_cb()
            path = d["path"]
            item = dict(d)
            failure = ""
            try:
                if not fs.exists(path):
                    failure = tr("源文件已不存在")
                else:
                    item["source_version"] = capture_source_version(path)
                    item["content_hash"] = Mod.compute_sha1(path)
                    if not item["content_hash"]:
                        failure = tr("内容哈希为空")
                    elif capture_source_version(path) != item["source_version"]:
                        failure = tr("计算内容指纹时源文件发生变化")
            except InterruptedError:
                raise
            except Exception as e:
                failure = str(e) or e.__class__.__name__
            if not failure:
                if cancel_cb is not None:
                    cancel_cb()
                self._enrich_rebuild_item(item, d, lock, cache_index=cache_index, resource_dir=resource_dir)
            with lock:
                if failure:
                    failures.append((i, path, failure))
                else:
                    results[i] = item
                done += 1
                # 进度回调须在锁内执行，保证 done 按完成顺序严格递增；
                # 锁外通知可能导致进度顺序与计数更新顺序不一致。
                if progress_cb is not None:
                    try:
                        progress_cb(done, total)
                    except Exception:
                        pass

        try:
            if workers <= 1:
                for i, d in enumerate(descriptors):
                    _one(i, d)
            else:
                with ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="rebuild-hash",
                ) as pool:
                    for fut in [pool.submit(_one, i, d) for i, d in enumerate(descriptors)]:
                        fut.result()  # InterruptedError / 未预期异常照旧向上抛
        except BaseException:
            # 移除保护前等待线程池结束，避免取消或重建失败后工作线程重新创建未受保护的缓存。
            self._release_rebuild_inflight_ids()
            raise
        # 失败按 descriptor 原序汇报：并行完成顺序不稳定，报告必须可复现
        for _i, path, entry_error in sorted(failures):
            logger.warning("重建跳过无法计算内容哈希的 Mod %s: %s", path, entry_error)
            if unreadable is not None and path not in unreadable:
                unreadable.append(path)
        return [item for item in results if item is not None]

    def _enrich_rebuild_item(
        self,
        item: dict,
        d: dict,
        lock,
        *,
        cache_index: PreviewCacheIndex | None = None,
        resource_dir: str | None = None,
    ) -> None:
        """哈希成功后补齐一条 descriptor（id / 在途保护 / 启用态 / 预览 / sidecar）。

        在哈希线程里执行，只碰文件系统。``register_inflight_mod_id`` 自带锁，
        ``_rebuild_inflight_ids`` 是普通 set 故这里用调用方的锁保护。
        """
        path = item["path"]
        item["new_id"] = item["mod_id"]
        # 预览写入至索引提交之间登记在途身份，防止并行清理将其误判为孤立缓存。
        with lock:
            self._rebuild_inflight_ids.add(item["new_id"])
        self.register_inflight_mod_id(item["new_id"])
        try:
            item["sidecar"] = read_sidecar(path)
        except InvalidManagedMetadataError:
            item["sidecar"] = None
        item["mod_id"] = read_source_mod_id(path)
        if d.get("mod_id") and item["mod_id"] != d["mod_id"]:
            raise IdentityConflict(tr("重建期间源 Mod 身份发生变化：{path}", path=path))
        item["preview_images"] = (
            self._find_previews(
                path,
                mod_id=item["new_id"],
                resource_dir=resource_dir,
                content_hash=item["content_hash"],
                source_version=item["source_version"],
                cache_index=cache_index,
                ignore_selection=True,
            )
            or []
        )

    def _apply_sidecar_info(self, mod: Mod, sidecar: dict) -> None:
        """从本地元数据恢复重新构造的 Mod 信息。

        以持久化文件内容填充字段，无效年龄分级使用默认值。
        """
        info = (sidecar or {}).get("info") or {}
        if not info:
            return
        if info.get("author"):
            mod.info.author = info["author"]
        if info.get("description"):
            mod.info.description = info["description"]
        if info.get("version"):
            mod.info.version = info["version"]
        if info.get("url"):
            mod.info.url = info["url"]
        if info.get("tags"):
            mod.info.tags = list(info["tags"])
        ar = info.get("age_rating")
        if ar:
            try:
                mod.info.age_rating = AgeRating(ar)
            except ValueError:
                logger.debug("sidecar age_rating 非法，忽略: %s", ar)
        if isinstance(info.get("custom_data"), dict):
            mod.info.custom_data = dict(info["custom_data"])

    @staticmethod
    def _order_previews(previews: list[str], sidecar: dict) -> list[str]:
        """按 sidecar.local.preview_references 中的路径重排重新发现的预览图。

        未登记的预览图排在末尾，保持发现时的相对顺序。
        """
        references = ((sidecar or {}).get("local") or {}).get("preview_references") or []
        order = [ref["path"] for ref in references if isinstance(ref, dict) and ref.get("path")]
        if not order or not previews:
            return previews
        normalized = [str(name).replace("\\", "/").strip("/").casefold() for name in order]

        def _rank(path: str) -> int:
            """按记录的相对路径或文件名确定预览优先级，未匹配项排在所有已知引用之后。"""
            full = str(path).replace("\\", "/").casefold()
            base = Path(path).name.casefold()
            for i, key in enumerate(normalized):
                if full == key or full.endswith("/" + key):
                    return i
                if "/" not in key and base == key:
                    return i
            return len(normalized)

        return sorted(previews, key=_rank)

    @property
    def index_healthy(self) -> bool:
        """检查当前索引文件能否被解析，供启动自检使用。

        可解析的空库仍可能缺少数据，须结合 index_needs_recovery 判断是否需要恢复。
        """
        try:
            return self._index.health_check()
        except Exception:
            return False

    @property
    def index_schema_error(self) -> str:
        """返回索引结构不兼容的原因；空字符串表示结构有效。"""
        return getattr(self._index, "schema_error", "")

    def _disk_has_library_content(self, game_id: str | None = None) -> bool:
        """该游戏的文件库根目录是否含「已有数据」（类别目录 / Mod 文件夹 / zip）。

        用于区分「库被清空/丢失」（磁盘有数据但 DB 空）与「全新安装」（都空）。
        """
        gid = game_id or self.current_game_id
        try:
            root = self._get_library_root(gid)
            for entry in fs.list_children(root):
                if entry.name.startswith("."):
                    continue
                if fs.is_dir(entry) or (fs.is_file(entry) and is_resident_archive(entry)):
                    return True
        except OSError:
            return False
        return False

    def index_needs_recovery(self) -> str:
        """判断启动时的索引状态。

        incompatible 表示结构不可用，corrupt 表示文件无法解析，两者均须确认后隔离重建；
        stale 表示索引为空但磁盘仍有资源，需要重建；空字符串表示正常。
        先检查结构，以免被关闭的业务读取被误判为空库。
        """
        if self.index_schema_error:
            return "incompatible"
        if not self.index_healthy:
            return "corrupt"
        try:
            for g in self._game_service.games:
                gid = g.id
                if not self._disk_has_library_content(gid):
                    continue
                empty_chars = len(self.get_characters(gid)) == 0
                empty_mods = len(self.get_mods(game_id=gid)) == 0
                if empty_chars and empty_mods:
                    return "stale"
        except Exception as e:
            logger.warning("index_needs_recovery 检查失败: %s", e)
        return ""

    def rebuild_all_games(self, progress_cb=None) -> dict:
        """对每个已知游戏从文件库重建索引（不隔离 DB；用于 stale 修复与恢复复用）。"""
        reports: dict[str, RebuildReport] = {}
        for g in self._game_service.games:
            try:
                self.rebuild_character_tree_from_markers(g.id)
                reports[g.id] = self.rebuild_index_from_filesystem(g.id, progress_cb=progress_cb)
            except Exception as e:
                logger.warning("重建游戏 %s 失败: %s", g.id, e)
        return reports

    def recover_corrupt_index(self) -> dict:
        """隔离损坏的索引及辅助文件，再从各游戏的资源库重建分类和索引。

        返回隔离路径与各游戏重建结果。
        """
        db_path = get_data_dir() / "mod_index.db"
        try:
            self._index.close()
        except Exception:
            pass

        quarantined = ""
        if db_path.exists():
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            dst = db_path.with_name(f"mod_index.db.corrupt-{ts}")
            try:
                db_path.replace(dst)
                for ext in ("-wal", "-shm"):
                    side = db_path.with_name(db_path.name + ext)
                    if side.exists():
                        side.replace(dst.with_name(dst.name + ext))
                quarantined = str(dst)
                logger.warning("已隔离疑似损坏的索引 DB → %s", dst)
            except OSError as e:
                logger.error("隔离损坏 DB 失败: %s", e)

        # 重建空库并改接引用（PresetManager 共享同一 ModIndex 实例）
        self._index = ModIndex(self.current_game_id)
        self._preset_manager._mod_index = self._index

        reports = self.rebuild_all_games()
        return {"quarantined": quarantined, "reports": reports}

    def plan_scan_intakes(
        self,
        *,
        invalid_metadata: list[str] | None = None,
        changed_paths: set[Path] | None = None,
    ):
        """为外部发现的路径规划不持有数据库连接的导入请求。

        invalid_metadata 收集无法解析标记的容器，供调用方提示处理。
        changed_paths 限定监控事件涉及的分支，避免单个目录事件触发全库扫描。
        扫描文件名不代表用户改名，正式名称由纳管时读取的分享元数据确定。
        """
        from src.core.mod_intake import IntakeTargetMissingError, ModIntakeService

        gid = self.current_game_id
        if not gid:
            return []

        def normalized(path) -> str:
            """规范化扫描路径为绝对比较键，避免同一路径以大小写或相对形式重复纳管。"""
            return os.path.normcase(os.path.abspath(str(path)))

        existing_paths = {normalized(mod.path) for mod in self._index.get_all_mods(gid) if mod.path}
        service = ModIntakeService(self)
        requests = []
        for descriptor in self._discover_library_mods(
            gid,
            invalid_metadata=invalid_metadata,
            changed_paths=changed_paths,
        ):
            source = Path(descriptor["path"])
            if normalized(source) in existing_paths:
                continue
            managed = self._is_body_in_container(source)
            try:
                request = service.plan_request(
                    source_path=source,
                    game_id=gid,
                    node_id=descriptor.get("node_id", ""),
                    move_source=not managed,
                    context="rediscovery" if managed else "external",
                )
            except IntakeTargetMissingError as exc:
                logger.warning("跳过目标已失效的纳管项 (%s): %s", source, exc)
                continue
            requests.append(request)
        return requests

    @library_mutation
    def reconcile_library(
        self,
        game_id: str | None = None,
        *,
        trigger: str,
        changed_paths: set[Path] | None = None,
        plan_intakes: bool = True,
    ) -> ReconcileReport:
        """统一处理启动、游戏切换、目录事件、手动刷新和定时轮询的库对账。

        先检查进行中的事务及索引可用性，再按持久身份认领被移动或改名的容器，
        清理仍缺失的源记录，核对实际部署，最后规划新增内容并返回 ReconcileReport。
        认领和清理须早于发现新增，避免已改名容器被当成重复输入。

        启动、游戏切换和手动刷新扫描当前游戏；目录事件限定为 changed_paths；
        轮询仅检查已登记路径。plan_intakes 为 False 时保留路径对账，暂不规划新增。
        """
        gid = game_id or self.current_game_id
        report = ReconcileReport(game_id=gid, trigger=trigger)
        if not gid:
            report.skipped = "no-active-game"
            return report

        # 未完成事务或不可用资源库暂不参与本轮对账。
        if self.index_schema_error:
            report.skipped = "index-schema"
            logger.debug("索引结构不可用，跳过本轮对账: %s", trigger)
            return report
        try:
            if self._preset_manager.has_pending_apply(gid):
                report.skipped = "preset-apply"
                logger.debug("预设切换事务期间跳过库对账: %s (%s)", gid, trigger)
                return report
        except Exception as e:
            # 对账会撤销预设意图，读不到事务状态就必须 fail closed，等下一轮。
            logger.warning("检查预设切换事务失败，已跳过库对账: %s", e)
            report.skipped = "preset-apply-unknown"
            return report

        # 2. 源失效（DB 准确路径）：先精确认领，认领不到的再按删除处理
        try:
            context = ReconcileContext(self._index.get_library_rows(gid))
        except Exception as error:
            logger.warning("读取库快照失败，跳过本轮对账: %s", error)
            report.skipped = "snapshot-error"
            return report
        report.claimed = self.claim_relocated_containers(gid, context=context)
        report.removed_sources = self.reconcile_missing_sources(gid, context=context)

        # 3. 部署目录改名或副本失效（持久身份）
        try:
            report.disabled_deployments = self.reconcile_missing_deployments(gid, context=context)
        except Exception as e:
            logger.warning("部署副本对账失败: %s", e)

        report.pending_cleanup = self.has_pending_source_cleanup()

        # 4. 浅扫新增（只出计划，重 IO 归调用方的 worker）
        if plan_intakes and self.trigger_discovers(trigger, changed_paths):
            if gid != self.current_game_id:
                # plan_request 冻结的是「当前激活游戏」的配置，跨游戏规划没有意义。
                logger.debug("非激活游戏不规划纳管: %s (%s)", gid, trigger)
            else:
                try:
                    report.intake_requests = self.plan_scan_intakes(
                        invalid_metadata=report.invalid_containers,
                        changed_paths=changed_paths,
                    )
                except Exception as e:
                    logger.warning("规划新增 Mod 纳管失败: %s", e)
        return report

    @staticmethod
    def trigger_discovers(
        trigger: str,
        changed_paths: set[Path] | None,
    ) -> bool:
        """判断当前触发条件是否需要浅层扫描以发现新增内容。

        调用方据此决定纳管期间是否登记补充扫描。轮询和无变化路径的文件监控事件
        不发现新增内容，不应登记补充扫描或显示待处理提示。
        """
        if trigger == "poll":
            return False
        if trigger == "watcher":
            return bool(changed_paths)
        return True

    @library_mutation
    def claim_relocated_containers(self, game_id: str | None = None, *, context: ReconcileContext | None = None) -> int:
        """按持久化 Mod 身份认领被移动或重命名的容器，返回成功认领数量。

        更新位置时保留 Mod 编号、部署关联和预设引用。独立目录和归档没有可认领身份，
        由纳管流程处理。目录事件可能只提供旧路径，因此存在失效记录时扫描整个资源库，
        不将候选范围限制为事件路径。
        """
        gid = game_id or self.current_game_id
        try:
            if context is None:
                context = ReconcileContext(self._index.get_library_rows(gid))
        except Exception as e:
            logger.warning("claim_relocated_containers 获取 mods 失败: %s", e)
            return 0

        def _norm(path) -> str:
            """生成与平台大小写规则一致的绝对路径键，用于匹配移动后的容器位置。"""
            return os.path.normcase(os.path.abspath(str(path)))

        rows = tuple(context.rows.values())
        missing = [row for row in rows if row.path and context.check(row.path).state == "missing"]
        if not missing:
            return 0

        # 候选本体不能被其他有效记录占用，避免两条记录引用同一资源。
        occupied = {_norm(row.path) for row in rows if row.path and context.check(row.path).state != "missing"}
        candidates: dict[str, tuple[Path, str]] = {}
        invalid_metadata: list[str] = []
        descriptors = self._discover_library_mods(gid, invalid_metadata=invalid_metadata)
        if invalid_metadata:
            # 不可读或损坏的标记可能掩盖已移动的身份；继续处理可确认的认领，其余记录不能判定缺失。
            context.relocation_complete = False
        for descriptor in descriptors:
            body = Path(descriptor["path"])
            if _norm(body) in occupied or not self._is_body_in_container(body):
                continue
            identity = read_source_mod_id(body)
            if identity in candidates:
                raise IdentityConflict(tr("源 Mod mod_id 身份重复：{body}", body=body))
            candidates[identity] = (body, descriptor.get("node_id", ""))

        claimed = 0
        for row in missing:
            found = candidates.get(row.mod_id)
            if found is None:
                continue
            body, node_id = found
            if _norm(body) in occupied:
                continue
            mod = self._index.get_mod(row.mod_id)
            if mod is None or mod.path != row.path or mod.mod_id != row.mod_id:
                continue
            if not self._claim_container_move(mod, body, node_id, game_id=gid):
                continue
            context.update(mod)
            occupied.add(_norm(body))
            candidates.pop(mod.mod_id, None)
            claimed += 1
        if claimed:
            logger.info("按 mod_id 认领已移动的容器: %d 个", claimed)
        return claimed

    def _claim_container_move(
        self,
        mod: Mod,
        body: Path,
        node_id: str,
        *,
        game_id: str | None = None,
        locations=None,
    ) -> bool:
        """把一条记录改指新本体：路径、预览图前缀、分类跟着走，派生态随后自愈。"""
        if (
            check_path(mod.path).state != "missing"
            or check_path(str(body)).state != "exists"
            or read_source_mod_id(body) != mod.mod_id
        ):
            return False
        # 审计可能通过后代容器发现节点移动，只恢复带标记的祖先，再定位实际父节点。
        from src.core.library_events import LibraryEventService, LocalChanges

        service = LibraryEventService(self._index.paths)
        gid = game_id or mod.game_id
        root = self._index.paths.roots[gid]
        ancestors = []
        parent = body.parent.parent
        while parent != root and parent != parent.parent:
            ancestors.append(parent)
            parent = parent.parent
        if parent != root:
            return False
        try:
            for ancestor in reversed(ancestors):
                marker = service._marker(ancestor)
                if marker and marker[0] == "node":
                    result = LocalChanges()
                    service._apply(ancestor, marker, result)
                    if result.review_paths or result.conflicts:
                        return False
            node_id = service._parent(body.parent, gid) or ""
        except (OSError, ValueError, KeyError):
            return False
        old_body = Path(mod.path)
        old_container = old_body.parent
        new_container = body.parent
        # 恢复读取可识别已改名的本体；发布索引路径前保存已验证的名称，
        # 避免后续容器事件仍将本体判断为缺失。
        marker = read_container_marker(new_container)
        if marker is None or marker.get("mod_id") != mod.mod_id or marker.get("body") != body.name:
            return False
        if not write_container_marker(
            new_container, body.name, marker["content_hash"], mod_id=mod.mod_id, imported_at=mod.imported_at
        ):
            return False
        prefixes = (
            (str(old_body), str(body)),
            (str(old_container), str(new_container)),
        )
        updated_previews: list[str] = []
        for preview in mod.preview_images:
            for old_prefix, new_prefix in prefixes:
                if preview.startswith(old_prefix):
                    updated_previews.append(preview.replace(old_prefix, new_prefix, 1))
                    break
            else:
                updated_previews.append(preview)

        old_path = mod.path
        old_folder_name = mod.folder_name
        old_previews = mod.preview_images
        old_character_id = mod.node_id
        mod.preview_images = updated_previews
        mod.path = str(body)
        mod.folder_name = new_container.name
        # 按容器当前物理位置更新分类，资源库根下的内容归为未分类。
        # 保留用户保存的目录名称，部署关联继续使用持久身份。
        reclassified = node_id != old_character_id
        if reclassified:
            mod.node_id = node_id
        previous_game = self._index.game_id
        self._index.game_id = game_id or self.current_game_id
        try:
            updated = self._index.update_mod(mod)
        finally:
            self._index.game_id = previous_game
        if not updated:
            mod.path = old_path
            mod.folder_name = old_folder_name
            mod.preview_images = old_previews
            mod.node_id = old_character_id
            logger.warning("认领已移动容器的索引写入失败: %s", body)
            return False
        live_events = getattr(self, "_local_source_events", None)
        if live_events is not None and live_events.paths is self._index.paths:
            live_events.accept_source_claim(mod.mod_id, old_container, new_container)
        if reclassified:
            self._preset_manager.cascade_move_mod(mod.mod_id, old_character_id or "", node_id or "", mod.game_id)
        if game_id is None or game_id == self.current_game_id:
            self._heal_derived_state_after_claim(mod, locations=locations)
        logger.info(
            "已按 mod_id 认领移动后的容器: %s (%s -> %s)",
            mod.mod_id[:8],
            old_path,
            mod.path,
        )
        return True

    def _heal_derived_state_after_claim(self, mod: Mod, *, locations=None) -> None:
        """源容器移动后，按身份找到实际部署并重新登记 INI 同步。"""
        if self._ini_sync is not None:
            try:
                self._ini_sync.unregister_mod(mod.mod_id)
                self._ini_sync_refs.pop(mod.mod_id, None)
            except Exception as e:
                logger.warning("认领后注销旧 ini 同步失败 (%s): %s", mod.mod_id, e)
        try:
            location = (
                self._storage.scan_deployments(
                    (mod.deployed_path,) if mod.deployed_path else (),
                    force=True,
                )
                if locations is None
                else locations
            ).get(mod.mod_id)
        except (OSError, IdentityConflict) as error:
            logger.warning("认领后查找部署身份失败 (%s): %s", mod.mod_id, error)
            return
        if location is not None and location.active:
            self._storage._check_deployment_identity(location.path, mod.mod_id)
            self._register_ini_sync(mod.mod_id, mod.path, location.path, mod_ids=(mod.mod_id,))

    @library_mutation
    def reconcile_missing_sources(self, game_id: str | None = None, *, context: ReconcileContext | None = None) -> int:
        """按已登记的源路径清理无法认领的缺失资源，返回清理完成的记录数。

        统一对账先按持久身份认领改名或移动的容器；本方法仅处理仍缺失的记录。
        以幂等事务清理部署、预设引用和附属数据。物理清理失败时保留索引，
        供后续重试，避免遗留不可管理的部署。
        """
        gid = game_id or self.current_game_id
        try:
            if self._preset_manager.has_pending_apply(gid):
                logger.debug("预设切换事务期间跳过源库对账: %s", gid)
                return 0
        except Exception as e:
            logger.warning("检查预设切换事务失败，已跳过源库对账: %s", e)
            return 0
        self._pending_source_cleanup = False
        try:
            if context is None:
                context = ReconcileContext(self._index.get_library_rows(gid))
        except Exception as e:
            logger.warning("reconcile_missing_sources 获取 mods 失败: %s", e)
            return 0

        removed = 0
        for row in tuple(context.rows.values()):
            check = context.check(row.path)
            if check.state == "exists":
                continue
            if check.state == "error":
                logger.warning("源路径检查失败，保留记录 (%s): %s", row.path, check.error)
                continue
            if not context.relocation_complete:
                self._pending_source_cleanup = True
                continue
            try:
                mod = self._index.get_mod(row.mod_id)
                if mod is None or mod.path != row.path or mod.mod_id != row.mod_id:
                    continue
                container = self._index.paths.container_path(mod.mod_id)
                if check_path(str(container)).state == "exists":
                    try:
                        marker = read_container_marker(container)
                    except (OSError, InvalidManagedMetadataError):
                        marker = None
                    if marker and marker.get("mod_id") == mod.mod_id:
                        recovered = container / marker["body"]
                        if check_path(str(recovered)).state == "exists":
                            write_container_marker(
                                container,
                                marker["body"],
                                marker["content_hash"],
                                mod_id=mod.mod_id,
                                imported_at=mod.imported_at,
                            )
                            if self._claim_container_move(mod, recovered, mod.node_id, game_id=gid):
                                context.update(mod)
                            continue
                if self._purge_missing_source_mod(mod, gid):
                    removed += 1
                    context.rows.pop(row.mod_id, None)
                else:
                    self._pending_source_cleanup = True
            except Exception as e:
                logger.warning("清理孤儿 Mod 记录失败: %s - %s", row.mod_id, e)
                self._pending_source_cleanup = True

        if removed:
            logger.info("源库对账完成: 清理 %d 个孤儿记录", removed)
        if self._pending_source_cleanup:
            logger.warning("仍有外部删除的 Mod 未清零，将自动重试")
        if not self._cleanup_orphaned_managed_artifacts(gid, context=context):
            self._pending_source_cleanup = True
            logger.warning("仍有孤儿预览缓存/更新备份待清理，将自动重试")
        return removed

    def _find_previews(
        self,
        folder_path: str,
        mod_id: str = "",
        resource_dir: str | None = None,
        *,
        content_hash: str | None = None,
        source_version: tuple | None = None,
        cache_index: PreviewCacheIndex | None = None,
        ignore_selection: bool = False,
    ) -> list[str]:
        """收集本体原始预览和容器补充预览。

        本体扫描排除元数据目录；归档图片使用预览缓存，手动添加或分享包带入的图片
        从容器 .d3dxmeta/previews 读取。合并两类结果，不重复复制本体图片。
        """
        path = Path(folder_path)
        results: list[str] = []
        try:
            sidecar = read_sidecar(path) or {}
        except InvalidManagedMetadataError:
            sidecar = {}
        local = sidecar.get("local") or {}
        # 用户选择由 restore_preview_references 恢复，普通刷新不补入其他资源图片。
        if local.get("preview_selection_locked") is True and not ignore_selection:
            return []

        # ① 本体自带原始预览图
        if fs.is_file(path) and is_resident_archive(path):
            # 压缩档（zip/7z/rar）：从包内查找并提取到中心 cache（list_contents/extract_files 三格式通用）。
            ext_set = {e.lower() for e in IMAGE_EXTENSIONS} - {".dds"}
            excluded = {os.path.normcase(name) for name in local.get("excluded_archive_previews", [])}
            if ignore_selection:
                excluded.clear()
            cache_id = mod_id or path.stem
            cache_dir = get_data_dir() / (resource_dir or self._res_dir()) / "preview_cache" / cache_id
            # 重建和导入提供当前内容指纹及资源观察。
            observed = capture_source_version(path)
            if source_version is not None and observed != source_version:
                return []
            if content_hash is None or source_version is None:
                actual_hash = Mod.compute_sha1(str(path))
                if content_hash is not None and actual_hash != content_hash:
                    return []
                content_hash = actual_hash
            source_version = observed
            if capture_source_version(path) != source_version:
                return []
            contents = None
            if excluded:
                contents = self._archive.list_contents(path)
                # 导入转换可能去掉归档的单层包装目录，必须按完整相对路径匹配，不能只比较文件名。
                relative_names = Mod._strip_single_top(contents)
                exact_names = {os.path.normcase(name.replace("\\", "/")) for name in contents}
                stripped_exclusions = excluded - exact_names
                excluded.update(
                    os.path.normcase(name.replace("\\", "/"))
                    for name, relative in relative_names.items()
                    if os.path.normcase(relative) in stripped_exclusions
                )
            cached = self._cached_archive_previews(
                mod_id, resource_dir, ext_set, content_hash=content_hash, cache_index=cache_index, excluded=excluded
            )
            if ignore_selection and (local.get("preview_selection_locked") or local.get("excluded_archive_previews")):
                cached = []
            if cached:
                if capture_source_version(path) == source_version:
                    results.extend(cached)
            else:
                if contents is None:
                    contents = self._archive.list_contents(path)
                image_files = [
                    n
                    for n in contents
                    if os.path.splitext(n)[1].lower() in ext_set
                    and ".d3dxmeta" not in n.replace("\\", "/").split("/")
                    and os.path.normcase(n.replace("\\", "/")) not in excluded
                ][:5]
                if image_files:
                    try:
                        with preview_cache_staging(cache_dir) as stage:
                            extracted = self._archive.extract_files(path, image_files, stage)
                            present = [Path(p) for p in extracted if fs.is_file(p)]
                            if len(present) == len(image_files) and capture_source_version(path) == source_version:
                                published = publish_preview_cache(stage, cache_dir, present, content_hash=content_hash)
                                results.extend(str(p) for p in published)
                    except InterruptedError:
                        raise
                    except OSError as error:
                        logger.warning("预览缓存写入失败 (%s): %s", path, error)
        elif fs.is_dir(path):
            # 扫描文件夹本体，排除 DDS 贴图及 .d3dxmeta 子树中的元数据图片。
            try:
                ext_set = {ext.lower() for ext in IMAGE_EXTENSIONS}
                imgs = [str(p) for p in fs.iter_files(path) if p.suffix.lower() in ext_set]
                results.extend(
                    img for img in imgs if not img.lower().endswith(".dds") and ".d3dxmeta" not in Path(img).parts
                )
            except Exception:
                pass

        # 读取容器级 .d3dxmeta/previews/ 中独立于本体保存的补充预览图。
        prev_dir = resolve_previews_dir(path)
        if prev_dir is not None and prev_dir.is_dir():
            try:
                results.extend(
                    str(p)
                    for p in sorted(prev_dir.iterdir())
                    if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS and not p.name.lower().endswith(".dds")
                )
            except OSError:
                pass

        # 去重保序（本体图在前、手动/外来图在后），上限 5 张
        seen: set[str] = set()
        deduped: list[str] = []
        for img in results:
            key = os.path.normcase(os.path.abspath(img))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(img)
        return deduped[:5]

    @staticmethod
    def _iter_preview_cache_candidates(
        root: Path,
        mod_id: str,
        content_hash: str,
        *,
        cache_index: PreviewCacheIndex | None = None,
    ):
        """仅已发布的内容指纹可授权复用缓存。"""
        exact = root / mod_id
        if mod_id and cache_content_hash(exact) == content_hash:
            yield exact
        if cache_index is not None:
            for candidate in cache_index.get(content_hash, ()):
                if candidate != exact and cache_content_hash(candidate) == content_hash:
                    yield candidate

    def _cached_archive_previews(
        self,
        mod_id: str,
        resource_dir: str | None,
        ext_set: set,
        *,
        content_hash: str = "",
        cache_index: PreviewCacheIndex | None = None,
        excluded: set[str] | None = None,
    ) -> list[str]:
        """按已验证的内容指纹解析缓存，不依赖 Mod 身份。"""
        if not mod_id or not content_hash:
            return []
        try:
            root = get_data_dir() / (resource_dir or self._res_dir()) / "preview_cache"
            own = root / mod_id
            for cache_dir in self._iter_preview_cache_candidates(root, mod_id, content_hash, cache_index=cache_index):
                # 归档预览可能保留多层成员路径，需递归枚举缓存目录。
                found = read_cached_previews(cache_dir, ext_set)
                if excluded:
                    found = [
                        image
                        for image in found
                        if os.path.normcase(image.relative_to(cache_dir).as_posix()) not in excluded
                    ]
                if not found:
                    continue
                if cache_dir == own:
                    return [str(p) for p in found]
                adopted = self._adopt_cached_previews(found, cache_dir, own, content_hash=content_hash)
                if adopted:
                    return adopted
            return []
        except OSError:
            return []

    @staticmethod
    def _adopt_cached_previews(
        found: list[Path],
        source_dir: Path,
        own_dir: Path,
        *,
        content_hash: str = "",
    ) -> list[str]:
        """把同内容的缓存图复制到本 id 的目录下，返回新路径（失败返回空 → 照常开包）。"""
        try:
            with preview_cache_staging(own_dir) as stage:
                copied = []
                for src in found:
                    dest = stage / src.relative_to(source_dir)
                    fs.ensure_dir(dest.parent)
                    shutil.copy2(fs.native_path(src), fs.native_path(dest))
                    copied.append(dest)
                return [str(path) for path in publish_preview_cache(stage, own_dir, copied, content_hash=content_hash)]
        except (OSError, ValueError, shutil.Error):
            return []

    def has_deploy_dir(self) -> bool:
        """返回当前游戏是否已配置部署目录。

        未配置目录时拒绝启用 Mod，界面据此提前提示。
        不涉及文件操作的预设切换仍可执行；需要部署时由 prepare_preset_apply 校验。
        """
        return self._storage.get_deploy_dir() is not None

    def get_mod_deployment_path(self, mod: Mod) -> Path | None:
        """核验当前游戏中已登记的部署副本，包括禁用后保留的目录。

        只读取当前副本的身份标记；改名后的路径由现有后台对账更新，预览不扫描部署根目录。
        """
        root = self._storage.get_deploy_dir()
        if root is None or mod.game_id != self.current_game_id or not mod.deployed_path:
            return None
        path = Path(fs.normal_path(mod.deployed_path))
        name = path.name.casefold()
        if name.startswith(".") or name.endswith(".partial") or name.startswith("disabled_.update_"):
            return None
        try:
            if self._storage._path_key(path.parent.resolve()) != self._storage._path_key(root.resolve()):
                return None
            if not fs.is_dir(path):
                return None
            identity = read_deployment_identity(path)
        except (OSError, ValueError):
            return None
        return path if identity is not None and identity["mod_id"] == mod.mod_id else None

    def describe_game_data(self, game_id: str, resource_dir: str, *, cancelled=None, progress=None) -> dict:
        """列出「删除该游戏」会一并抹掉的东西（供确认对话框把后果讲清楚）。

        只统计、不改任何状态。返回 {mods, characters, presets, deployed, library_dir,
        library_size}。
        """
        from src.core.game_data_summary import describe_game_data

        return describe_game_data(
            self._index._db_path,
            game_id,
            get_data_dir() / resource_dir if resource_dir else None,
            len(self._preset_manager.get_all_presets(game_id)),
            cancelled=cancelled,
            progress=progress,
        )

    @library_mutation
    def purge_game_data(
        self,
        game_id: str,
        resource_dir: str,
        *,
        remove_deployed: bool = True,
    ) -> dict:
        """删除指定游戏的受管数据，返回清理统计和失败路径。

        移除已登记的部署副本、该游戏的索引记录及预设，再删除游戏资源目录。
        remove_deployed=False 可保留部署副本；未登记的外部部署不在清理范围。
        共享缓存和下载目录保留，游戏配置条目由 GameService 管理。
        """
        stats: dict = {"deployed_removed": 0, "rows": {}, "presets": 0, "library_removed": False, "errors": []}
        if game_id and game_id == self.current_game_id:
            self.stop_ini_sync()
        if remove_deployed:
            try:
                mods = self._index.get_all_mods(game_id)
            except Exception:
                mods = []
            targets: list[Path] = []
            for mod in mods:
                if mod.deployed_path:
                    targets.append(Path(mod.deployed_path))
            try:
                for record in self._index.get_deployments(game_id):
                    if record.actual_path:
                        targets.append(Path(record.actual_path))
            except Exception:
                pass
            seen: set[str] = set()
            for target in targets:
                key = os.path.normcase(os.path.abspath(str(target)))
                if key in seen:
                    continue
                seen.add(key)
                if not os.path.lexists(str(target)):
                    continue
                if self._storage._remove_deploy(target):
                    stats["deployed_removed"] += 1
                else:
                    stats["errors"].append(str(target))
        stats["rows"] = self._index.purge_game(game_id)
        try:
            stats["presets"] = self._preset_manager.purge_game(game_id)
        except Exception as e:
            logger.warning("删除游戏预设失败 (%s): %s", game_id, e)
        if resource_dir:
            root = get_data_dir() / resource_dir
            if root.exists():
                if self._remove_managed_path(root):
                    stats["library_removed"] = True
                else:
                    stats["errors"].append(str(root))
        logger.info("已删除游戏数据 %s: %s", game_id, stats)
        return stats

    @library_mutation
    def clear_deploy_dir_contents(self, game_id: str, deploy_dir: str | Path) -> tuple[int, str]:
        """清空指定游戏的部署目录，并将对应 Mod 标记为禁用。

        返回禁用数量和错误信息。删除失败时不更新数据库，调用方应停止保存配置。
        目录联接和符号链接只移除链接本身，不进入源目录。
        """
        root = Path(deploy_dir)
        if game_id and game_id == self.current_game_id:
            # 删除路径前停止对应同步器，避免继续处理已失效目录的文件事件。
            self.stop_ini_sync()
        try:
            children = list(root.iterdir()) if root.is_dir() else []
        except OSError as e:
            return 0, tr("无法读取目录内容：{e}", e=e)
        failures: list[str] = []
        for child in children:
            if not self._storage._remove_deploy(child):
                failures.append(child.name)
        if failures:
            return 0, tr("以下内容未能删除（可能正被占用）：") + "、".join(failures[:5])

        disabled = 0
        try:
            mods = self._index.get_all_mods(game_id)
        except Exception as e:
            logger.warning("清空部署目录后读取 Mod 列表失败: %s", e)
            mods = []
        for mod in mods:
            if not mod.is_enabled:
                continue
            mod.disable()
            mod.forget_deployment()
            self._persist_mod_deployment_state(mod)
            disabled += 1
        try:
            current = self._preset_manager.get_current(game_id)
            current.character_mods.clear()
            self._preset_manager._save()
        except Exception as e:
            logger.warning("清空部署目录后同步当前预设失败: %s", e)
        logger.info("已清空部署目录 %s；%d 个 Mod 置为禁用", root, disabled)
        return disabled, ""

    @library_mutation
    def rebase_deployments(
        self,
        game_id: str,
        old_dir: str | Path,
        new_dir: str | Path,
        *,
        entry_names: set[str] | None = None,
    ) -> int:
        """部署根目录迁移后更新索引中的实际部署路径，返回更新数量。

        先更新记录再重新登记 INI 同步，避免对账将原路径缺失误判为外部删除。
        部分搬迁回滚时可限定直接子项名称，保留目标目录原有部署记录。
        """
        old_root = os.path.normcase(os.path.abspath(str(old_dir)))
        new_root = str(Path(new_dir))
        if not old_root or not new_root:
            return 0
        # 同步对仍指向旧路径，先停掉；set_deploy_dir 稍后会按新目录整体重建。
        if game_id == self.current_game_id and self._ini_sync is not None:
            self.stop_ini_sync()
        selected_names = {os.path.normcase(name) for name in entry_names} if entry_names is not None else None
        updates = []
        for record in self._index.get_deployments(game_id):
            if not record.actual_path:
                continue
            cur = os.path.normcase(os.path.abspath(record.actual_path))
            if cur != old_root and not cur.startswith(old_root + os.sep):
                continue
            rel = os.path.relpath(record.actual_path, str(old_dir))
            if selected_names is not None and (
                rel == "." or os.path.normcase(Path(rel).parts[0]) not in selected_names
            ):
                continue
            target = str(Path(new_root) / rel) if rel != "." else new_root
            updates.append((target, record.mod_id))
        # 部署投影管理这些路径；根目录移动不修改资源元数据、预览、预设身份或部署状态。
        if updates:
            with self._index.transaction() as conn:
                conn.executemany("UPDATE deployment_records SET actual_path=? WHERE mod_id=?", updates)
        moved = len(updates)
        if moved:
            logger.info("部署路径已随目录迁移改写: %d 条记录 → %s", moved, new_root)
        return moved

    @library_mutation
    def set_deploy_dir(self, deploy_dir: str, *, defer_activation: bool = False) -> None:
        """设置用户显式配置的 Mod 部署目录，不根据加载器目录推导。

        空值清除部署位置并禁止启用；已启用 Mod 仍可禁用，不涉及文件的预设操作仍可执行。
        """
        old_dir = self._storage.get_deploy_dir()
        old_key = os.path.normcase(os.path.abspath(str(old_dir))) if old_dir is not None else ""
        new_key = os.path.normcase(os.path.abspath(str(deploy_dir))) if deploy_dir else ""
        changed = old_key != new_key

        # 目录变化后旧同步对仍指向旧路径，必须停止并在新目录重建；否则迁移完成后
        # watchdog 继续监听旧目录，部署侧修改不会再回写 override。
        if (changed or defer_activation) and self._ini_sync is not None:
            self.stop_ini_sync()
        self._storage.set_migoto_mods_dir(deploy_dir or "")
        if deploy_dir:
            recovery_errors = self._preset_manager.recover_incomplete_applies(self.current_game_id)
            for error in recovery_errors:
                logger.error("预设部署恢复需要人工处理: %s", error)

        # 事务恢复回调可能重新建立同步注册；延后激活会替换整套注册，
        # 先释放本次恢复创建的临时同步器。
        if defer_activation and self._ini_sync is not None:
            self.stop_ini_sync()

        if changed and deploy_dir and not defer_activation:
            try:
                self.reconcile_deployment_locations(self.current_game_id, force=True)
                self._register_existing_deployments()
            except (OSError, IdentityConflict) as error:
                logger.warning("部署目录身份尚不可确认，保留原记录：%s", error)

    def publish_deployment_activation(self, request, result) -> bool:
        """在数据库所有者线程接收纯文件系统激活结果。

        游戏、根目录或操作状态变化会使清单过期；返回 True 前，私有 INI 监控仍由工作线程负责。
        """
        current_root = self._storage.get_deploy_dir()
        if (
            request.game_id != self.current_game_id
            or path_key(request.root) != path_key(str(current_root or ""))
            or request.stamp != self.library_probe_stamp
            or self.library_probe_busy()
        ):
            return False
        self._commit_deployment_activation(request, result)
        return True

    @library_mutation
    def _commit_deployment_activation(self, request, result) -> None:
        """将后台部署探测结果对账到索引，接管同步器和清单，并发布实际变化的启停事件。"""
        from src.core.deployment_reconcile import reconcile_deployment_records

        changes = reconcile_deployment_records(
            self._index,
            self._storage,
            request.game_id,
            rows=request.rows,
            locations=result.locations,
        )
        self._ini_sync = result.sync
        self._ini_sync_refs = dict(result.refs)
        if self._ini_sync is not None:
            self._ini_sync.on_synced = self._on_deployment_ini_synced
        self._storage.adopt_deployment_inventory(result.inventory)
        result.changed_ids = tuple(mod.mod_id for mod in changes.changed)
        for mod in changes.changed:
            self._emit("mod_enabled" if mod.is_enabled else "mod_disabled", mod_id=mod.mod_id, game_id=request.game_id)

    # 移动操作 (剪切/粘贴)

    @protect_node_operation
    @library_mutation
    def move_character(self, char_id: str, new_parent_id: str, game_id: str | None = None) -> bool:
        """预检后移动分类目录并更新父节点，失败回滚目录和记录；提交后修复后代部署引用。"""
        if not self.check_node_operation(char_id):
            return False
        if new_parent_id and not self.check_node_operation(new_parent_id, children=False):
            return False
        gid = game_id or self.current_game_id
        current = self.get_character(char_id, gid)
        if current is None or char_id == new_parent_id:
            return False
        conn = self._index._db()
        moved = False
        old_path = self._get_character_library(char_id, gid, create=False)
        new_path = old_path
        try:
            parent = self._get_character_library(new_parent_id, gid, create=False)
            new_path = parent / current.directory_name
            if old_path == new_path:
                return True
            if fs.exists(new_path):
                raise FileExistsError(new_path)
            # 写入前由触发器检查游戏归属和祖先循环。
            conn.execute("UPDATE nodes SET parent_id=? WHERE id=?", (new_parent_id or None, char_id))
            os.rename(fs.native_path(old_path), fs.native_path(new_path))
            moved = True
            conn.commit()
        except Exception as error:
            conn.rollback()
            if moved:
                try:
                    os.rename(fs.native_path(new_path), fs.native_path(old_path))
                except OSError:
                    logger.exception("恢复类别移动失败")
            logger.error("移动类别失败: %s", error)
            return False
        return self._repair_deployment_links_for_subtree(char_id)

    @protect_mod_operation()
    @library_mutation
    def move_mod(self, mod_id: str, new_character_id: str) -> bool:
        """移动 Mod 到另一个类别"""
        mod = self._index.get_mod(mod_id)
        if not mod:
            return False
        if self.check_mod_operation(mod_id, metadata=True, deployment=True) is None:
            return False
        if mod.node_id == new_character_id:
            return True  # 同级目录不做操作
        if new_character_id and not self.check_node_operation(new_character_id, children=False):
            return False

        old_character_id = mod.node_id
        original_mod = copy.deepcopy(mod)

        try:
            old_path = Path(mod.path)
            new_parent = self._get_character_library(new_character_id, create=False)
            with access_file(new_parent, write=True, directory=True):
                pass
            managed_container = old_path.parent if fs.is_file(old_path.parent / MOD_CONTAINER_MARKER) else None
            if managed_container is not None:
                new_container = new_parent / managed_container.name
                new_path = new_container / old_path.name
            else:
                new_container = None
                new_path = new_parent / old_path.name

            # 当前容器移动以 sidecar + DB 均成功为提交点；任一失败恢复整容器和旧记录。
            if managed_container is not None:
                sidecar_path = managed_container / META_DIR / MANIFEST_NAME
                try:
                    sidecar_bytes = sidecar_path.read_bytes()
                except OSError as e:
                    logger.error("读取移动前 sidecar 快照失败: %s", e)
                    return False
                db_updated = False

                def _rollback_managed_move() -> None:
                    """尽力移回纳管容器及旁车原件，再还原内存与已更新的索引记录，文件恢复失败记录日志。"""
                    try:
                        if new_container is not None and new_container.exists():
                            if not managed_container.exists():
                                shutil.move(str(new_container), str(managed_container))
                        restored_sidecar = managed_container / META_DIR / MANIFEST_NAME
                        restored_sidecar.parent.mkdir(parents=True, exist_ok=True)
                        restored_sidecar.write_bytes(sidecar_bytes)
                    except OSError as rollback_error:
                        logger.error("Mod 容器移动回滚失败: %s", rollback_error)
                    mod.__dict__.clear()
                    mod.__dict__.update(copy.deepcopy(original_mod.__dict__))
                    if db_updated:
                        self._index.update_mod(original_mod)

                if not old_path.exists() or new_container.exists():
                    return False
                shutil.move(str(managed_container), str(new_container))

                old_prefix = str(managed_container)
                new_prefix = str(new_container)
                mod.preview_images = [
                    path.replace(old_prefix, new_prefix, 1) if path.startswith(old_prefix) else path
                    for path in mod.preview_images
                ]
                mod.node_id = new_character_id
                mod.path = str(new_path)

                if not self._write_mod_sidecar(mod):
                    logger.error("移动后的 sidecar 写入失败，正在回滚: %s", new_container)
                    _rollback_managed_move()
                    return False
                if not self._index.update_mod(mod):
                    logger.error("移动后的索引写入失败，正在回滚: %s", new_container)
                    _rollback_managed_move()
                    return False
                db_updated = True

                try:
                    self._preset_manager.cascade_move_mod(
                        mod_id,
                        old_character_id,
                        new_character_id,
                        self.current_game_id,
                    )
                except Exception as e:
                    logger.warning("preset cascade_move_mod 失败: %s", e)
                    _rollback_managed_move()
                    return False

                if mod.is_enabled and mod.deployed_path:
                    self._register_ini_sync(mod_id, mod.path, mod.deployed_path, (mod_id,))

                logger.info("Mod 已移动: %s -> %s", mod.display_name, new_character_id[:8])
                return True

            return False  # 仅移动受管容器。
        except Exception as e:
            logger.error("移动 Mod 失败: %s", e)
            return False

    @library_mutation
    def _repair_deployment_links_for_subtree(self, char_id: str) -> bool:
        """仅刷新当前子树的活动部署与 INI 关联，无需重写后代的资源和图片路径。"""
        from src.core.mod_package import list_overrides, override_path_for
        from src.models.deployment import DeploymentRecord, DeploymentState

        conn = self._index._db()
        rows = conn.execute(
            "WITH RECURSIVE subtree(id) AS (SELECT id FROM nodes WHERE id=? "
            "UNION ALL SELECT n.id FROM nodes n JOIN subtree s ON n.parent_id=s.id) "
            "SELECT m.mod_id,d.actual_path FROM subtree s JOIN mods m ON m.node_id=s.id "
            "JOIN deployment_records d ON d.mod_id=m.mod_id WHERE d.state='active'",
            (char_id,),
        ).fetchall()
        success = True
        for row in rows:
            identity, target = row["mod_id"], Path(row["actual_path"])
            try:
                self._storage._check_deployment_identity(target, identity)
                body = self._index.paths.body_path(identity)
                if self._ini_sync is not None:
                    self._ini_sync.unregister_mod(identity)
                    self._ini_sync_refs.pop(identity, None)
                for relative in list_overrides(str(body)):
                    source = override_path_for(str(body), relative)
                    if source is None:
                        raise OSError(tr("INI override 位置缺失"))
                    destination = target / relative
                    mode = self._storage.link_ini(Path(source), destination)
                    if mode == "copy" and destination.read_bytes() != Path(source).read_bytes():
                        raise OSError(tr("INI copy 修复未完成"))
                self._register_ini_sync(identity, str(body), target, mod_ids=(identity,))
            except Exception as error:
                success = False
                self._index.upsert_deployment(DeploymentRecord(identity, DeploymentState.ERROR, str(target)))
                logger.error("节点移动后部署关联修复失败 (%s): %s", identity, error)
        return success

    @library_mutation
    def migrate_game_resource_dir(self, game_id: str, old_rd: str, new_rd: str) -> str:
        """迁移资源根及受管 INI 链接，成功后返回新名称，普通失败完整回滚并返回旧名称。

        调用方须先暂停相关监听，保存游戏配置后再重建。活动与停放部署共用迁移规则，
        独立副本和硬链接不改写；回滚本身失败时抛出携带实际资源位置的专用异常。
        """
        import sqlite3

        from src.core.library_schema import valid_relative
        from src.core.mod_package import resolve_ini_overrides_dir

        if not valid_relative(old_rd, True) or not valid_relative(new_rd, True):
            return old_rd
        if old_rd == new_rd:
            return old_rd
        old_path, new_path = get_data_dir() / old_rd, get_data_dir() / new_rd
        if os.path.lexists(fs.native_path(new_path)):
            return old_rd
        current_root = self._index.paths.roots.get(game_id)
        if current_root is not None and path_key(str(current_root)) != path_key(str(old_path / "mods")):
            return old_rd

        def relocated(path: Path) -> Path:
            """仅重定位旧资源根内的规范路径，保留外部部署和外部链接目标。"""
            normalized = Path(os.path.abspath(fs.normal_path(path)))
            try:
                return new_path / normalized.relative_to(old_path)
            except ValueError:
                return normalized

        def link_version(path: Path) -> tuple[int, int, str]:
            """核验目标仍为同一符号链接，拒绝覆盖运行期间被编辑器替换的独立副本。"""
            info = os.lstat(fs.native_path(path))
            if not stat.S_ISLNK(info.st_mode):
                raise OSError(tr("目标文件已被修改，请刷新后重试"))
            return info.st_dev, info.st_ino, os.readlink(fs.native_path(path))

        def walk_failed(error: OSError) -> None:
            """将覆盖目录的访问错误交给迁移回滚，避免把不可访问误判为没有 INI。"""
            raise error

        links: list[tuple[Path, Path, Path, tuple[int, int, str]]] = []
        backups: list[tuple[Path, Path]] = []
        moved = completed = False
        connection = None
        try:
            updates = []
            records = sorted(self._index.get_deployments(game_id), key=lambda record: record.actual_path)
            for record in records:
                if not record.actual_path:
                    continue
                target = Path(fs.normal_path(record.actual_path))
                body = self._index.paths.body_path(record.mod_id)
                if read_source_mod_id(body) != record.mod_id:
                    raise IdentityConflict(tr("源文件身份或位置已变化"))
                self._storage._check_deployment_identity(target, record.mod_id)
                rebound = relocated(target)
                if rebound != target:
                    updates.append((str(rebound), record.mod_id))
                overrides = resolve_ini_overrides_dir(body)
                if overrides is None:
                    continue
                try:
                    os.stat(fs.native_path(overrides))
                except FileNotFoundError:
                    continue
                # 覆盖层只包含 INI，无需遍历部署目录中的贴图等大体积资源。
                for directory, _, files in os.walk(fs.native_path(overrides), onerror=walk_failed):
                    for name in files:
                        if Path(name).suffix.casefold() != ".ini":
                            continue
                        relative = (Path(fs.normal_path(directory)) / name).relative_to(overrides)
                        destination = target / relative
                        try:
                            info = os.lstat(fs.native_path(destination))
                        except FileNotFoundError:
                            continue
                        if not stat.S_ISLNK(info.st_mode):
                            continue
                        version = link_version(destination)
                        original = Path(fs.normal_path(version[2]))
                        absolute = Path(os.path.abspath(destination.parent / original))
                        new_target = relocated(absolute)
                        if new_target == absolute:
                            continue
                        if not destination.parent.resolve().is_relative_to(target.resolve()):
                            raise IdentityConflict(tr("同步路径必须位于 Mod 内"))
                        token = uuid.uuid4().hex
                        temporary = destination.with_name(f".{name}.resource-{token}.tmp")
                        backup = destination.with_name(f".{name}.resource-{token}.bak")
                        if os.path.lexists(fs.native_path(backup)):
                            raise FileExistsError(str(backup))
                        os.symlink(fs.native_path(new_target), fs.native_path(temporary))
                        links.append((destination, temporary, backup, version))

            connection = self._index._db()
            with self._index.transaction():
                if os.path.lexists(fs.native_path(old_path)):
                    os.rename(fs.native_path(old_path), fs.native_path(new_path))
                    moved = True
                for destination, temporary, backup, version in links:
                    destination, temporary, backup = (relocated(path) for path in (destination, temporary, backup))
                    if link_version(destination) != version:
                        raise OSError(tr("目标文件已被修改，请刷新后重试"))
                    # 保存原始链接本身，回滚不依赖再次取得创建符号链接的权限。
                    os.replace(fs.native_path(destination), fs.native_path(backup))
                    backups.append((destination, backup))
                    os.replace(fs.native_path(temporary), fs.native_path(destination))
                if updates:
                    connection.executemany("UPDATE deployment_records SET actual_path=? WHERE mod_id=?", updates)
            self._index.paths.roots[game_id] = new_path / "mods"
            self._index.paths.preview_roots[game_id] = new_path / "preview_cache"
            completed = True
            return new_rd
        except (OSError, ValueError, KeyError, sqlite3.Error) as error:
            logger.error("游戏资源目录移动失败: %s", error)
            rollback_errors = []
            if connection is not None and connection.in_transaction:
                try:
                    connection.rollback()
                except sqlite3.Error as rollback_error:
                    rollback_errors.append(str(rollback_error))
            for destination, backup in reversed(backups):
                try:
                    os.replace(fs.native_path(backup), fs.native_path(destination))
                except OSError as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if moved:
                try:
                    if os.path.lexists(fs.native_path(old_path)):
                        raise FileExistsError(str(old_path))
                    os.rename(fs.native_path(new_path), fs.native_path(old_path))
                    moved = False
                except OSError as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if rollback_errors:
                actual_name, actual_path = (new_rd, new_path) if moved else (old_rd, old_path)
                self._index.paths.roots[game_id] = actual_path / "mods"
                self._index.paths.preview_roots[game_id] = actual_path / "preview_cache"
                raise ResourceDirectoryMigrationError(actual_name, actual_path, rollback_errors) from error
            return old_rd
        finally:
            for _, temporary, backup, _ in links:
                for path in (temporary, backup) if completed else (temporary,):
                    path = relocated(path) if moved else path
                    try:
                        os.unlink(fs.native_path(path))
                    except FileNotFoundError:
                        pass
                    except OSError as cleanup_error:
                        logger.warning("资源目录迁移临时链接清理失败 (%s): %s", path, cleanup_error)

    @classmethod
    def instance(cls) -> ModManager:
        """取得共享 Mod 管理器，并在首次访问时完成初始化。"""
        return cls()
