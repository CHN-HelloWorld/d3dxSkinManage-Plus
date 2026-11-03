"""归档工作并发执行，共享启用配置串行提交。"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from pathlib import Path
from time import perf_counter

from PySide6.QtCore import QObject, QThread, Signal

from src.core.deployment_enable import EnablePreparation
from src.ui.worker_lifecycle import manage_page_worker
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.qt_timers import call_later
from src.utils.runtime_metrics import MetricSamples


class _EnableIO(QThread):
    """执行一个独立文件操作并保存返回值或异常，供界面线程继续事务。"""

    def __init__(self, work):
        """保存无参数工作回调，并初始化结果与错误容器。"""
        super().__init__()
        self.work, self.result, self.error = work, None, None

    def run(self):
        """在工作线程执行回调，将返回值或异常保存到实例而不直接更新界面。"""
        try:
            self.result = self.work()
        except Exception as error:
            self.error = error

    def stop(self):
        # 配置事务原子完成或回滚，关闭时等待，不中断共享偏好写入。
        """标记关闭请求；当前原子操作仍由自身完成或回滚。"""
        self.requestInterruption()


class EnablePublicationController(QObject):
    """串行协调冲突停用、启用准备、界面发布与失败恢复。"""

    idle = Signal()
    stopped = Signal()

    def __init__(self, manager, completion, parent=None):
        """绑定管理器和发布回调，建立启用队列及回滚恢复队列。"""
        super().__init__(parent)
        self.manager = manager
        self.completion = completion
        self.closing = False
        self._queue = deque()
        self._active = None
        self._worker = None
        self._restore_queue = deque()
        self.metrics = {"capture_ms": MetricSamples(), "worker_ms": MetricSamples(), "publish_ms": MetricSamples()}

    @property
    def busy(self):
        """返回是否存在正在处理或等待处理的启用请求。"""
        return self._active is not None or bool(self._queue)

    def enqueue(self, mod_id, deploy_worker, disabled_ids):
        """保护本次启用及冲突 Mod 的路径与身份，再将未重复入队的部署结果加入发布队列。"""
        if getattr(deploy_worker, "_enable_queued", False):
            return
        # 显示结果可能释放归档预留，配置和部署回滚完成前仍保留独立所有权并延后监控事件。
        ids = tuple(dict.fromkeys((mod_id, *disabled_ids)))
        mods = self.manager._index.get_mods_by_ids(ids)
        paths = [path for mod in mods for path in (Path(mod.path).parent, mod.deployed_path) if path]
        paths.append(deploy_worker.result[1])
        if getattr(deploy_worker, "_stage_only", False):
            staged = Path(deploy_worker.result[1])
            active_name = self.manager._storage.active_name_from_parked(staged, mod_id)
            paths.append(staged.with_name(active_name))
        self.manager.protect_library_operation("enable-publication:" + mod_id, paths=paths, mod_ids=ids)
        deploy_worker._enable_queued = True
        self._queue.append((mod_id, deploy_worker, list(disabled_ids)))
        self._next()

    def _run_io(self, work, done):
        """托管低优先级工作线程，完成后将结果传给后续事务步骤。"""
        started = perf_counter()
        worker = _EnableIO(work)
        self._worker = worker
        manage_page_worker(self, worker)

        def finished():
            """记录后台耗时并交付结果；回调异常转入统一失败处理，最后安排线程对象释放。"""
            self.metrics["worker_ms"].append((perf_counter() - started) * 1000)
            self._worker = None
            try:
                done(worker)
            except Exception as error:
                self._deliver(error)
            finally:
                worker.deleteLater()

        worker.finished.connect(finished)
        worker.start(QThread.Priority.LowPriority)

    def _next(self):
        """取出下一项启用请求，先准备冲突停用；关闭期间取消排队请求，队列清空时报告空闲。"""
        if self._active is not None:
            return
        if not self._queue:
            self.idle.emit()
            if self.closing:
                self.stopped.emit()
            return
        self._active = self._queue.popleft()
        mid, worker, disabled = self._active
        if self.closing:
            self._deliver(InterruptedError(tr("已取消排队的启用请求")))
            return
        try:
            if disabled:
                preparation = self.manager.create_deployment_preparation(trigger="mod_disable", mod_ids=disabled)
                self._run_io(preparation.prepare, lambda io: self._disabled_prepared(io, preparation))
            else:
                self._prepare_enable()
        except Exception as error:
            self._deliver(error)

    def _disabled_prepared(self, io, preparation):
        """验证停用准备结果和条目完整性，再以后台操作停放冲突部署。"""
        if io.error is not None:
            self._deliver(io.error)
            return
        mid, worker, disabled = self._active
        entries = self.manager.prepare_disable_batch(disabled, preparation=preparation)
        if len(entries) != len(disabled):
            self._deliver(RuntimeError(tr("无法确认需要停用的 Mod")))
            return
        # 该后台禁用入口只使用存储、同步器和独立凭据，不访问数据库。
        step = self.manager.disable_batch_step

        def disable():
            """依次停放冲突 Mod，遇到首个失败即停止，并返回已执行条目的结果。"""
            results = []
            for entry in entries:
                result = step(replace(entry, park=True))
                results.append(result)
                if not result.ok:
                    break
            return results

        self._run_io(disable, self._disabled)

    def _disabled(self, io):
        """在界面线程提交停用结果，确认所有冲突均已解除后继续启用准备。"""
        if io.error is not None:
            self._deliver(io.error)
            return
        mid, worker, disabled = self._active
        worker.switched_ids = self.manager.commit_disable_batch(io.result)
        if len(worker.switched_ids) != len(disabled):
            self._deliver(RuntimeError(tr("停用冲突 Mod 未完成")))
            return
        self._prepare_enable()

    def _prepare_enable(self):
        """捕获启用准备所需状态，并在后台执行配置与部署路径调整。"""
        mid, worker, disabled = self._active
        started = perf_counter()
        preparation = EnablePreparation(
            self.manager,
            mid,
            worker.result[1],
            stage_only=getattr(worker, "_stage_only", False),
            source_receipt=getattr(worker, "source_receipt", None),
        )
        worker.enable_preparation = preparation
        self.metrics["capture_ms"].append((perf_counter() - started) * 1000)
        self._run_io(preparation.run, lambda io: self._deliver(io.error))

    def _deliver(self, error=None):
        """将准备结果交给界面发布回调；未发布时启动部署回滚并恢复此前停用的 Mod。"""
        if self._active is None:
            return
        mid, worker, disabled = self._active
        preparation = getattr(worker, "enable_preparation", None)
        worker.configuration_prepared = True
        worker.configuration_error = error
        worker.rollback_managed = True
        if preparation is not None:
            worker.result = (worker.result[0], preparation.deploy_path)
            worker.source_receipt = preparation.source_receipt
        rollback = (
            mid,
            worker.result[1],
            getattr(worker, "created_deployment", False),
            tuple(getattr(worker, "switched_ids", ())),
        )
        started = perf_counter()
        try:
            self.completion(mid, worker, disabled)
        except Exception:
            logger.exception("发布后台启用结果失败 (%s)", mid)
        self.metrics["publish_ms"].append((perf_counter() - started) * 1000)
        if preparation is None or not preparation.published:
            self._restore_queue = deque(rollback[3])
            storage = self.manager._storage

            def undo():
                """撤销未发布的配置，核验身份后删除本次部署或停放已有部署，并汇总回滚问题。"""
                problems = list(preparation.rollback() if preparation is not None else ())
                mid, path, created, _ = rollback
                if path:
                    try:
                        storage._check_deployment_identity(path, mid)
                        if created:
                            storage.remove_deploy_path(path)
                        elif not Path(path).name.upper().startswith("DISABLED"):
                            storage.park_deployment(path, mid)
                    except (OSError, ValueError) as failure:
                        problems.append(str(failure))
                return tuple(problems)

            self._run_io(undo, self._rolled_back)
        else:
            self._finish()

    def _rolled_back(self, io):
        """记录回滚失败信息，再继续恢复先前启用的冲突 Mod。"""
        if io.error or io.result:
            logger.error("回滚未发布的启用配置失败: %s", io.error or io.result)
        self._restore_previous()

    def _restore_previous(self):
        """逐项准备恢复先前启用的 Mod；缺失或已启用条目跳过，单项失败不阻断其余恢复。"""
        if not self._restore_queue:
            self._finish()
            return
        mid = self._restore_queue.popleft()
        try:
            mod = self.manager.get_mod(mid)
            if mod is None or mod.is_enabled:
                self._restore_previous()
                return
            preparation = EnablePreparation(self.manager, mid, mod.deployed_path, stage_only=True)
            self._run_io(preparation.run, lambda io: self._restored_previous(io, preparation))
        except Exception as error:
            logger.error("恢复原启用 Mod 失败 (%s): %s", mid, error)
            self._restore_previous()

    def _restored_previous(self, io, preparation):
        """提交单项恢复结果；发布失败时撤销准备并停放部署，随后处理下一项。"""
        try:
            if io.error:
                raise io.error
            self.manager.finalize_enable(
                preparation.mod_id,
                True,
                deploy_path=preparation.deploy_path,
                source_receipt=preparation.source_receipt,
                preparation=preparation,
            )
        except Exception as error:
            logger.error("发布原启用 Mod 恢复失败 (%s): %s", preparation.mod_id, error)
        if not preparation.published:
            storage = self.manager._storage

            def undo():
                """撤销恢复准备并将未成功发布的部署重新停放。"""
                errors = preparation.rollback()
                if not Path(preparation.deploy_path).name.upper().startswith("DISABLED"):
                    storage.park_deployment(preparation.deploy_path, preparation.mod_id)
                return errors

            self._run_io(undo, lambda _: self._restore_previous())
        else:
            self._restore_previous()

    def _finish(self):
        """释放当前启用事务的库操作保护，并在下一轮事件循环推进队列。"""
        if self._active is not None:
            self.manager.release_library_operation("enable-publication:" + self._active[0])
        self._active = None
        call_later(0, self, self._next)

    def close(self):
        """标记控制器关闭；空闲时立即报告停止，已有事务由队列完成收尾。"""
        self.closing = True
        if not self.busy:
            self.stopped.emit()
