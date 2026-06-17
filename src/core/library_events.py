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


class LibraryEventService:
    """依据文件身份将同一代监听事件转为资料库局部变更，并维护缺失确认凭据。"""

    def __init__(
        self, paths, *, generation=0, locations=None, protected=None, clock=time.monotonic, missing_grace=0.016
    ):
        """绑定路径服务、代次和保护回调，初始化根身份、缺失等待与监听健康状态。"""
        self.paths = paths
        self.generation = generation
        self.locations = locations if locations is not None else IdentityLocations()
        self.protected = protected or (lambda path: False)
        self.clock, self.missing_grace = clock, missing_grace
        self._missing = {}
        self._missing_evidence = {}
        self._tainted = False
        self._health_revision = 0
        self._root_identity = None
        self._watched_root = None
        self._completed_batch = 0

    @staticmethod
    def _exists(path):
        """检查路径是否存在，仅将文件不存在视为 False，其余访问错误向上传递。"""
        try:
            Path(fs.native_path(path)).stat()
            return True
        except FileNotFoundError:
            return False

    def _root(self, path):
        """查找路径所属的游戏资料库根，未匹配任何根时返回 None。"""
        key = directory_key(os.path.abspath(path))
        for gid, root in self.paths.roots.items():
            base = directory_key(str(root))
            if key == base or key.startswith(base.rstrip("\\") + "\\"):
                return gid, root
        return None

    def _parent(self, path, gid):
        """解析目标目录的已提交父分类，位于游戏根下时返回 None，未登记父目录则拒绝。"""
        parent = Path(path).parent
        if directory_key(str(parent)) == directory_key(str(self.paths.roots[gid])):
            return None
        owner = self.paths.locate(str(parent))
        if (
            not owner
            or owner[0] != "node"
            or directory_key(str(self.paths.node_path(owner[1]))) != directory_key(str(parent))
        ):
            raise ValueError("destination parent is not a committed node")
        return owner[1]

    def _marker(self, container):
        """读取容器的 Mod 或分类标记并校验身份字段，两种标记均缺失时返回 None。"""
        for kind, filename, id_key in (("mod", MOD_CONTAINER_MARKER, "mod_id"), ("node", CATEGORY_MARKER_FILE, "id")):
            try:
                marker = container / filename
                if os.name == "nt":
                    from src.core.watcher_windows import read_marker_text

                    text = read_marker_text(marker)
                else:
                    text = fs.read_text(marker, encoding="utf-8")
                data = json.loads(text)
            except FileNotFoundError:
                continue
            if not isinstance(data, dict) or not isinstance(data.get(id_key), str) or not data[id_key]:
                raise ValueError("invalid identity marker")
            return kind, data[id_key], data
        return None

    def handle(self, event):
        """过滤过期事件并检查根身份，将有效路径变化转为局部更新或待审查记录。

        监听异常会使缺失证据失效；缺失目标仅进入确认队列，文件或索引读取错误转为审查路径。"""
        result = LocalChanges()
        if event.generation != self.generation or not self._root(event.root):
            result.ignored = True
            return result
        if event.root_identity:
            self._watched_root = event.root
            if self._root_identity is not None and self._root_identity != event.root_identity:
                self._tainted = True
                self._health_revision += 1
            self._root_identity = event.root_identity
        if event.kind in {"overflow", "zero_bytes", "root_replaced", "watch_failed", "root_offline"}:
            self._tainted = True
            self._health_revision += 1
            result.review_paths.add(event.root)
            return result
        if event.kind == "rename_unpaired":
            result.review_paths.add(event.path)
            return result
        if event.kind == "batch_end":
            self._completed_batch = max(self._completed_batch, event.batch_id)
            return result
        try:
            if not self._exists(event.root):
                result.review_paths.add(event.root)
                return result
            targets = dict.fromkeys(p for p in (event.destination, event.path) if p)
            for raw in targets:
                if (
                    event.kind == "moved"
                    and raw == event.path
                    and event.destination
                    and (result.updated_mod_ids or result.moved_node_ids)
                    and not self._exists(raw)
                ):
                    continue
                if not self._root(raw):
                    continue
                if self.protected(raw):
                    result.review_paths.add(raw)
                    continue
                candidate = Path(raw)
                if candidate.name in (MOD_CONTAINER_MARKER, CATEGORY_MARKER_FILE):
                    candidate = candidate.parent
                owner = self.paths.locate(str(candidate))
                # 子文件事件归属于已提交的容器；节点下的未知子项作为待发现内容处理。
                if owner and owner[0] == "mod":
                    candidate = self.paths.container_path(owner[1])
                if not self._exists(candidate):
                    if owner and owner[0] == "mod":
                        self._missing.setdefault(owner[1], (self.clock() + self.missing_grace, str(candidate)))
                        if (
                            event.native_evidence in {"delete_pending", "outside_root"}
                            and event.evidence_mod_id == owner[1]
                            and directory_key(str(candidate)) == directory_key(raw)
                            and event.root_identity
                            and event.batch_id
                        ):
                            self._missing_evidence[owner[1]] = (event.batch_id, event.root_identity)
                        result.pending_missing_ids.add(owner[1])
                    result.review_paths.add(str(candidate))
                    continue
                marker = self._marker(candidate)
                if marker is None:
                    result.review_paths.add(str(candidate))
                    if owner is None or owner[0] == "node":
                        result.discoveries.add(str(candidate))
                    continue
                self._apply(candidate, marker, result)
                if (
                    marker[0] == "mod"
                    and marker[1] in result.updated_mod_ids
                    and (event.kind != "moved" or not event.is_directory)
                ):
                    result.refresh_mod_ids.add(marker[1])
        except (OSError, ValueError, KeyError, sqlite3.Error):
            result.review_paths.update(p for p in (event.path, event.destination) if p)
        return result

    def _apply(self, candidate, marker, result):
        """按持久标记认领新增分类或已移动对象，冲突及受保护路径只登记审查。

        Mod 移动会更新路径并按需同步默认显示名与旁置文件；自有事务失败时恢复数据库和仍匹配的旁置内容。"""
        kind, identity, data = marker
        conn = self.paths.connection
        table, key = ("mods", "mod_id") if kind == "mod" else ("nodes", "id")
        columns = "game_id,node_id,name,folder_name,info_json" if kind == "mod" else "game_id,parent_id,directory_name"
        row = conn.execute(f"SELECT {columns} FROM {table} WHERE {key}=?", (identity,)).fetchone()
        gid = self._root(str(candidate))[0]
        if row is None:
            if kind == "node":
                parent = self._parent(candidate, gid)
                if data.get("game_id", gid) != gid or data.get("node_type", "category") not in (
                    "category",
                    "character",
                ):
                    raise ValueError("node marker ownership or type is invalid")
                if not all(isinstance(data.get(key, ""), str) for key in ("name", "name_en")):
                    raise ValueError("node marker names must be strings")
                owns_transaction = not conn.in_transaction
                try:
                    conn.execute(
                        "INSERT INTO nodes(id,game_id,parent_id,directory_name,name,name_en,node_type) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            identity,
                            gid,
                            parent,
                            candidate.name,
                            data.get("name", candidate.name),
                            data.get("name_en", ""),
                            data.get("node_type", "category"),
                        ),
                    )
                    if owns_transaction:
                        conn.commit()
                except sqlite3.Error:
                    if owns_transaction:
                        conn.rollback()
                    raise
                result.moved_node_ids.add(identity)
                return
            result.discoveries.add(str(candidate))
            return
        if row[0] != gid:
            result.conflicts.add(identity)
            result.review_paths.add(str(candidate))
            return
        old = self.paths.container_path(identity) if kind == "mod" else self.paths.node_path(identity)
        self.locations.claim(identity, candidate)
        if directory_key(str(old)) != directory_key(str(candidate)):
            if self._exists(old):
                self.locations.claim(identity, old)
                result.conflicts.add(identity)
                result.review_paths.update((str(old), str(candidate)))
                old_marker = self._marker(old) if kind == "mod" else None
                if old_marker is not None and old_marker[:2] == ("mod", identity):
                    result.discoveries.add(str(candidate))
                    result.external_copy_paths.add(str(candidate))
                return
            for other in self.locations.locations(identity):
                if directory_key(other) != directory_key(str(candidate)) and self._exists(other):
                    result.conflicts.add(identity)
                    result.review_paths.update((other, str(candidate)))
                    return
        if self.protected(str(old)) or self.protected(str(candidate)):
            result.review_paths.update((str(old), str(candidate)))
            return
        parent = self._parent(candidate, gid)
        if kind == "mod":
            body = data.get("body")
            if not valid_relative(body) or not self._exists(candidate / body):
                result.review_paths.add(str(candidate))
                return
            owns_transaction = not conn.in_transaction
            sidecar = candidate / ".d3dxmeta" / "info.json"
            prior = published = None
            payload = None
            name = row[2]
            follows_directory = name in ("", row[3]) and candidate.name != row[3]
            if follows_directory:
                if not owns_transaction:
                    raise ValueError("sidecar rename requires an owned transaction")
                prior = _sidecar_state(sidecar)
                if prior is None:
                    from src.core.mod_package import build_local_manifest

                    manifest = build_local_manifest({"name": name, "info": json.loads(row[4])})
                else:
                    manifest = json.loads(prior[1])
                    if not isinstance(manifest, dict) or not isinstance(manifest.get("name", ""), str):
                        raise ValueError("invalid sidecar rename metadata")
                # 外部修改的显示名优先于过期的默认名称记录。
                name = manifest.get("name", "")
                if name in ("", row[2], row[3]):
                    name = candidate.name
                    manifest["name"] = name
                    payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
            try:
                if owns_transaction:
                    conn.execute("BEGIN")
                conn.execute(
                    "UPDATE mods SET node_id=?,name=?,folder_name=?,path=? WHERE mod_id=?",
                    (parent, name, candidate.name, body, identity),
                )
                if payload is not None:
                    published = _replace_sidecar(sidecar, prior, payload)
                elif follows_directory and _sidecar_state(sidecar) != prior:
                    raise ValueError("external sidecar edit changed during rename")
                if owns_transaction:
                    conn.commit()
            except (sqlite3.Error, OSError, ValueError):
                if owns_transaction:
                    conn.rollback()
                if published is not None and _sidecar_state(sidecar) == published:
                    if prior is None:
                        fs.unlink(sidecar)
                    else:
                        _replace_sidecar(sidecar, published, prior[1])
                raise
            result.updated_mod_ids.add(identity)
            result.old_node_ids[identity] = row[1]
            self._missing.pop(identity, None)
            self._missing_evidence.pop(identity, None)
            getattr(self, "_cleanup_retry_after", {}).pop(identity, None)
        else:
            if row[1] != parent or row[2] != candidate.name:
                self.paths.move_node(identity, parent, candidate.name)
                result.moved_node_ids.add(identity)
                result.updated_mod_ids.update(
                    r[0]
                    for r in conn.execute(
                        "WITH RECURSIVE subtree(id) AS (SELECT ? UNION ALL "
                        "SELECT n.id FROM nodes n JOIN subtree s ON n.parent_id=s.id) "
                        "SELECT mod_id FROM mods WHERE node_id IN (SELECT id FROM subtree)",
                        (identity,),
                    )
                )
        self.locations.release(identity, old)
        self.locations.claim(identity, candidate)
