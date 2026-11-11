"""在所有者线程中发布有限范围的文件系统观察。

监控队列清空并重新检查精确目标前，资源缺失状态仍可恢复；最终删除由部署清理负责。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from src.core.library_events import LibraryEventService
from src.core.library_protection import path_protected, protection_snapshot
from src.core.library_schema import directory_key
from src.core.mod_identity import DeploymentLocation, read_deployment_identity
from src.core.mod_package import read_container_marker
from src.models.deployment import DeploymentRecord, DeploymentState
from src.utils import fs
from src.utils.constants import MIGOTO_DISABLED_PREFIX, MOD_CONTAINER_MARKER
from src.utils.logger import logger
from src.utils.paths import resolve_stored_path, to_stored_path


@dataclass
class LocalPublication:
    """汇总局部事件发布涉及的成员、分类、发现项、审查路径及重试时间。"""

    changed_ids: set[str] = field(default_factory=set)
    removed_ids: set[str] = field(default_factory=set)
    discoveries: set[str] = field(default_factory=set)
    review_paths: set[str] = field(default_factory=set)
    moved_node_ids: set[str] = field(default_factory=set)
    pending_missing_ids: set[str] = field(default_factory=set)
    external_copy_paths: set[str] = field(default_factory=set)
    refresh_mod_ids: set[str] = field(default_factory=set)
    retry_after_ms: int | None = None


def _deployment_path_keys(path: str | Path) -> tuple[str, str]:
    """同时查询应用内相对记录和旧绝对记录，沿用部署路径的表达式索引。"""
    return directory_key(to_stored_path(path)), directory_key(resolve_stored_path(path))


def _service(manager, generation):
    """获取与监听代次和路径服务匹配的局部事件服务，必要时创建新实例。"""
    current = getattr(manager, "_local_source_events", None)
    paths = manager._index.paths
    if current is None or current.generation != generation or current.paths is not paths:
        current = manager._local_source_events = LibraryEventService(
            paths, generation=generation, protected=manager.is_library_path_protected
        )
    return current


def _ready_events(manager, events, channel):
    """保留受保护事件，提交或取消后重放，包括配对的移动事件。"""
    attribute = "_deferred_" + channel + "_events"
    queued = getattr(manager, attribute, {})
    queued.update({event: None for event in events})
    snapshot = protection_snapshot(manager)
    ready, pending = [], {}
    for event in queued:
        # 监控健康状态属于通知流，不受根目录操作锁控制；数据批次延后时仍记录健康变化。
        if event.kind in {"overflow", "zero_bytes", "root_replaced", "watch_failed", "root_offline"}:
            ready.append(event)
            continue
        if event.kind in {"batch_end", "rename_unpaired"}:
            if len(ready) >= 32:
                pending[event] = None
            else:
                ready.append(event)
            continue
        protected = False
        for value in (event.path, event.destination):
            if not value:
                continue
            if path_protected(manager._index.paths, value, snapshot):
                protected = True
                break
            # 分离的创建和删除事件可能使身份出现在尚未索引的位置。
            if snapshot[1]:
                candidate = Path(value)
                if candidate.name == MOD_CONTAINER_MARKER:
                    candidate = candidate.parent
                try:
                    marker = (
                        read_container_marker(candidate) if channel == "source" else read_deployment_identity(candidate)
                    )
                    if marker and marker["mod_id"] in snapshot[1]:
                        protected = True
                        break
                except (OSError, ValueError):
                    pass
                if channel == "deployment":
                    rows = (
                        manager._index._db()
                        .execute(
                            "SELECT mod_id FROM deployment_records WHERE directory_key(actual_path) IN (?,?)",
                            _deployment_path_keys(candidate),
                        )
                        .fetchall()
                    )
                    if any(row[0] in snapshot[1] for row in rows):
                        protected = True
                        break
        if protected or len(ready) >= 32:
            pending[event] = None
        else:
            ready.append(event)
    setattr(manager, attribute, pending)
    return ready


def _coalesce_source_modifications(events):
    """合并同一批次中的重复快照，同时保留通知流的顺序边界。"""
    result, last = [], {}
    for event in events:
        if event.kind == "modified":
            key = (event.root, event.generation, event.root_identity, event.path, event.destination, event.is_directory)
            previous = last.get(key)
            if previous is not None:
                result[previous] = None
            last[key] = len(result)
        elif event.kind != "batch_end":
            # 成员变化、未配对改名和健康事件须按顺序处理，即使涉及其他路径。
            last.clear()
        result.append(event)
    return [event for event in result if event is not None]


def source_events(manager, events):
    """处理已就绪源事件，汇总局部变更并修复预设分类关联和部署 INI 同步。

    关联修复失败的目录转入审查，已具备缺失证据的成员安排立即复核。"""
    publication = LocalPublication()
    direct_old_nodes = {}
    for event in _coalesce_source_modifications(_ready_events(manager, events, "source")):
        service = _service(manager, event.generation)
        changed = service.handle(event)
        publication.changed_ids.update(changed.updated_mod_ids)
        publication.refresh_mod_ids.update(changed.refresh_mod_ids)
        publication.external_copy_paths.update(changed.external_copy_paths)
        publication.pending_missing_ids.update(changed.pending_missing_ids)
        publication.moved_node_ids.update(changed.moved_node_ids)
        publication.discoveries.update(changed.discoveries)
        missing_paths = {str(service.paths.container_path(mid)) for mid in changed.pending_missing_ids}
        publication.review_paths.update(changed.review_paths - missing_paths)
        for mid, old_node in changed.old_node_ids.items():
            direct_old_nodes.setdefault(mid, old_node)
    # 节点移动只改变逻辑祖先路径，不重写后代记录。
    # 仅直接 Mod 事件需要完整对象，用于预设成员和 INI 回调。
    moved_nodes = publication.moved_node_ids
    direct_mods = manager._index.get_mods_by_ids(direct_old_nodes) if direct_old_nodes else ()
    for mod in direct_mods:
        mid = mod.mod_id
        try:
            old_node = direct_old_nodes[mid] or ""
            if old_node != (mod.node_id or ""):
                manager._preset_manager.cascade_move_mod(mid, old_node, mod.node_id or "", mod.game_id)
            if mod.is_enabled and mod.deployed_path:
                repaired_with_node = moved_nodes and any(
                    row[0] in moved_nodes for row in manager._index.paths._chain(mod.node_id or None)
                )
                if not repaired_with_node:
                    manager._register_ini_sync(mid, mod.path, mod.deployed_path, (mid,))
        except Exception as error:
            logger.error(
                "Local source association refresh failed game=%s mod=%s path=%s reason=%s",
                mod.game_id,
                mid,
                mod.path,
                error,
            )
            publication.review_paths.add(str(Path(mod.path).parent))
    for nid in moved_nodes:
        # 祖先修复已经涵盖全部活动后代。
        if any(row[0] in moved_nodes for row in manager._index.paths._chain(nid)[1:]):
            continue
        manager._repair_deployment_links_for_subtree(nid)
    service = getattr(manager, "_local_source_events", None)
    if service is not None:
        # 同一批次内的删除和创建可能重新定位同一 Mod。
        # 发布最终状态，避免路径恢复后又被较早的缺失观察禁用。
        publication.pending_missing_ids.intersection_update(service._missing)
    if service is not None and any(service.missing_retry_after_ms(mid) == 0 for mid in service._missing):
        # 发布后再调度确认，不在采集资源观察时同步清理部署和预设。
        publication.retry_after_ms = 0
    return publication


def confirm_missing(manager, *, purge=None):
    """监控事件处理完成后，仅重新检查待确认的资源目标。"""
    publication = LocalPublication()
    service = getattr(manager, "_local_source_events", None)
    if service is None:
        return publication
    retry_deadlines = getattr(service, "_cleanup_retry_after", None)
    if retry_deadlines is None:
        retry_deadlines = service._cleanup_retry_after = {}
    attempted_purge = False

    def retry_in(delay):
        """将此次发布的重试延迟更新为已请求延迟中的最小值。"""
        publication.retry_after_ms = (
            delay if publication.retry_after_ms is None else min(delay, publication.retry_after_ms)
        )

    def defer_failure(mid):
        """为清理失败的成员设置半秒冷却，并安排发布重试。"""
        retry_deadlines[mid] = service.clock() + 0.5
        retry_in(500)

    for mid in tuple(service._missing):
        remaining = retry_deadlines.get(mid, 0) - service.clock()
        if remaining > 0:
            retry_in(max(1, math.ceil(remaining * 1000)))
            continue
        if attempted_purge:
            if service.evidence_ready(mid):
                retry_in(0)
            continue
        # 轮换已检查的条目，避免失败或受保护目标持续阻挡后续可处理目标。
        service._missing[mid] = service._missing.pop(mid)
        recovered = service.recover_missing(mid)
        if mid not in service._missing:
            publication.changed_ids.add(mid)
            continue
        if not service.confirm_missing(mid, generation=service.generation, scope_complete=recovered):
            publication.pending_missing_ids.add(mid)
            if service.evidence_ready(mid):
                defer_failure(mid)
                continue
            retry = service.missing_retry_after_ms(mid, immediate=False)
            if retry is not None:
                publication.retry_after_ms = (
                    retry if publication.retry_after_ms is None else min(retry, publication.retry_after_ms)
                )
            continue
        mod = manager.get_mod(mid)
        if mod is None:
            service._missing.pop(mid, None)
            service._missing_evidence.pop(mid, None)
            retry_deadlines.pop(mid, None)
            continue
        locations = {}
        try:
            record = manager._index.get_deployment(mid)
            if record and record.actual_path:
                target = Path(record.actual_path)
                Path(fs.native_path(target.parent)).stat()
                if service._exists(target):
                    marker = read_deployment_identity(target)
                    if not marker or marker["mod_id"] != mid:
                        raise ValueError("deployment identity conflicts with missing source")
                    locations[mid] = DeploymentLocation(mid, target, marker["content_hash"])
        except (OSError, ValueError):
            publication.pending_missing_ids.add(mid)
            defer_failure(mid)
            continue
        attempted_purge = True
        result = (purge or manager._purge_missing_source_mod)(mod, mod.game_id, locations=locations)
        if result is None:
            # 界面协调器负责完成通知和下一目标调度；期间保留记录与证据，
            # 避免空转重试或将临时不可用状态误认为删除完成。
            return publication
        if result:
            publication.removed_ids.add(mid)
            service._missing.pop(mid, None)
            service._missing_evidence.pop(mid, None)
            retry_deadlines.pop(mid, None)
        else:
            publication.pending_missing_ids.add(mid)
            defer_failure(mid)
    publication.pending_missing_ids.update(service._missing)
    return publication


def deployment_events(manager, events):
    """依据部署标记处理根目录直接子项变化，更新部署索引和同步登记。

    在途操作、重复占位或无法证明缺失的目录仅进入审查，确认目录不存在后才标记部署缺失。"""
    publication = LocalPublication()
    index = manager._index
    for event in _ready_events(manager, events, "deployment"):
        if event.kind in {"batch_end", "rename_unpaired"}:
            continue
        if event.kind in {"overflow", "zero_bytes", "root_replaced", "watch_failed", "root_offline"}:
            publication.review_paths.add(event.root)
            continue
        root = Path(event.root)
        try:
            Path(fs.native_path(root)).stat()
            for value in dict.fromkeys(p for p in (event.destination, event.path) if p):
                path = Path(value)
                if path.name == MOD_CONTAINER_MARKER:
                    path = path.parent
                if path.parent != root:
                    continue
                try:
                    marker = read_deployment_identity(path)
                except FileNotFoundError:
                    marker = None
                if marker is not None:
                    mid = marker["mod_id"]
                    if mid in manager.inflight_mod_ids():
                        publication.review_paths.add(str(path))
                        continue
                    mod = index.get_mod(mid)
                    if mod is None or mod.game_id != manager.current_game_id:
                        publication.review_paths.add(str(path))
                        continue
                    occupant = (
                        index._db()
                        .execute(
                            "SELECT mod_id FROM deployment_records "
                            "WHERE directory_key(actual_path) IN (?,?) AND mod_id<>?",
                            (*_deployment_path_keys(path), mid),
                        )
                        .fetchone()
                    )
                    if occupant is not None:
                        publication.review_paths.add(str(path))
                        continue
                    previous = index.get_deployment(mid)
                    if (
                        previous
                        and previous.actual_path
                        and directory_key(previous.actual_path) != directory_key(str(path))
                    ):
                        old = Path(previous.actual_path)
                        if LibraryEventService._exists(old):
                            publication.review_paths.update((str(old), str(path)))
                            continue
                    state = (
                        DeploymentState.DISABLED
                        if path.name.upper().startswith(MIGOTO_DISABLED_PREFIX.upper())
                        else DeploymentState.ACTIVE
                    )
                    index.upsert_deployment(DeploymentRecord(mid, state, str(path)))
                    publication.changed_ids.add(mid)
                    if state == DeploymentState.ACTIVE:
                        manager._register_ini_sync(mid, mod.path, str(path), (mid,))
                    continue
                # 使用每个连接都注册的表达式索引查询已记录的精确位置。
                row = (
                    index._db()
                    .execute(
                        "SELECT d.mod_id FROM deployment_records d "
                        "WHERE directory_key(d.actual_path) IN (?,?) "
                        "AND EXISTS (SELECT 1 FROM mods m WHERE m.mod_id=d.mod_id AND m.game_id=?)",
                        (*_deployment_path_keys(path), manager.current_game_id),
                    )
                    .fetchone()
                )
                if row is None:
                    continue
                if row[0] in manager.inflight_mod_ids():
                    publication.review_paths.add(str(path))
                    continue
                try:
                    Path(fs.native_path(path)).stat()
                except FileNotFoundError:
                    mid = row[0]
                    index.upsert_deployment(DeploymentRecord(mid, DeploymentState.ABSENT, ""))
                    sync = getattr(manager, "_ini_sync", None)
                    if sync is not None:
                        sync.unregister_mod(mid)
                    publication.changed_ids.add(mid)
                else:
                    publication.review_paths.add(str(path))
        except (OSError, ValueError) as error:
            logger.error("局部部署核验失败 game=%s path=%s reason=%s", manager.current_game_id, event.path, error)
            publication.review_paths.add(event.root)
    return publication
