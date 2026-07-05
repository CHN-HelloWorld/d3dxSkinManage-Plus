"""在所有者线程调度发现任务，工作线程只接收文件输入。"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from src.core.library_snapshot import path_key
from src.core.mod_manager import ModManager, ReconcileReport
from src.core.mod_storage import ModStorage
from src.core.preview_cache_index import capture_source_version
from src.utils import fs
from src.utils.logger import logger


@dataclass(frozen=True)
class DiscoveryRequest:
    """保存库发现的目录布局、已知来源和审计范围，供后台扫描独立读取。"""

    game_id: str
    root: str
    characters: tuple
    locations: tuple
    existing_paths: frozenset
    changed_paths: frozenset | None
    source_inventory: object | None = None
    verify_markers: bool = False
    audit_owns_known_checks: bool = False
    local_targets: tuple | None = None
    external_copy_paths: frozenset = frozenset()


def capture_discovery(manager, changed_paths, **options):
    """供已经持有独立快照的同步调用方使用。"""
    iterator = iter_capture_discovery(manager, changed_paths, **options)
    while True:
        try:
            next(iterator)
        except StopIteration as completed:
            return completed.value


def iter_capture_discovery(
    manager,
    changed_paths,
    *,
    source_inventory=None,
    verify_markers=False,
    audit_owns_known_checks=False,
    external_copy_paths=(),
):
    """在管理器所属线程捕获发现请求；局部变更收敛为直接目录目标，全量请求分片读取库索引。"""
    gid = manager.current_game_id
    if changed_paths is not None:
        paths = manager._index.paths
        root = paths.roots[gid]
        targets = {}
        for raw in changed_paths:
            candidate = Path(raw)
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            owner = paths.locate(str(candidate))
            if owner and owner[0] == "mod":
                continue
            node_id = owner[1] if owner else None
            parent = paths.node_path(node_id) if node_id else root
            if candidate == parent:
                # 完整性或根目录检查可在工作线程枚举此子树。
                targets[str(candidate)] = (str(candidate), node_id, True)
                continue
            # 定位最近的已知节点，输入归属于其下第一个未知目录，不创建虚构父节点。
            parts = candidate.relative_to(parent).parts
            if parts:
                candidate = parent / parts[0]
                targets[str(candidate)] = (str(candidate), node_id, False)
        return DiscoveryRequest(
            gid,
            str(root),
            (),
            (),
            frozenset(),
            frozenset(changed_paths),
            local_targets=tuple(targets.values()),
            external_copy_paths=frozenset(external_copy_paths),
        )
    root, chars, locations = manager.library_scan_layout(gid)
    rows = yield from manager._index.iter_library_rows(gid)
    audit_owns_known_checks = (
        audit_owns_known_checks
        and not verify_markers
        and source_inventory is not None
        and source_inventory.root == path_key(root)
    )
    return DiscoveryRequest(
        gid,
        root,
        chars,
        locations,
        frozenset(row.path for row in rows if row.path),
        None if changed_paths is None else frozenset(changed_paths),
        source_inventory,
        verify_markers,
        audit_owns_known_checks,
    )


def discover(request, cancel, observed=None):
    """复用统一扫描器，不共享管理器或数据库连接。"""

    def check():
        """在发现扫描的检查点响应取消，并以中断异常终止当前工作。"""
        if cancel():
            raise InterruptedError("Discovery cancelled")

    if request.local_targets is not None:
        from src.core.mod_package import read_container_marker
        from src.utils.constants import CATEGORY_MARKER_FILE, is_resident_archive

        candidates, invalid = [], []
        for raw, node_id, scan_children in request.local_targets:
            check()
            path = Path(raw)
            try:
                entries = (
                    [Path(fs.normal_path(p)) for p in Path(fs.native_path(path)).iterdir()] if scan_children else [path]
                )
                for entry in entries:
                    check()
                    if entry.name.startswith(".") or not fs.exists(entry):
                        continue
                    if fs.is_dir(entry) and fs.exists(entry / CATEGORY_MARKER_FILE):
                        continue  # 节点登记由身份服务负责。
                    marker = read_container_marker(entry) if fs.is_dir(entry) else None
                    body = entry / marker["body"] if marker and str(entry) not in request.external_copy_paths else entry
                    if marker or fs.is_dir(entry) or is_resident_archive(entry):
                        candidates.append(dict(path=str(body), folder_name=entry.name, node_id=node_id or ""))
            except (OSError, ValueError) as error:
                invalid.append(str(path))
                logger.warning("局部发现保留输入 %s: %s", path, error)
        if observed and candidates:
            observed(tuple(item["path"] for item in candidates))
        if candidates:
            _wait_for_stable_inputs(candidates, check)
        return candidates, invalid
    chars = tuple(SimpleNamespace(id=c[0], parent_id=c[1], name=c[2], name_en=c[3]) for c in request.characters)
    paths = {location.node_id: Path(location.path) for location in request.locations}
    storage = SimpleNamespace(scan_library_directory=lambda *a, **kw: ModStorage.scan_library_directory(None, *a, **kw))
    adapter = SimpleNamespace(
        current_game_id=request.game_id,
        get_characters=lambda gid: chars,
        _get_character_library=lambda cid, gid, create=False: paths[cid],
        _get_library_root=lambda gid: Path(request.root),
        _storage=storage,
    )
    invalid = []
    descriptors = ModManager._discover_library_mods(
        adapter,
        request.game_id,
        invalid_metadata=invalid,
        changed_paths=request.changed_paths,
        cancel_cb=check,
        known_paths=request.existing_paths,
        source_inventory=None if request.verify_markers else request.source_inventory,
        audit_owns_known_checks=getattr(request, "audit_owns_known_checks", False),
    )
    check()
    existing_paths = set()
    for path in request.existing_paths:
        check()
        existing_paths.add(path_key(path))
    candidates = []
    for item in descriptors:
        check()
        if path_key(item["path"]) not in existing_paths:
            candidates.append(item)
    if observed and candidates:
        observed(tuple(item["path"] for item in candidates))
    if candidates:
        _wait_for_stable_inputs(candidates, check)
    return candidates, invalid


def _wait_for_stable_inputs(candidates, check):
    """在后台等待新输入完成复制。

    仅检查新发现候选，防止目录仍在复制时被移动，已有记录不等待。
    """

    def versions():
        """逐项捕获候选来源版本，采样之间检查取消以支持稳定性确认。"""
        values = []
        for item in candidates:
            check()
            values.append(capture_source_version(item["path"], checkpoint=check))
        return values

    previous = versions()
    deadlines = [time.monotonic() + 0.5 for _ in candidates]
    while True:
        check()
        remaining = min(deadlines) - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.05, remaining))
            continue
        current = versions()
        now = time.monotonic()
        ready = []
        for index, (before, after) in enumerate(zip(previous, current)):
            if before != after:
                deadlines[index] = now + 0.5
            elif now >= deadlines[index]:
                ready.append(index)
        check()
        for index, (item, version) in enumerate(zip(candidates, current)):
            if index in ready:
                item["source_version"] = version
            else:
                item["_deferred"] = True
        # 等待一个稳定窗口后让出线程，仍变化的路径与新到达输入一起进入下一批。
        return


class DiscoveryWorker(QThread):
    """使用只读请求快照扫描候选来源，并向界面报告发现路径与最终结果。"""

    observed = Signal(object)

    def __init__(self, request, parent):
        """保存发现请求并创建线程独立工作预算，初始化结果与错误状态。"""
        super().__init__(parent)
        self.request, self.result, self.error = request, None, None
        from src.ui.library_work_budget import LibraryWorkBudget

        self._work_budget = LibraryWorkBudget()

    def _checkpoint(self):
        """按后台时间预算让出执行机会，并返回当前中断状态。"""
        return self._work_budget.checkpoint(self.isInterruptionRequested)

    def run(self):
        """执行候选发现并转发观察路径，将结果或异常保存到实例并记录耗时。"""
        start = time.perf_counter()
        try:
            self.result = discover(self.request, self._checkpoint, self.observed.emit)
        except Exception as error:
            self.error = error
        finally:
            self.elapsed_ms = (time.perf_counter() - start) * 1000
