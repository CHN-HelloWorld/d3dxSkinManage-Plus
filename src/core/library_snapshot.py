"""轻量且不可变的文件库数据，不跨边界传递数据库或 Qt 对象。"""

from __future__ import annotations

import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from functools import lru_cache, wraps
from itertools import islice
from pathlib import Path

from src.core.mod_identity import DeploymentLocation, scan_deployment_identities
from src.utils import fs


def path_key(path: str) -> str:
    """将非空路径规范化为当前平台的绝对比较键，空路径保持为空。"""
    return os.path.normcase(os.path.abspath(path)) if path else ""


@dataclass(frozen=True)
class LibraryRow:
    """冻结 Mod 身份、源部署路径、启用状态和预览引用，供跨线程检查使用。"""

    mod_id: str
    path: str
    deployed_path: str
    enabled: bool
    preview_images: tuple[str, ...]

    @classmethod
    def from_mod(cls, mod) -> LibraryRow:
        """从可变 Mod 对象提取不含数据库引用的不可变记录。"""
        return cls(mod.mod_id, mod.path, mod.deployed_path, mod.is_enabled, tuple(mod.preview_images))


@dataclass(frozen=True)
class PathCheck:
    """保存路径检查结果，区分存在、缺失与访问失败及其原因。"""

    path: str
    state: str
    error: str = ""


def check_path(path: str) -> PathCheck:
    """仅不存在或父路径不是目录时认定缺失；访问和读写错误须保留记录。"""
    if not path:
        return PathCheck(path, "missing")
    try:
        os.stat(fs.native_path(path))
    except (FileNotFoundError, NotADirectoryError):
        return PathCheck(path, "missing")
    except OSError as error:
        return PathCheck(path, "error", str(error))
    return PathCheck(path, "exists")


class ReconcileContext:
    """由单次同步对账拥有的可变工作视图，不传递给工作线程。"""

    def __init__(self, rows: tuple[LibraryRow, ...]):
        """建立单次对账的成员视图和路径检查缓存，初始认为重定位扫描完整。"""
        self.rows = {row.mod_id: row for row in rows}
        self.checks: dict[str, PathCheck] = {}
        self.relocation_complete = True

    def check(self, path: str) -> PathCheck:
        """按规范路径复用本次对账的检查结果，首次访问时读取文件状态。"""
        key = path_key(path)
        if key not in self.checks:
            self.checks[key] = check_path(path)
        return self.checks[key]

    def update(self, mod) -> None:
        """用最新 Mod 状态替换对账视图中同身份的不可变记录。"""
        self.rows[mod.mod_id] = LibraryRow.from_mod(mod)


def library_mutation(method):
    """标记文件写入前与完成后的状态，包括失败和回滚。

    存储和导入也在工作线程执行，因此计数受锁保护；这些钩子不访问数据库或可变清单。
    """

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        """在方法执行前后更新资料库版本与活动修改计数，异常时也完成计数清理。"""
        with self._library_epoch_lock:
            self._library_epoch += 1
            self._library_mutations += 1
        try:
            return method(self, *args, **kwargs)
        finally:
            with self._library_epoch_lock:
                self._library_epoch += 1
                self._library_mutations -= 1

    return wrapped


@dataclass(frozen=True)
class ProbeToken:
    """标识探测所属游戏、监听代次和请求序号，用于拒绝过期结果。"""

    game_id: str
    generation: int
    request_id: int


@dataclass(frozen=True)
class DeploymentInventory:
    """保存部署根签名、扫描时间及已识别部署位置的不可变缓存。"""

    signature: tuple[tuple[str, int, int], ...] = ()
    scanned_at: float = 0.0
    locations: tuple[DeploymentLocation, ...] = ()


def deployment_signature(roots) -> tuple[tuple[str, int, int], ...]:
    """读取部署根的规范路径、修改时间与文件编号；根不是目录时抛出异常。"""
    signature = []
    for root in roots:
        info = os.stat(fs.native_path(root))
        if not stat.S_ISDIR(info.st_mode):
            raise NotADirectoryError(root)
        signature.append((path_key(str(root)), info.st_mtime_ns, info.st_ino))
    return tuple(signature)


def source_directory_signature(paths) -> tuple[tuple[str, int, int], ...]:
    """通过存在性和目录时间戳约束移动扫描的根目录。"""
    signature = []
    for path in paths:
        try:
            info = os.stat(fs.native_path(path))
        except (FileNotFoundError, NotADirectoryError):
            signature.append((path_key(str(path)), -1, -1))
        else:
            if not stat.S_ISDIR(info.st_mode):
                raise NotADirectoryError(path)
            signature.append((path_key(str(path)), info.st_mtime_ns, info.st_ino))
    return tuple(signature)


def scan_deployment_inventory(
    roots, previous=DeploymentInventory(), *, force=False, now=None, should_cancel=None
) -> DeploymentInventory:
    """存储缓存的纯版本投影，失败时保留最后的有效状态。"""
    signature = deployment_signature(roots)
    now = time.monotonic() if now is None else now
    if not force and signature == previous.signature and now - previous.scanned_at < 5:
        return previous
    locations = (
        scan_deployment_identities(roots)
        if should_cancel is None
        else scan_deployment_identities(roots, should_cancel=should_cancel)
    )
    return DeploymentInventory(signature, now, tuple(locations.values()))


@dataclass(frozen=True)
class ScanLocation:
    """描述待扫描分类目录、所属节点和需要跳过的子项名称。"""

    path: str
    node_id: str
    skip_names: tuple[str, ...] = ()
