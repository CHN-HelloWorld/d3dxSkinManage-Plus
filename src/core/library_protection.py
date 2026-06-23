"""工作线程与本地事件消费者共用的操作范围保护。"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from pathlib import Path
from threading import RLock

from src.core.library_schema import directory_key
from src.utils.i18n import t as tr


class LibraryOperationBusy(RuntimeError):
    """资源已由冲突操作占用，调用方可稍后重试。"""


_local_operations = ContextVar("library_operation_tokens", default=())


def _overlaps(left, right):
    """判断两个规范路径集合中是否存在相同路径或祖先与后代关系。"""
    return any(
        a == b or a.startswith(b.rstrip("\\") + "\\") or b.startswith(a.rstrip("\\") + "\\")
        for a in left
        for b in right
    )


class LibraryProtection:
    """在线程间登记路径和 Mod 操作占用，并对排他配置操作实施冲突检查。"""

    def __init__(self):
        """初始化受重入锁保护的操作登记表和排他操作集合。"""
        self._lock = RLock()
        self._operations = {}
        self._variants = set()

    def protect(self, token, *, paths=(), mod_ids=(), exclusive=False, excluding=()):
        """检查竞争操作后累加令牌拥有的路径与成员，可将本操作登记为排他操作。"""
        paths = {directory_key(str(p)) for p in paths if p}
        mod_ids = {str(mid) for mid in mod_ids if mid}
        with self._lock:
            competing = set(self._operations) if exclusive else self._variants
            self._check(paths, mod_ids, competing - {token} - set(excluding))
            owned_paths, owned_ids = self._operations.setdefault(token, (set(), set()))
            owned_paths.update(paths)
            owned_ids.update(mod_ids)
            if exclusive:
                self._variants.add(token)

    def _check(self, paths, mod_ids, tokens):
        """检查指定竞争令牌的成员或路径交叠，存在占用时抛出操作繁忙异常。"""
        for token in tokens:
            occupied_paths, occupied_ids = self._operations[token]
            if mod_ids.intersection(occupied_ids) or _overlaps(paths, occupied_paths):
                raise LibraryOperationBusy(tr("该 Mod 或目录正在执行配置、更新、移动或删除操作，请稍后重试"))

    def current_mod_operation_token(self, mod_id):
        """内部步骤须显式声明直接所属的命令。

        Qt 回调可能在命令上下文内执行；新入口不能仅因上下文存在而继承写权限。
        """
        with self._lock:
            for registry, token in reversed(_local_operations.get()):
                if registry == id(self):
                    _, mod_ids = self._operations.get(token, ((), ()))
                    return token if mod_id in mod_ids else ""
        return ""

    @contextmanager
    def operation(self, token, *, paths=(), mod_ids=()):
        """在上下文内登记常规操作及本地令牌，退出时恢复上下文并释放登记。"""
        self.protect(token, paths=paths, mod_ids=mod_ids)
        context = _local_operations.set((*_local_operations.get(), (id(self), token)))
        try:
            yield
        finally:
            _local_operations.reset(context)
            self.release(token)

    @contextmanager
    def variant_operation(self, token, *, paths=(), mod_ids=(), excluding=()):
        """检查全部非排除操作后独占指定范围，上下文结束时释放保护。"""
        paths = {directory_key(str(p)) for p in paths if p}
        mod_ids = {str(mid) for mid in mod_ids if mid}
        with self._lock:
            self._check(paths, mod_ids, set(self._operations) - set(excluding) - {token})
            self._operations[token] = (paths, mod_ids)
            self._variants.add(token)
        try:
            yield
        finally:
            self.release(token)

    def release(self, token):
        """在锁内删除令牌持有的保护范围和排他标记。"""
        with self._lock:
            self._operations.pop(token, None)
            self._variants.discard(token)

    def snapshot(self):
        """返回当前全部受保护路径与 Mod 编号的独立集合。"""
        with self._lock:
            paths, ids = set(), set()
            for owned_paths, owned_ids in self._operations.values():
                paths.update(owned_paths)
                ids.update(owned_ids)
            return paths, ids


def protect_mod_operation(*, failure_value=False):
    """Mod 操作入口在耗时或可重入检查前预留资源。"""

    def decorate(method):
        """确定被包装方法的目标参数，并构造具有操作保护的调用包装器。"""
        parameter = tuple(signature(method).parameters)[1]

        @wraps(method)
        def wrapped(manager, *args, **kwargs):
            """在目标 Mod 的源和部署保护范围内调用方法，冲突时报告失败并返回约定值。"""
            registry = getattr(manager, "_library_protection", None)
            if registry is None:
                return method(manager, *args, **kwargs)
            target = args[0] if args else kwargs[parameter]
            mid = target.mod_id if hasattr(target, "mod_id") else target
            mod = manager._index.get_mod(mid)
            paths = (Path(mod.path).parent, mod.deployed_path) if mod is not None else ()
            try:
                with registry.operation("mod-command:" + uuid.uuid4().hex, paths=paths, mod_ids=(mid,)):
                    return method(manager, *args, **kwargs)
            except LibraryOperationBusy as error:
                from src.core.operation_preflight import OperationPreflightError

                manager.report_operation_failure(OperationPreflightError(mid, mod.path if mod else "", str(error)))
                return failure_value

        return wrapped

    return decorate


def protect_node_operation(method):
    """祖先目录的移动或删除操作拥有该目录及其中的 Mod。"""
    parameter = tuple(signature(method).parameters)[1]

    @wraps(method)
    def wrapped(manager, *args, **kwargs):
        """在目标分类子树的保护范围内执行方法，操作冲突时报告错误并返回 False。"""
        registry = getattr(manager, "_library_protection", None)
        if registry is None:
            return method(manager, *args, **kwargs)
        node_id = args[0] if args else kwargs[parameter]
        path = manager._index.paths.node_path(node_id)
        try:
            with registry.operation("node-command:" + uuid.uuid4().hex, paths=(path,)):
                return method(manager, *args, **kwargs)
        except LibraryOperationBusy as error:
            from src.core.operation_preflight import OperationPreflightError

            manager.report_operation_failure(OperationPreflightError("", path, str(error)))
            return False

    return wrapped


def protection_snapshot(manager):
    """仅读取活动操作和事务日志，不枚举资源记录。"""
    registry = getattr(manager, "_library_protection", None)
    paths, ids = registry.snapshot() if registry else (set(), set())
    ids.update(manager.inflight_mod_ids())
    for journal in manager._index.get_journals():
        data = json.loads(journal.plan_json)
        for action in data.get("actions", ()):
            ids.add(action["mod_id"])
            paths.update(
                directory_key(action[key])
                for key in ("source_path", "active_path", "disabled_path", "partial_path")
                if action.get(key)
            )
    return paths, ids


def path_protected(paths_service, path, snapshot):
    """根据路径交叠或所属 Mod 编号判断给定保护快照是否覆盖目标。"""
    paths, ids = snapshot
    key = directory_key(str(path))
    # 祖先目录改名同样影响当前操作，无关的同级目录保持可用。
    if any(key == p or key.startswith(p.rstrip("\\") + "\\") or p.startswith(key.rstrip("\\") + "\\") for p in paths):
        return True
    owner = paths_service.locate(str(path))
    return bool(owner and owner[0] == "mod" and owner[1] in ids)
