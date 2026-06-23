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


@dataclass(frozen=True)
class RelocatedSource:
    """保存由持久身份重新定位得到的 Mod 正文路径及所属分类。"""

    mod_id: str
    body: str
    node_id: str


@dataclass(frozen=True)
class ProbeRequest:
    """冻结一次后台探测的路径、记录、缓存和事件范围，避免跨线程访问可变管理器。"""

    token: ProbeToken
    rows: tuple[LibraryRow, ...]
    library_root: str
    deploy_roots: tuple[str, ...]
    cache_root: str
    scan_locations: tuple[ScanLocation, ...]
    inventory: DeploymentInventory
    # 当前部署记录的冻结投影，用于变化检测。
    records: tuple[tuple[str, str, str, str], ...] = ()
    mode: str = "full"
    source_inventory: object = None
    changed_paths: tuple[str, ...] = ()
    events: tuple = ()
    force_source_audit: bool = False


@dataclass(frozen=True)
class CacheEntry:
    """记录缓存对象的身份、时间、类型和重解析属性，供清理前重新核对。"""

    path: str
    inode: int
    device: int
    modified_ns: int
    directory: bool
    reparse: bool


@dataclass(frozen=True)
class CacheCandidate:
    """保存待清理缓存路径及扫描时的对象凭据集合。"""

    path: str
    entries: tuple[CacheEntry, ...]


@dataclass(frozen=True)
class ProbeResult:
    """汇总后台探测的路径状态、身份盘点和清理候选，并保留错误及取消状态。"""

    request: ProbeRequest
    checks: tuple[PathCheck, ...] = ()
    inventory: DeploymentInventory = DeploymentInventory()
    relocated: tuple[RelocatedSource, ...] = ()
    cache_candidates: tuple[CacheCandidate, ...] = ()
    source_signature: tuple[tuple[str, int, int], ...] = ()
    error: str = ""
    cancelled: bool = False
    source_inventory: object = None

    @property
    def token(self):
        """返回原探测请求的身份令牌，供所有者核对结果是否仍有效。"""
        return self.request.token


@dataclass(frozen=True)
class ProbeApplyStep:
    """描述探测发布的单步阶段、目标身份、变化状态和移除数量。"""

    phase: str
    item_id: str = ""
    changed: bool = False
    removed: int = 0


def protected_cache_names(rows, cache_root: str, *, should_cancel=None) -> frozenset[str]:
    """复用不可变记录的纯投影，不缓存文件系统证据。

    调用方可能传入相对路径，因此根目录和工作目录参与缓存键。
    引用、所有者、根目录或路径变化时，不复用旧保护范围。
    """
    root, cwd = path_key(cache_root), os.getcwd()
    protected = set()
    for row in rows:
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Cache protection collection cancelled")
        protected.update(_cached_protected_cache_names(row.mod_id, row.preview_images, root, cwd))
    return frozenset(protected)


@lru_cache(maxsize=32768)
def _cached_protected_cache_names(mod_id: str, preview_images: tuple[str, ...], root: str, _cwd: str) -> frozenset[str]:
    """仅缓存当前所有者的不可变名称，不依赖其他记录字段。"""
    protected = {mod_id}
    for image in preview_images:
        if not image:
            continue
        key = path_key(image)
        try:
            if os.path.commonpath((root, key)) == root:
                parts = Path(os.path.relpath(key, root)).parts
                if parts and parts[0] not in (".", ".."):
                    protected.add(parts[0])
        except ValueError:
            continue
    # Windows 文件系统名称比较不区分大小写。
    return frozenset(os.path.normcase(name) for name in protected)


