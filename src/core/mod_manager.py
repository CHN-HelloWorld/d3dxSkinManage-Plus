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
