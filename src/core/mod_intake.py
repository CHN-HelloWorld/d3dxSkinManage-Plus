"""准备受管 Mod 的导入文件，并在主线程原子提交。"""

from __future__ import annotations

import os
import tempfile
import threading
import uuid
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from src.core.import_format import (
    DEGRADED_METADATA_NOTICE,
    PreparedImportSource,
    get_import_settings,
    install_staged_ini_overrides,
    materialize_import_body,
    materialize_staged_previews,
    normalize_import_format,
    prepare_import_source,
    stage_import_metadata,
    take_reject_reason,
)
from src.core.mod_identity import (
    IdentityConflict,
    new_mod_id,
    read_source_mod_id,
    validate_mod_id,
)
from src.core.mod_package import (
    InvalidManagedMetadataError,
    build_local_manifest,
    is_mod_container,
    local_meta_from_mod,
    read_container_marker,
    read_sidecar,
    restore_preview_references,
    valid_imported_at,
    write_container_marker,
    write_sidecar,
)
from src.core.mod_variant_package import saved_variants, stage_imported_variants, verify_variant_files
from src.core.preview_cache_index import capture_source_version
from src.models.mod import (
    AgeRating,
    Mod,
    ModStatus,
    validate_mod_record,
)
from src.models.mod_variant import ModVariant
from src.utils import fs
from src.utils.constants import is_resident_archive
from src.utils.i18n import t as tr
from src.utils.paths import get_data_dir

if TYPE_CHECKING:
    from src.core.mod_manager import ModManager


ProgressCallback = Callable[[float | None, str], None]


@dataclass
class _IdentityReservations:
    """保存同一管理器中已索引与正在导入的身份集合，防止并发导入重复认领。"""

    indexed: set[str] = field(default_factory=set)
    inflight: set[str] = field(default_factory=set)


_identity_lock = threading.Lock()
_identity_reservations: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class IntakeError(RuntimeError):
    """无法安全准备或提交导入。"""


class DuplicateIntakeError(IntakeError):
    """该游戏已存在逻辑内容相同的 Mod。"""


class IntakeTargetMissingError(IntakeError):
    """导入期间目标目录或分类被移除。"""


def intake_identity(context: str, marker: dict | None = None, *, used=()) -> tuple[str, str]:
    """使用显式来源类型，不根据其他字段猜测来源。"""
    now = datetime.now().isoformat()
    if context == "external":
        return new_mod_id(used), now
    if context != "rediscovery":
        raise IntakeError(f"unknown intake context: {context}")
    marker = marker or {}
    try:
        identity = validate_mod_id(marker.get("mod_id"))
    except IdentityConflict:
        identity = new_mod_id(used)
    timestamp = marker.get("imported_at")
    return identity, timestamp if valid_imported_at(timestamp) else now


@dataclass(frozen=True)
class IntakePlanningSnapshot:
    """冻结导入规划使用的游戏、目标目录及现有身份和内容摘要信息。"""

    game_id: str
    index_stamp: tuple
    existing_hashes: frozenset[str]
    existing_mod_ids: frozenset[str]


@dataclass(frozen=True)
class IntakeRequest:
    """描述导入来源及可选原始文件名、目标和元数据，并携带来源版本和去重依据。"""

    source_path: Path
    game_id: str
    node_id: str
    display_name: str = ""
    move_source: bool = False
    destination_path: Path | None = None
    target_format: str = ""
    strict_format: bool | None = None
    classification: tuple[str, ...] = ()
    resource_dir: str = ""
    auto_preview: bool = True
    selected_preview_name: str = ""
    excluded_previews: tuple[str, ...] = ()
    age_rating: str = ""
    gamebanana_metadata: dict | None = None
    force_age_rating: str = ""
    existing_hashes: frozenset[str] = frozenset()
    existing_mod_ids: frozenset[str] = frozenset()
    context: str = "external"
    external_copy: bool = False
    source_version: tuple | None = None
    source_name: str = ""


@dataclass
class PreparedIntake:
    """保存已准备的 Mod、文件产物、原始快照和预留资源，供提交或回滚使用。"""

    original_path: Path
    body_path: Path
    container_path: Path
    content_hash: str
    share_manifest: dict | None
    preview_paths: list[Path]
    created_paths: list[Path]
    source_was_moved: bool
    request: IntakeRequest = field(repr=False)
    mod: Mod = field(repr=False)
    import_source: PreparedImportSource = field(repr=False)
    preview_cache_path: Path | None = field(default=None, repr=False)
    source_backup_path: Path | None = field(default=None, repr=False)
    source_backup_root: Path | None = field(default=None, repr=False)
    source_marker_snapshot: bytes | None = field(default=None, repr=False)
    source_sidecar_snapshot: tuple[bool, bytes] | None = field(default=None, repr=False)
    metadata_version: tuple[bytes | None, bytes | None] | None = field(default=None, repr=False)
    written_marker: bytes | None = field(default=None, repr=False)
    written_sidecar: bytes | None = field(default=None, repr=False)
    operation_temp: Path | None = field(default=None, repr=False)
    body_version: tuple | None = field(default=None, repr=False)
    variants: tuple[ModVariant, ...] = field(default=(), repr=False)
    #: 非空表示随包描述类元数据已整份丢弃（内容完好）；由任务中心明细行展示给用户。
    metadata_notice: str = ""
    committed: bool = field(default=False, init=False)


class ModIntakeService:
    """先准备文件，再提交一条数据库记录。"""

    def __init__(self, manager: ModManager) -> None:
        """绑定管理器并获取其共享身份预留表，以协调多个导入服务实例。"""
        self._manager = manager
        with _identity_lock:
            self._identities = _identity_reservations.setdefault(manager, _IdentityReservations())

    def _snapshot_mod_ids(self) -> frozenset[str]:
        """在主线程读取游戏配置，准备线程仅使用冻结快照。"""
        indexed = self._manager._index.get_mod_ids()
        with _identity_lock:
            self._identities.indexed = set(indexed)
        return frozenset(indexed)

    def planning_snapshot(self, game_id: str) -> IntakePlanningSnapshot:
        """采集批量查重信息，不构造完整 Mod 对象。"""
        index = self._manager._index
        if index.in_transaction:
            raise IntakeError("cannot capture intake planning snapshot in a transaction")
        stamp = index.change_stamp
        rows = (
            index._db()
            .execute("SELECT DISTINCT content_hash FROM mods WHERE game_id=? AND content_hash<>''", (game_id,))
            .fetchall()
        )
        hashes = frozenset(row[0] for row in rows)
        identities = self._snapshot_mod_ids()
        if stamp != index.change_stamp:
            raise IntakeError("index changed during planning snapshot capture")
        return IntakePlanningSnapshot(game_id, stamp, hashes, identities)

    def _reserve_mod_id(self, request: IntakeRequest, identity: str | None = None) -> str:
        """在锁内生成或认领未占用身份，重复身份抛出冲突异常。"""
        with _identity_lock:
            occupied = self._identities.indexed | self._identities.inflight | request.existing_mod_ids
            if identity is None:
                identity = new_mod_id(occupied)
            elif identity in occupied:
                raise IdentityConflict(tr("mod_id 身份已被其他 Mod 占用: {identity}", identity=identity))
            self._identities.inflight.add(identity)
            return identity

    def _release_mod_id(self, identity: str, *, committed: bool = False) -> None:
        """释放在途身份预留；已提交身份同时加入已索引集合。"""
        with _identity_lock:
            if committed:
                # 较早请求可能仍持有提交前的快照。
                self._identities.indexed.add(identity)
            self._identities.inflight.discard(identity)