def strict_is_dir(path) -> bool:
    """保留访问异常，不将其静默转换为路径不存在。"""
    try:
        return stat.S_ISDIR(os.stat(fs.native_path(path)).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False


def _source_directories(path, should_cancel):
    """复用目录项类型，避免逐个查询子项元数据。"""
    with os.scandir(fs.native_path(path)) as entries:
        for entry in entries:
            if should_cancel():
                raise InterruptedError("Source relocation cancelled")
            if not entry.name.startswith(".") and entry.is_dir():
                yield Path(fs.normal_path(entry.path))


def bounded_identity_reads(entries, read, should_cancel):
    """按输入顺序执行身份读取，最多四个读取线程，每批最多提交 32 项。

    关闭迭代器、取消或读取失败时等待活动任务结束；提前停止消费时须显式关闭生成器。
    """
    entries = iter(entries)
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="source-identity")

    def read_current(entry):
        """在读取单个身份条目前后检查取消请求，防止继续发布已取消任务的结果。"""
        if should_cancel():
            raise InterruptedError("Source identity read cancelled")
        result = read(entry)
        if should_cancel():
            raise InterruptedError("Source identity read cancelled")
        return result

    try:
        while True:
            if should_cancel():
                raise InterruptedError("Source identity read cancelled")
            batch = tuple(islice(entries, 32))
            if not batch:
                break
            futures = [pool.submit(copy_context().run, read_current, entry) for entry in batch]
            for future in futures:
                if should_cancel():
                    raise InterruptedError("Source identity read cancelled")
                yield future.result()
    finally:
        try:
            close = getattr(entries, "close", None)
            if close is not None:
                close()
        finally:
            # 辅助任务必须随探测结束，不能访问已经切换的文件库。
            pool.shutdown(wait=True, cancel_futures=True)


def plan_cache_removal(path: str, should_cancel) -> CacheCandidate:
    """仅在工作线程枚举，不跟随目录联接或符号链接。"""
    pending, entries = [path], []
    while pending:
        if should_cancel():
            raise InterruptedError("Cache probe cancelled")
        item = pending.pop()
        info = os.lstat(fs.native_path(item))
        directory = stat.S_ISDIR(info.st_mode)
        reparse = stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)
        entries.append(CacheEntry(item, info.st_ino, info.st_dev, info.st_mtime_ns, directory, reparse))
        if directory and not reparse:
            pending.extend(str(child) for child in fs.list_children(item))
    return CacheCandidate(path, tuple(reversed(entries)))


def remove_cache_entry(entry: CacheEntry, ancestors: dict[str, CacheEntry]) -> bool:
    """按当前清单执行有限删除，保留被替换或新增的内容供下次扫描。"""
    try:
        parent = Path(entry.path).parent
        while path_key(str(parent)) in ancestors:
            expected = ancestors[path_key(str(parent))]
            current = os.lstat(fs.native_path(parent))
            if (current.st_ino, current.st_dev) != (expected.inode, expected.device):
                return False
            parent = parent.parent
        current = os.lstat(fs.native_path(entry.path))
        if (current.st_ino, current.st_dev) != (entry.inode, entry.device):
            return False
        if not entry.directory and current.st_mtime_ns != entry.modified_ns:
            return False
        operation = os.rmdir if entry.directory else os.unlink
        try:
            operation(fs.native_path(entry.path))
        except PermissionError:
            # 预览副本可能保留 Windows 只读属性；修改权限并重试前重新核对身份，
            # 不得修改已被替换的文件。
            current = os.lstat(fs.native_path(entry.path))
            if (current.st_ino, current.st_dev) != (entry.inode, entry.device) or entry.reparse:
                return False
            os.chmod(fs.native_path(entry.path), stat.S_IWRITE)
            operation(fs.native_path(entry.path))
        return True
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False


def probe_library(request: ProbeRequest, *, should_cancel=lambda: False) -> ProbeResult:
    """只读取文件系统，返回不持有数据库或管理器的不可变探测结果。"""
    try:
        if should_cancel():
            return ProbeResult(request, cancelled=True)
        from src.core.library_inventory import probe_with_inventory

        return probe_with_inventory(request, should_cancel)
    except InterruptedError:
        return ProbeResult(request, cancelled=True)
    except Exception as error:
        # 读取失败不能作为空清单授权后续删除。
        return ProbeResult(request, error=f"{type(error).__name__}: {error}")
