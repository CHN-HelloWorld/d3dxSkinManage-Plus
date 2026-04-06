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


@dataclass(frozen=True)
class ExportResult:
    """记录分类预设导出的最终文件位置及节点和图片数量。"""

    game_id: str
    path: Path
    node_count: int
    image_count: int


def category_preset_filename(name: str) -> str:
    """校验预设名称及 Windows 文件名限制，统一补齐一个分类预设扩展名。"""
    if not isinstance(name, str) or not name or name != name.strip():
        raise CategoryPresetError(tr("请输入有效的预设名称，不能以空格结尾"))
    while name.lower().endswith(".d3dxcp"):
        name = name[:-7]
    if not name or "/" in name or "\\" in name:
        raise CategoryPresetError(tr("预设名称必须是单个文件名"))
    _validate_path(name)
    if len((name + ".d3dxcp").encode("utf-16-le")) // 2 > 255:
        raise CategoryPresetError(tr("预设文件名超过 Windows 长度限制"))
    return name + ".d3dxcp"


def _cancel(cancelled):
    """检查调用方取消请求，收到请求时抛出分类预设取消异常。"""
    if cancelled and cancelled():
        raise CategoryPresetCancelled(tr("分类预设操作已取消"))


def _notify(progress, phase, percent):
    """在调用方提供进度回调时发送当前阶段和完成百分比。"""
    if progress:
        progress(phase, percent)


def _signature(path):
    """读取安全路径的身份、大小和修改时间；对象缺失时返回 None。"""
    safe_path(path)
    try:
        info = Path(path).stat()
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _copy(source, target, cancelled=None, *, owner=None):
    """分块排他复制文件，并按需同步暂存所有权和内容摘要。

    复制中检查取消及容量上限，写入后刷新磁盘并核对来源签名；源变化或读取失败时抛出异常。"""
    source, target = safe_path(source), safe_path(target)
    before = _signature(source)
    total = 0
    copied_digest = hashlib.sha256()
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        if owner:
            owner.opened_file(target, outgoing)
        for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
            _cancel(cancelled)
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise CategoryPresetError(tr("文件超过 1 GiB 上限"))
            outgoing.write(chunk)
            copied_digest.update(chunk)
            if owner:
                owner.file_digest(target, copied_digest.hexdigest())
        outgoing.flush()
        os.fsync(outgoing.fileno())
    if before != _signature(source):
        raise CategoryPresetError(tr("来源发生变化，请重新比较：{source}", source=source))


class CategoryPresetService:
    """组织分类预设的预览、事务发布、导出和按持久凭据恢复。"""

    def __init__(self, manager):
        """绑定资料库管理器并保存数据库路径，供每次操作建立独立连接。"""
        self.manager = manager
        self.db_path = Path(manager._index._db_path)

    @contextmanager
    def _connection(self):
        """提供注册过目录排序规则的独立 SQLite 连接，并在上下文结束时关闭。"""
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        register_connection(conn)
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
        finally:
            conn.close()

    def _games(self):
        # 直接读取配置，不使用绑定界面数据库连接的 LibraryPaths。
        """直接读取游戏配置条目，避免使用绑定所有者线程数据库的路径服务。"""
        return self.manager._game_service._store.get_games()

    def _target(self, game_id):
        """根据游戏编号解析并检查分类根目录，游戏已删除时拒绝操作。"""
        game = next((g for g in self._games() if g["id"] == game_id), None)
        if game is None:
            raise CategoryPresetError(tr("目标游戏已被删除，请重新选择"))
        root = safe_path(get_data_dir() / game["resource_dir"] / "mods")
        return CategoryPresetTarget(game_id, game["name"], game["name_en"], root)
