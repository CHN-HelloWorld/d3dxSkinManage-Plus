"""工作线程处理目标元数据，所有者校验身份后更新数据库。"""

import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from src.core.mod_package import read_container_marker, read_sidecar, restore_preview_references, write_container_marker
from src.core.preview_cache_index import capture_source_version
from src.models.mod import Mod
from src.utils import fs
from src.utils.logger import logger


@dataclass(frozen=True)
class MetadataRequest:
    """保存单个 Mod 元数据读取所需的身份、路径、导入时间和预览位置快照。"""

    mod_id: str
    game_id: str
    path: str
    container: str
    imported_at: str
    resource_dir: str
    node_root: str
    cache_root: str


def _metadata_version(container):
    """返回身份文件和元数据文件的统计签名，缺失项以 None 表示。"""
    result = []
    for path in (Path(container) / ".d3dxmod", Path(container) / ".d3dxmeta" / "info.json"):
        try:
            info = Path(fs.native_path(path)).stat()
            result.append((info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
        except FileNotFoundError:
            result.append(None)
    return tuple(result)


def capture_request(manager, mid):
    """在管理器所属线程捕获读取快照；Mod 不存在或正在执行操作时返回 None。"""
    mod = manager.get_mod(mid)
    if mod is None or mid in manager.inflight_mod_ids():
        return None
    return MetadataRequest(
        mid,
        mod.game_id,
        mod.path,
        str(manager._index.paths.container_path(mid)),
        mod.imported_at,
        manager._res_dir(mod.game_id),
        str(manager._index.paths.node_path(mod.node_id)) if mod.node_id else "",
        str(Path(manager._index.paths.preview_roots[mod.game_id]) / mid),
    )


def _root_version(path):
    """读取来源根对象的类型、文件身份、大小和时间戳，用于发布前核验。"""
    info = Path(fs.native_path(path)).stat()
    return (info.st_mode, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_metadata(request, find_previews):
    """工作函数不读取数据库或当前游戏状态。"""
    version = capture_source_version(request.path)
    root_version = _root_version(request.path)
    metadata_version = _metadata_version(request.container)
    marker = read_container_marker(request.container)
    if marker is None or marker["mod_id"] != request.mod_id or marker["imported_at"] != request.imported_at:
        raise ValueError("metadata refresh identity changed")
    sidecar = read_sidecar(request.path)
    content_hash = Mod.compute_sha1(request.path)
    if not content_hash:
        raise ValueError("metadata refresh fingerprint is empty")
    previews = (
        find_previews(
            request.path,
            mod_id=request.mod_id,
            resource_dir=request.resource_dir,
            content_hash=content_hash,
            source_version=version,
            cache_index={},
        )
        or []
    )
    previews = restore_preview_references(
        sidecar, previews, container=request.container, preview_cache=request.cache_root, node=request.node_root or None
    )
    if capture_source_version(request.path) != version or _metadata_version(request.container) != metadata_version:
        raise ValueError("source changed while refreshing metadata")
    return request, version, metadata_version, marker, sidecar, content_hash, previews, root_version


def publish_metadata(manager, result):
    """核验来源、元数据版本和 Mod 身份后，将后台读取结果更新到索引。

    过期或无变化的结果返回 False；身份标记更新后若索引保存失败，则恢复原标记，成功时发布元数据变化事件。"""
    request, version, metadata_version, marker, sidecar, content_hash, previews, root_version = result
    mod = manager.get_mod(request.mod_id)
    if (
        mod is None
        or mod.game_id != request.game_id
        or mod.path != request.path
        or mod.imported_at != request.imported_at
        or mod.mod_id in manager.inflight_mod_ids()
    ):
        return False
    if _root_version(mod.path) != root_version or _metadata_version(request.container) != metadata_version:
        return False
    # 工作线程已重复核对目录成员，控制器同时检查监控版本，所有者阶段保持常数开销。
    if not stat.S_ISDIR(root_version[0]) and capture_source_version(mod.path) != version:
        return False
    before = mod.to_dict()
    mod.content_hash = content_hash
    mod.preview_images = manager._order_previews(previews, sidecar or {})
    if sidecar is not None:
        if "name" in sidecar:
            mod.name = sidecar["name"]
        # 读取完整元数据投影，包括明确清空的字段。
        payload = mod.to_dict()
        payload["info"] = sidecar.get("info") or {}
        mod.info = Mod.from_dict(payload).info
    marker_path = Path(fs.native_path(Path(request.container) / ".d3dxmod"))
    old_marker = marker_path.read_bytes()
    changed_marker = marker["content_hash"] != content_hash
    if not changed_marker and mod.to_dict() == before:
        # 监控也会报告自身保存结果，不重复写入记录或发送元数据变化通知。
        return False
    if changed_marker:
        write_container_marker(
            Path(request.container), marker["body"], content_hash, mod_id=mod.mod_id, imported_at=mod.imported_at
        )
    if not manager._index.update_mod(mod):
        if changed_marker:
            marker_path.write_bytes(old_marker)
        return False
    manager._emit("mod_metadata_changed", mod_id=mod.mod_id, game_id=mod.game_id)
    return True


class LibraryMetadataController(QObject):
    """合并局部元数据刷新请求，在单线程池读取并在界面线程核验发布。"""

    completed = Signal(object)

    def __init__(self, manager, publish, parent=None):
        """创建单工作线程池、待刷新集合和队列信号，并初始化请求代次。"""
        super().__init__(parent)
        self.manager, self.publish = manager, publish
        self.pending, self.active = set(), None
        self.closing, self.future = False, None
        self.generation = 0
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-metadata")
        self._start_timer = QTimer(self)
        self._start_timer.setSingleShot(True)
        self._start_timer.timeout.connect(self._start)
        self.completed.connect(self._finish, Qt.ConnectionType.QueuedConnection)

    def schedule(self, mod_ids):
        """合并 Mod 标识并安排刷新；已关闭的控制器忽略新请求。"""
        if self.closing:
            return
        self.pending.update(mod_ids)
        if self.active is None and not self._start_timer.isActive():
            self._start_timer.start(0)

    def _start(self):
        """等待库事务空闲后捕获一个请求及探测版本，再提交后台读取。"""
        if self.closing or self.active is not None or not self.pending:
            return
        if self.manager.library_probe_busy():
            # 标签编辑先写元数据后提交数据库，避免读取中间状态并将自身保存误判为冲突。
            self._start_timer.start(50)
            return
        stamp = self.manager.library_probe_stamp
        mid = self.pending.pop()
        request = capture_request(self.manager, mid)
        if request is None:
            self._start_timer.start(0)
            return
        self.active = mid
        future = self.future = self.pool.submit(read_metadata, request, self.manager._find_previews)
        future.metadata_generation = self.generation
        future.metadata_stamp = stamp

        def finished(job):
            """将线程池完成通知排入 Qt 事件队列，所属窗口已销毁时忽略通知。"""
            try:
                self.completed.emit(job)
            except RuntimeError:
                pass  # 文件工作完成时所属窗口已经销毁。

        future.add_done_callback(finished)

    def _finish(self, future):
        """忽略旧代次结果，重排跨越库操作的读取，并仅发布仍属当前游戏的有效元数据。"""
        if future.metadata_generation != self.generation or future is not self.future:
            return
        mid, self.active = self.active, None
        self.future = None
        if self.closing:
            return
        try:
            if self.manager.library_probe_busy() or future.metadata_stamp != self.manager.library_probe_stamp:
                # 跨越保存操作的读取结果无效，提交或回滚后重新读取。
                if future.metadata_stamp[0] == self.manager.current_game_id:
                    self.pending.add(mid)
            else:
                result = future.result()
                if (
                    mid not in self.pending
                    and result[0].game_id == self.manager.current_game_id
                    and publish_metadata(self.manager, result)
                ):
                    from src.core.library_publication import LocalPublication

                    self.publish(LocalPublication(changed_ids={mid}))
        except Exception as error:
            logger.warning("局部元数据刷新保留原记录 mod=%s reason=%s", mid, error)
        self._start_timer.start(0)

    def stop(self):
        """使在途结果失效并停止新调度，取消未开始任务且不阻塞等待线程池退出。"""
        self.closing = True
        self.generation += 1
        self._start_timer.stop()
        self.pending.clear()
        if self.future is not None:
            self.future.cancel()
        self.active, self.future = None, None
        self.pool.shutdown(wait=False, cancel_futures=True)

    def resume(self):
        """在停止后重建单线程池，允许重新接收元数据刷新请求。"""
        if self.closing:
            self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-metadata")
            self.closing = False
