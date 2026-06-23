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


def marker_stat_version(container):
    """仅读取版本信息，信任内容前仍须读取标记。"""
    try:
        info = os.stat(fs.native_path(Path(container) / MOD_CONTAINER_MARKER))
    except (FileNotFoundError, NotADirectoryError):
        return ()
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size, fs.stat_identity_time_ns(info))


def marker_version(container):
    """确认可读性和版本，只有明确缺失才返回空值。

    打开文件检查访问权限，避免已缓存的解析结果掩盖新的权限错误并错误授权删除。
    """
    try:
        path = Path(container) / MOD_CONTAINER_MARKER
        if os.name == "nt":
            from src.core.watcher_windows import open_marker

            stream = open_marker(path)
        else:
            stream = open(fs.native_path(path), "rb")
        with stream:
            info = os.fstat(stream.fileno())
    except (FileNotFoundError, NotADirectoryError):
        return ()
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size, fs.stat_identity_time_ns(info))


def scan_source_inventory(request, previous=None, *, should_cancel=lambda: False, force=False):
    """扫描分类范围中的源容器身份，按版本复用未变化条目并定期强制审计。

    重复身份、扫描期间的目录或标记变化会中止；取消抛出中断异常，返回的清单不直接修改数据库。"""
    from src.core.mod_package import read_container_marker, read_missing_container_marker

    root = path_key(request.library_root)
    if not strict_is_dir(request.library_root):
        if not request.rows:
            return SourceInventory(root, (), (), time.monotonic())
        raise FileNotFoundError(request.library_root)
    previous = previous if previous is not None and previous.root == root else None
    cached = {}
    for entry in previous.entries if previous else ():
        if should_cancel():
            raise InterruptedError("Source identity index cancelled")
        cached[path_key(entry.container)] = entry
    force = force or previous is None or time.monotonic() - previous.audited_at >= 30
    locations = deque(request.scan_locations)
    visited, seen_entries, identities = set(), set(), {}
    entries, signatures = [], []
    # 事件命中的路径强制重读，即使外部编辑器恢复了时间戳。
    changed = tuple(path_key(p) for p in request.changed_paths)

    def candidates(location):
        """枚举扫描位置的直接子目录，按规范路径去重并附加所属分类编号。"""
        with closing(_source_directories(location.path, should_cancel)) as children:
            for path in children:
                container = str(path)
                child_key = path_key(container)
                if child_key not in seen_entries:
                    seen_entries.add(child_key)
                    yield container, child_key, location.node_id

    def read_entry(candidate):
        """复用未变化条目或重新解析容器标记，核对读取前后版本并保留缺失正文身份。"""
        container, child_key, node_id = candidate
        if should_cancel():
            raise InterruptedError("Source identity scan cancelled")
        # 完整审计已读取标记，无需重复打开句柄验证可读性。
        version = marker_stat_version(container) if force else marker_version(container)
        old = cached.get(child_key)
        touched = any(
            p == child_key or p.startswith(child_key + os.sep) or child_key.startswith(p + os.sep) for p in changed
        )
        reusable = (
            not force
            and not touched
            and old is not None
            and old.version == version
            and not old.missing_mod_id
            and (old.source is None or check_path(old.source.body).state == "exists")
        )
        if reusable:
            entry = old
            if entry.source is not None and entry.source.node_id != node_id:
                entry = SourceEntry(
                    container,
                    version,
                    RelocatedSource(entry.source.mod_id, entry.source.body, node_id),
                    entry.error,
                    entry.body_type,
                    entry.content_hash,
                )
            return entry
        if should_cancel():
            raise InterruptedError("Source identity scan cancelled")
        try:
            marker = read_container_marker(container) if version else None
            source = (
                RelocatedSource(validate_mod_id(marker.get("mod_id")), str(Path(container) / marker["body"]), node_id)
                if marker
                else None
            )
            entry = SourceEntry(
                container,
                version,
                source,
                body_type=marker.get("body_type", "") if marker else "",
                content_hash=marker.get("content_hash", "") if marker else "",
            )
        except OSError:
            raise
        except ValueError as error:
            try:
                missing_marker = read_missing_container_marker(container)
            except (OSError, ValueError):
                missing_marker = None
            entry = (
                SourceEntry(container, version, None, missing_mod_id=missing_marker["mod_id"])
                if missing_marker
                else SourceEntry(container, version, None, str(error))
            )
        if should_cancel():
            raise InterruptedError("Source identity scan cancelled")
        if marker_stat_version(container) != version:
            raise OSError("Source marker changed during identity scan")
        return entry

    while locations:
        if should_cancel():
            raise InterruptedError("Source identity scan cancelled")
        location = locations.popleft()
        key = path_key(location.path)
        if key in visited:
            continue
        visited.add(key)
        signature = source_directory_signature((location.path,))
        signatures.extend(signature)
        if signature[0][1] == -1:
            continue
        # 解析真实路径，防止分类链接形成扫描环。
        real = path_key(os.path.realpath(fs.native_path(location.path)))
        if ("real", real) in visited:
            continue
        visited.add(("real", real))
        with closing(candidates(location)) as inputs:
            parsed = (
                bounded_identity_reads(inputs, read_entry, should_cancel)
                if force
                else (read_entry(candidate) for candidate in inputs)
            )
            with closing(parsed):
                for entry in parsed:
                    if should_cancel():
                        raise InterruptedError("Source identity scan cancelled")
                    entries.append(entry)
                    if entry.source:
                        identity = entry.source.mod_id
                        if identity in identities and identities[identity] != entry.source.body:
                            raise IdentityConflict(f"Duplicate source identity: {identity}")
                        identities[identity] = entry.source.body
                    elif not entry.error and not entry.missing_mod_id:
                        category = check_path(str(Path(entry.container) / CATEGORY_MARKER_FILE))
                        if category.state == "error":
                            raise OSError(category.error)
                        if key == root or category.state == "exists":
                            locations.append(ScanLocation(entry.container, ""))
    signature = tuple(signatures)
    if source_directory_signature(item[0] for item in signature) != signature:
        raise OSError("Source directory changed during identity scan")
    # 直接核对标记内容；后续扫描期间，之前读取的标记仍可能被修改。
    # 目录时间戳无法发现标记文件的原地写入。
    for entry in entries:
        if should_cancel():
            raise InterruptedError("Source identity verification cancelled")
        if marker_stat_version(entry.container) != entry.version:
            raise OSError("Source marker changed during identity scan")
    return SourceInventory(root, tuple(entries), signature, time.monotonic() if force else previous.audited_at)


