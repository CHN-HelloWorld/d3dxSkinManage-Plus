"""可重建的资源身份清单，仅访问文件系统。

事件限定重点检查的记录，不限定身份可能存在的位置。
每次增量检查遍历扫描结构并核对标记版本，防止其他分类的漏报掩盖新复制或改指的身份。
仅复用内容未变化的解析结果。
"""

from __future__ import annotations

import os
import time
from collections import deque
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from src.core.library_snapshot import (
    RelocatedSource,
    ScanLocation,
    _source_directories,
    bounded_identity_reads,
    check_path,
    path_key,
    source_directory_signature,
    strict_is_dir,
)
from src.core.mod_identity import IdentityConflict, validate_mod_id
from src.utils import fs
from src.utils.constants import CATEGORY_MARKER_FILE, MOD_CONTAINER_MARKER


@dataclass(frozen=True)
class SourceEntry:
    """保存单个源容器的标记版本、身份位置、正文类型及读取问题。"""

    container: str
    version: tuple
    source: RelocatedSource | None
    error: str = ""
    body_type: str = ""
    content_hash: str = ""
    missing_mod_id: str = ""


@dataclass(frozen=True)
class SourceInventory:
    """保存一次资料库身份盘点的条目、目录签名和最近完整审计时间。"""

    root: str
    entries: tuple[SourceEntry, ...]
    signature: tuple[tuple[str, int, int], ...]
    audited_at: float


@dataclass(frozen=True)
class SourceObservation:
    """携带源盘点的检查结果及可复用清单，供所有者判定发布条件。"""

    mod_id: str
    path: str
    state: str
    destination: str = ""


def observe_source_paths(request, should_cancel):
    """采集可撤销的初步观察，不据此授权身份认领或清理。"""
    changed = tuple(path_key(p) for p in request.changed_paths)
    observations = []
    for row in request.rows:
        if should_cancel():
            return ()
        key = path_key(row.path)
        if not any(key == p or key.startswith(p + os.sep) or p.startswith(key + os.sep) for p in changed):
            continue
        if check_path(row.path).state == "missing":
            observations.append(SourceObservation(row.mod_id, row.path, "missing"))
    return tuple(observations)
