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
                    row = (
                        manager._index._db()
                        .execute(
                            "SELECT mod_id FROM deployment_records WHERE directory_key(actual_path)=?",
                            (directory_key(str(candidate)),),
                        )
                        .fetchone()
                    )
                    if row is not None and row[0] in snapshot[1]:
                        protected = True
                        break
        if protected or len(ready) >= 32:
            pending[event] = None
        else:
            ready.append(event)
    setattr(manager, attribute, pending)
    return ready
