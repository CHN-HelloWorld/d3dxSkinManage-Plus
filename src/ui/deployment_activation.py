"""可取消的部署激活，工作线程不持有管理器或数据库。

界面接受结果前，私有 INI 监控由工作线程拥有；拒绝和取消也在该线程清理，不阻塞界面。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from PySide6.QtCore import QObject, QThread, Signal, Slot

from src.core.ini_sync import IniSyncManager
from src.core.library_snapshot import (
    DeploymentInventory,
    LibraryRow,
    deployment_signature,
    path_key,
    scan_deployment_inventory,
)
from src.core.mod_identity import IdentityConflict, read_deployment_identity
from src.ui.error_messages import user_error_message
from src.ui.worker_lifecycle import manage_page_worker
from src.utils import fs
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.runtime_metrics import MetricSamples


@dataclass(frozen=True)
class ActivationRequest:
    """保存界面线程捕获的游戏、部署根、探测代次和索引行快照。"""

    game_id: str
    root: str
    source_root: str
    stamp: tuple
    rows: tuple[LibraryRow, ...]


@dataclass
class ActivationResult:
    """承载部署清单、身份位置和待移交的 INI 同步器及注册错误。"""

    inventory: DeploymentInventory = field(default_factory=DeploymentInventory)
    locations: dict = field(default_factory=dict)
    sync: IniSyncManager | None = None
    refs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    changed_ids: tuple[str, ...] = ()


def capture_deployment_activation(manager) -> ActivationRequest:
    """绑定或恢复部署根后，仅由管理器所属线程调用。"""
    root = manager._storage.get_deploy_dir()
    game_id = manager.current_game_id
    if not game_id or root is None:
        return ActivationRequest(game_id, str(root or ""), "", manager.library_probe_stamp, ())
    if manager.library_probe_busy():
        raise RuntimeError(tr("部署环境仍有未完成事务，暂不能激活"))
    return ActivationRequest(
        game_id,
        str(root or ""),
        str(manager._get_library_root()),
        manager.library_probe_stamp,
        manager._index.get_deployment_rows(manager.current_game_id),
    )


def prepare_deployment_activation(request, cancelled) -> ActivationResult:
    """在独立状态上扫描身份并对账和注册 INI。"""
    result = ActivationResult()
    if not request.root or not request.game_id:
        return result

    def check():
        """在扫描与注册边界检查取消请求，取消时抛出中断异常。"""
        if cancelled():
            raise InterruptedError("Deployment activation cancelled")

    roots = {path_key(request.root): Path(request.root)}
    checked_roots = set(roots)
    for row in request.rows:
        check()
        if row.deployed_path:
            root = Path(row.deployed_path).parent
            key = path_key(str(root))
            if key in checked_roots:
                continue
            checked_roots.add(key)
            try:
                os.stat(fs.native_path(root))
            except (FileNotFoundError, NotADirectoryError):
                # 应用关闭期间根目录可能移动，旧位置仅作提示，不要求它们继续存在。
                continue
            roots[key] = root
    check()
    result.inventory = scan_deployment_inventory(tuple(roots.values()), force=True, should_cancel=cancelled)
    result.locations = {loc.mod_id: loc for loc in result.inventory.locations}
    grouped = {}
    for row in request.rows:
        grouped.setdefault(row.mod_id, []).append(row)
    sync = result.sync = IniSyncManager()
    errors = []
    try:
        for mod_id, rows in grouped.items():
            check()
            location = result.locations.get(mod_id)
            if location is None or not location.active:
                continue
            try:
                marker = read_deployment_identity(location.path)
                if marker is None or marker["mod_id"] != mod_id:
                    raise IdentityConflict(tr("INI 同步的部署身份不符：{path}", path=location.path))
                sync.register_mod(
                    mod_id,
                    rows[0].path,
                    str(location.path),
                    source_watch_root=request.source_root,
                    deploy_watch_root=request.root,
                )
                result.refs[mod_id] = tuple(row.mod_id for row in rows)
            except Exception as error:  # 与普通 INI 注册采用相同的错误处理规则。
                errors.append(f"{mod_id}: {error}")
        check()
        if deployment_signature(tuple(roots.values())) != result.inventory.signature:
            raise InterruptedError("Deployment directories changed during activation")
        result.errors = tuple(errors)
        return result
    except BaseException:
        sync.stop()
        raise


class DeploymentActivationWorker(QThread):
    """在独立线程准备部署环境，并等待界面接受或拒绝同步器所有权。"""

    prepared = Signal()

    def __init__(self, request):
        """保存不可变请求快照，并创建用于接收所有权决策的线程事件。"""
        super().__init__()
        self.request = request
        self.result = None
        self.error = None
        self.elapsed_ms = 0.0
        self._decision = threading.Event()
        self._accepted = False

    def run(self):
        """准备部署结果并等待界面决策；未被接受的 INI 同步器由本线程停止。"""
        start = perf_counter()
        try:
            self.result = prepare_deployment_activation(self.request, self.isInterruptionRequested)
            self.elapsed_ms = (perf_counter() - start) * 1000
            if not self.isInterruptionRequested():
                self.prepared.emit()
                self._decision.wait()
        except Exception as error:
            self.error = error
        finally:
            if not self._accepted and self.result is not None and self.result.sync is not None:
                self.result.sync.stop()

    def accept(self):
        """标记结果已移交界面，并唤醒等待决策的工作线程。"""
        self._accepted = True
        self._decision.set()

    def stop(self):
        """请求中断准备工作并解除决策等待，使线程能够执行资源清理。"""
        self.requestInterruption()
        self._decision.set()


class DeploymentActivationController(QObject):
    """协调部署激活请求、过期结果丢弃和后台线程的异步收尾。"""

    settled = Signal(str, object)
    failed = Signal(str, str)
    discarded = Signal(str)
    stopped = Signal()

    def __init__(self, manager, parent=None):
        """绑定管理器并初始化当前请求、存活线程集合及激活耗时记录。"""
        super().__init__(parent)
        self.manager = manager
        self.worker = None
        self._workers = set()
        self._ready_key = None
        self._ready_sync = None
        self.closing = False
        self.metrics = dict(
            capture_ms=MetricSamples(), worker_ms=MetricSamples(), publish_ms=MetricSamples(), discarded=0
        )

    @property
    def busy(self):
        """返回是否仍有等待发布的当前激活请求。"""
        return self.worker is not None

    @property
    def running(self):
        """返回是否存在尚未结束的激活线程，包括已取消的旧请求。"""
        return bool(self._workers)

    def is_current(self, deploy_dir: str) -> bool:
        """重复发布相同设置不重新扫描 INI。"""
        if self.closing or self.manager.library_probe_busy():
            return False
        key = (
            self.manager.current_game_id,
            path_key(deploy_dir) if deploy_dir else "",
            self.manager.library_probe_stamp,
        )
        if self.worker is not None:
            request = self.worker.request
            return key == (request.game_id, path_key(request.root) if request.root else "", request.stamp)
        return (
            self._ready_key is not None
            and key[:2] == self._ready_key[:2]
            and self.manager._ini_sync is self._ready_sync
        )

    def start(self, deploy_dir: str) -> bool:
        """绑定部署根并启动新的快照激活；重复请求直接复用，准备失败时发出 failed 信号。"""
        if self.closing:
            return False
        if self.is_current(deploy_dir):
            return True
        self.cancel()
        self._ready_key = None
        self._ready_sync = None
        start = perf_counter()
        try:
            if deploy_dir and not fs.is_dir(deploy_dir):
                raise FileNotFoundError(tr("部署目录无效，请重新选择"))
            # 待恢复的预设使用原事务流程，普通路径只绑定部署根。
            self.manager.set_deploy_dir(deploy_dir, defer_activation=True)
            request = capture_deployment_activation(self.manager)
        except Exception as error:
            self.failed.emit(self.manager.current_game_id, user_error_message(error))
            return False
        self.metrics["capture_ms"].append((perf_counter() - start) * 1000)
        worker = DeploymentActivationWorker(request)
        self.worker = worker
        self._workers.add(worker)
        manage_page_worker(self, worker)
        # 绑定 QObject 槽，所有者销毁时由 Qt 自动断开连接。
        worker.prepared.connect(self._prepared)
        worker.finished.connect(self._finished)
        worker.start()
        return True

    @Slot()
    def _prepared(self) -> None:
        """在界面线程发布当前结果，接受成功时移交同步器；过期或失败结果交回工作线程清理。"""
        worker = self.sender()
        if self.closing or worker is not self.worker:
            worker.stop()
            return
        start = perf_counter()
        try:
            accepted = self.manager.publish_deployment_activation(worker.request, worker.result)
        except Exception as error:
            worker.stop()
            self.worker = None
            self.failed.emit(worker.request.game_id, user_error_message(error))
            return
        self.metrics["publish_ms"].append((perf_counter() - start) * 1000)
        self.metrics["worker_ms"].append(worker.elapsed_ms)
        self.worker = None
        if accepted:
            self._ready_sync = self.manager._ini_sync
            self._ready_key = (
                worker.request.game_id,
                path_key(worker.request.root) if worker.request.root else "",
                self.manager.library_probe_stamp,
            )
            worker.accept()
            self.settled.emit(worker.request.game_id, worker.result)
        else:
            worker.stop()
            self.metrics["discarded"] += 1
            self.discarded.emit(worker.request.game_id)

    @Slot()
    def _finished(self) -> None:
        """回收结束的线程，报告当前请求错误，并在关闭且所有线程退出时发出 stopped。"""
        worker = self.sender()
        self._workers.discard(worker)
        if worker is self.worker:
            self.worker = None
            if worker.error is not None and not self.closing:
                if isinstance(worker.error, InterruptedError):
                    self.metrics["discarded"] += 1
                    self.discarded.emit(worker.request.game_id)
                else:
                    logger.warning("部署环境激活失败: %s", worker.error)
                    self.failed.emit(worker.request.game_id, str(worker.error))
        worker.deleteLater()
        if self.closing and not self._workers:
            self.stopped.emit()

    def cancel(self):
        """清除可复用的激活标记并取消当前请求，旧线程继续独立完成清理。"""
        self._ready_key = None
        self._ready_sync = None
        if self.worker is not None:
            self.worker.stop()
            self.worker = None

    def close(self):
        """禁止后续发布并通知全部线程停止；线程清空后通过 stopped 报告关闭完成。"""
        self.closing = True
        self._ready_key = None
        self._ready_sync = None
        self.worker = None
        for worker in self._workers:
            worker.stop()
        if not self._workers:
            self.stopped.emit()