def probe_with_inventory(request, should_cancel):
    """准备事件差异，并为清理保留完整的身份空间。"""
    from src.core.library_snapshot import (
        PathCheck,
        ProbeResult,
        scan_deployment_inventory,
    )

    if request.mode == "deployment":
        inventory = scan_deployment_inventory(
            request.deploy_roots, request.inventory, force=True, should_cancel=should_cancel
        )
        return ProbeResult(request, inventory=inventory)

    previous = request.source_inventory
    if previous is not None and previous.root != path_key(request.library_root):
        previous = None
    event = request.mode == "source" and previous is not None
    changed = tuple(path_key(p) for p in request.changed_paths)
    checks = []
    for row in request.rows:
        if should_cancel():
            raise InterruptedError("Source check cancelled")
        key = path_key(row.path)
        affected = not event or any(
            key == p or key.startswith(p + os.sep) or p.startswith(key + os.sep) for p in changed
        )
        checks.append(check_path(row.path) if affected else PathCheck(row.path, "unchecked"))
    if any(c.state == "error" for c in checks):
        raise OSError("Source path inaccessible")
    missing = any(c.state == "missing" for c in checks)
    needs_inventory = (
        request.force_source_audit
        or event
        or missing
        or previous is None
        or time.monotonic() - previous.audited_at >= 30
    )
    source = (
        scan_source_inventory(request, previous, should_cancel=should_cancel, force=request.force_source_audit)
        if needs_inventory
        else previous
    )
    if missing and any(entry.error for entry in source.entries):
        raise OSError("Source identity namespace is incomplete")
    # 本次事件可能伴随其他位置的漏报；重新检查容器已消失或改指的索引本体。
    if event:

        def indexed_bodies(inventory):
            """收集清单中已识别源正文的规范路径，遍历过程中响应取消请求。"""
            bodies = set()
            for entry in inventory.entries:
                if should_cancel():
                    raise InterruptedError("Source body index cancelled")
                if entry.source:
                    bodies.add(path_key(entry.source.body))
            return bodies

        bodies = indexed_bodies(source)
        old_bodies = indexed_bodies(previous)
        gone = old_bodies - bodies
        for i, row in enumerate(request.rows):
            if should_cancel():
                raise InterruptedError("Source body check cancelled")
            if checks[i].state == "unchecked" and path_key(row.path) in gone:
                checks[i] = check_path(row.path)
        if any(c.state == "error" for c in checks):
            raise OSError("Source path inaccessible")
        missing = any(c.state == "missing" for c in checks)
        if missing and any(entry.error for entry in source.entries):
            raise OSError("Source identity namespace is incomplete")
    deployment = scan_deployment_inventory(
        request.deploy_roots, request.inventory, force=missing, should_cancel=should_cancel
    )
    # 缓存回收由完整的周期检查处理，不延迟资源和部署差异的提交。
    cache_candidates = ()
    if request.mode == "full" and not missing:
        from src.core.library_snapshot import plan_cache_removal, protected_cache_names

        protected = protected_cache_names(request.rows, request.cache_root, should_cancel=should_cancel)
        candidates = []
        if strict_is_dir(request.cache_root):
            with os.scandir(fs.native_path(request.cache_root)) as children:
                for child in children:
                    if should_cancel():
                        raise InterruptedError("Cache probe cancelled")
                    if os.path.normcase(child.name) not in protected:
                        candidates.append(plan_cache_removal(fs.normal_path(child.path), should_cancel))
        cache_candidates = tuple(candidates)
    return ProbeResult(
        request,
        tuple(checks),
        deployment,
        tuple(entry.source for entry in source.entries if entry.source) if missing else (),
        cache_candidates,
        source.signature if missing else (),
        source_inventory=source,
    )
