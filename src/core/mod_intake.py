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
