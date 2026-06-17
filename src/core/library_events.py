"""按有限范围对账资源事件，缺失观察不能直接授权删除。

所有者将调用与数据库发布串行化；完整审计可建立身份位置集合，
普通事件只查询受影响的父目录链。
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.core.library_schema import directory_key, valid_relative
from src.utils import fs
from src.utils.constants import CATEGORY_MARKER_FILE, MOD_CONTAINER_MARKER


def _sidecar_state(path):
    """读取旁置元数据的版本和字节，拒绝读取期间的变化；文件不存在时返回 None。"""
    try:
        with open(fs.native_path(path), "rb") as stream:
            before = os.fstat(stream.fileno())
            payload = stream.read()
            after = os.fstat(stream.fileno())

        def version(value):
            """提取文件编号、大小及修改和状态变更时间，用于比较读取前后版本。"""
            return (value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

        if version(before) != version(after):
            raise ValueError("sidecar changed while reading")
        return version(after), payload
    except FileNotFoundError:
        return None


def _replace_sidecar(path, expected, payload):
    """仅针对观察到的元数据版本发布，保留并发修改。"""
    fs.ensure_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".rename-", suffix=".tmp", dir=fs.native_path(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if _sidecar_state(path) != expected:
            raise ValueError("sidecar changed before rename publication")
        os.replace(temporary, fs.native_path(path))
        published = _sidecar_state(path)
        if published is None or published[1] != payload:
            raise ValueError("sidecar changed during rename publication")
        return published
    finally:
        fs.unlink(temporary, missing_ok=True)


class IdentityLocations:
    """按持久身份记录已观察到的位置，用于判断目录移动和重复身份冲突。"""

    def __init__(self):
        """初始化身份到规范路径及原始路径的映射。"""
        self._items = {}

    def claim(self, identity, path):
        """为身份登记一个位置，并按规范路径去重。"""
        self._items.setdefault(identity, {})[directory_key(str(path))] = str(path)

    def release(self, identity, path):
        """撤销身份在指定位置的登记，无剩余位置时删除身份条目。"""
        entries = self._items.get(identity, {})
        entries.pop(directory_key(str(path)), None)
        if not entries:
            self._items.pop(identity, None)

    def locations(self, identity):
        """返回指定身份当前已登记位置的不可变序列。"""
        return tuple(self._items.get(identity, {}).values())


@dataclass
class LocalChanges:
    """汇总局部文件事件产生的记录变化、待确认缺失、冲突和待审查路径。"""

    updated_mod_ids: set[str] = field(default_factory=set)
    old_node_ids: dict[str, str | None] = field(default_factory=dict)
    external_copy_paths: set[str] = field(default_factory=set)
    refresh_mod_ids: set[str] = field(default_factory=set)
    moved_node_ids: set[str] = field(default_factory=set)
    pending_missing_ids: set[str] = field(default_factory=set)
    review_paths: set[str] = field(default_factory=set)
    discoveries: set[str] = field(default_factory=set)
    conflicts: set[str] = field(default_factory=set)
    ignored: bool = False
