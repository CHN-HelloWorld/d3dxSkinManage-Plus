"""串行执行插件写操作，共用宿主的部署原语。

工作线程只执行文件读写；规划、数据库、预设、事件和完成回调在创建服务的界面线程处理。
"""

from __future__ import annotations

import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from src.core.config import Config
from src.plugin_sdk.operations import OperationResult, OperationTask
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.qt_timers import call_later


class _IOWorker(QThread):
    """执行插件操作的后台文件任务，并通过信号请求所有者线程处理持久状态。"""

    progress = Signal(str, float)
    owner_call = Signal(object)

    def __init__(self, run_io, parent):
        """绑定文件任务并初始化结果、错误、所有者调用和取消控制状态。"""
        super().__init__(parent)
        self._run_io = run_io
        self.result = None
        self.error = ""
        self.exception = None
        self.pending_call = None
        self.must_finish = False
        self.cancelled_on_start = False

    def call_owner(self, operation, *args):
        """发送需在所有者线程执行的操作并等待确认，将所有者异常传回工作线程。"""
        from types import SimpleNamespace

        call = SimpleNamespace(operation=operation, args=args, done=threading.Event(), error=None)
        self.pending_call = call
        self.owner_call.emit(call)
        call.done.wait()
        self.pending_call = None
        if call.error is not None:
            raise call.error

    def run(self):
        """执行后台文件任务，启动前取消及运行异常均保存为任务失败信息。"""
        try:
            if self.cancelled_on_start:
                raise InterruptedError(tr("操作已取消"))
            self.result = self._run_io(self)
        except Exception as error:
            self.exception = error
            self.error = str(error)
            logger.exception("插件后台文件操作失败")


@dataclass
class _Request:
    """保存插件操作的所有者、游戏、参数、成员和跨阶段工作状态。"""

    owner: str
    operation: str
    game_id: str
    task: OperationTask
    args: dict
    ids: tuple[str, ...] = ()
    worker: _IOWorker | None = None
    kind: str = ""
    entries: list = field(default_factory=list)
    state: dict = field(default_factory=dict)


class ModOperationService(QObject):
    """在宿主线程串行调度插件写操作，协调后台文件任务和所有者状态提交。"""

    activity_changed = Signal(bool)
    started = Signal(str, object)
    finished = Signal(object)
    progress = Signal(str, float)

    def __init__(self, manager, parent=None, *, host_busy=None, error_sink=None):
        """绑定管理器、忙碌检查和错误接收器，初始化串行队列及预设变体执行服务。"""
        super().__init__(parent)
        self._m = manager
        self._host_busy = host_busy or (lambda: False)
        self._error_sink = error_sink
        self._thread_id = threading.get_ident()
        self._queue = deque()
        self._active = None
        self._stopping = False
        self._owned_tasks = {}
        from src.core.preset_variant_operations import PresetVariantOperations

        self._preset_variants = PresetVariantOperations(self, manager)

    @property
    def busy(self):
        """判断服务是否有正在执行或等待调度的请求。"""
        return self._active is not None or bool(self._queue)

    @property
    def game_id(self):
        """返回当前活动或首个排队请求所属游戏，无请求时返回空字符串。"""
        request = self._active or (self._queue[0] if self._queue else None)
        return request.game_id if request else ""

    def _assert_thread(self):
        """要求调用发生在服务创建线程，跨线程请求直接抛出运行错误。"""
        if threading.get_ident() != self._thread_id:
            raise RuntimeError("Mod operations must be requested on the host thread")

    def submit(self, owner: str, operation: str, **args) -> OperationTask:
        """创建可取消任务并加入串行队列；宿主退出期间立即返回取消结果。"""
        self._assert_thread()
        task = OperationTask(error_sink=(lambda error: self._error_sink(owner, error)) if self._error_sink else None)
        ids = (args["mod_id"],) if args.get("mod_id") else ()
        request = _Request(owner, operation, self._m.current_game_id, task, args, ids)
        self._owned_tasks.setdefault(owner, weakref.WeakSet()).add(task)
        task._cancel = lambda: self._cancel_request(request)
        if self._stopping:
            task._finish(OperationResult(request.game_id, cancelled_ids=ids, error=tr("宿主正在退出")))
            return task
        was_busy = self.busy
        self._queue.append(request)
        if not was_busy:
            self.activity_changed.emit(True)
        call_later(0, self, self._drain)
        return task

    def _cancel_request(self, request):
        """向可中断的活动工作线程发送取消请求，必须完成的收尾任务继续运行。"""
        self._assert_thread()
        if request.worker is not None and not request.worker.must_finish:
            request.worker.requestInterruption()

    def cancel_owner(self, owner: str):
        """分离指定插件的任务回调，并取消其排队和活动请求。"""
        self._assert_thread()
        for task in self._owned_tasks.pop(owner, ()):
            task._detach()
        for request in list(self._queue) + ([self._active] if self._active else []):
            if request.owner == owner:
                request.task._detach()
                request.task.cancel()

    def cancel_all(self):
        """向全部排队和活动请求发出取消请求。"""
        self._assert_thread()
        for request in list(self._queue) + ([self._active] if self._active else []):
            request.task.cancel()

    def _complete(self, request, *, succeeded=(), failed=(), cancelled=(), error="", preset_id=None, variant_id=None):
        """释放请求预留并发布结果，先刷新宿主信号再完成插件回调，随后继续调度。"""
        self._preset_variants.release(request)
        result = OperationResult(
            request.game_id,
            tuple(succeeded),
            tuple(failed),
            tuple(cancelled),
            error,
            preset_id if preset_id is not None else request.args.get("preset_id", ""),
            variant_id if variant_id is not None else request.args.get("variant_id"),
        )
        if self._active is request:
            self._active = None
        # 插件回调可能打开嵌套事件循环并完成其他请求，须先刷新宿主反馈，避免旧提示覆盖新任务。
        self.finished.emit(result)
        request.task._finish(result)
        if not self.busy:
            self.activity_changed.emit(False)
        call_later(0, self, self._drain)

    def _drain(self):
        """取出下一个请求并检查取消、游戏与宿主占用状态，启动失败时发布失败结果。"""
        if self._active is not None or not self._queue:
            return
        request = self._queue.popleft()
        self._active = request
        if request.task._cancel_requested or self._stopping:
            self._complete(request, cancelled=request.ids, error=tr("操作已取消"))
            return
        try:
            if not request.game_id or request.game_id != self._m.current_game_id:
                raise ValueError(tr("游戏已切换，请重新发起操作"))
            if self._host_busy() or self._m.preset_manager.has_pending_apply(request.game_id):
                raise ValueError(tr("宿主正在处理其他 Mod 任务，请稍后重试"))
            self._start(request)
        except Exception as error:
            logger.warning("插件操作未完成: %s", error)
            from src.core.operation_preflight import OperationPreflightError

            if isinstance(error, OperationPreflightError):
                self._m.report_operation_failure(error)
            self._complete(request, failed=request.ids, error=str(error))

    def _start(self, request):
        """按操作类型验证目标并调度预设、删除、启用或禁用流程。"""
        args, operation = request.args, request.operation
        if self._cancel_before_mutation(request):
            return
        if operation.startswith(("preset_", "variant_")):
            self._preset_variants.start(request)
            return
        if operation == "disable_all":
            mods = self._m.get_mods(args.get("node_id"))
            request.ids = tuple(m.mod_id for m in mods if m.is_enabled)
            self.started.emit(operation, request.ids)
            if self._cancel_before_mutation(request):
                return
            self._disable(request)
            return
        mod_id = args.get("mod_id", "")
        mod = self._m.get_mod(mod_id)
        if mod is None or self._m._index.get_mod_game_id(mod_id) != request.game_id:
            raise ValueError(tr("Mod 不存在或不属于当前游戏"))
        self.started.emit(operation, request.ids)
        if self._cancel_before_mutation(request):
            return
        if operation == "delete_mod":
            policy = str(Config().get("mods.delete_method", "permanent"))
            ok = self._m.delete_mod(mod_id, delete_method=policy)
            self._complete(
                request,
                succeeded=request.ids if ok else (),
                failed=() if ok else request.ids,
                error="" if ok else tr("删除失败"),
            )
            return
        if operation not in ("toggle_mod", "set_enabled"):
            raise ValueError(tr("未知操作: {operation}", operation=operation))
        receipt = self._m.require_mod_operation(mod_id, metadata=True, deployment=True)
        enabled = not mod.is_enabled if operation == "toggle_mod" else bool(args["enabled"])
        if mod.is_enabled == enabled:
            self._complete(request, succeeded=request.ids)
        elif not enabled:
            self._disable(request)
        else:
            if self._m._storage.get_deploy_dir() is None:
                raise ValueError(tr("请先配置 Mod 部署目录"))
            # 直接恢复停放副本，避免再次切换操作在重新对账后反转目标状态。
            restored = self._m._restore_parked_deployment(mod)
            if restored is not None:
                changed = self._m.finalize_enable(mod_id, True, deploy_path=str(restored), source_receipt=receipt)
                ok = changed is not None and changed.is_enabled
                if not ok:
                    self._m._storage._check_deployment_identity(restored, mod_id)
                    import os

                    os.replace(str(restored), mod.deployed_path)
                self._complete(
                    request,
                    succeeded=request.ids if ok else (),
                    failed=() if ok else request.ids,
                    error="" if ok else tr("启用失败"),
                )
                return
            # 仅向工作线程传递不可变的路径和身份信息。
            storage = self._m._storage
            source, identity = mod.path, mod.mod_id
            name = mod.folder_name or Path(source).stem

            def deploy(worker):
                """在后台物化部署并重新核对源身份，源失效时移除本次新部署。"""
                from src.core.operation_preflight import confirm_source

                receipt = confirm_source(source, identity)
                last_report = 0.0

                def report(fraction):
                    """以节流方式发送部署复制进度，并确保完成进度立即通知。"""
                    nonlocal last_report
                    now = time.monotonic()
                    if fraction >= 1.0 or now - last_report >= 0.05:
                        last_report = now
                        worker.progress.emit(mod_id, float(fraction))

                target = storage.materialize_deployment(
                    source, identity, name, progress_cb=report, should_cancel=worker.isInterruptionRequested
                )
                try:
                    receipt.recheck()
                except OSError:
                    if target is not None:
                        storage._check_deployment_identity(target, identity)
                        storage.remove_deploy_path(target)
                    raise
                worker.source_receipt = receipt
                return target

            request.kind = "enable"
            self._start_worker(request, deploy)

    def _disable(self, request):
        """准备全部目标的禁用快照并核对成员集合，随后启动后台逐项禁用。"""
        if not request.ids:
            self._complete(request)
            return
        request.entries = self._m.prepare_disable_batch(request.ids)
        if self._cancel_before_mutation(request):
            return
        if {e.mod_id for e in request.entries} != set(request.ids):
            raise ValueError(tr("无法准备禁用操作，部署身份或 Mod 状态已变化"))
        entries = tuple(request.entries)
        step = self._m.disable_batch_step

        def disable(worker):
            """逐项执行禁用文件操作并收集成功或错误结果，响应中断并报告完成比例。"""
            from src.core.mod_manager import DisableStepResult

            results = []
            for entry in entries:
                if worker.isInterruptionRequested():
                    break
                try:
                    results.append(step(entry))
                except Exception as error:
                    results.append(DisableStepResult(mod_id=entry.mod_id, error=str(error)))
                worker.progress.emit(entry.mod_id, len(results) / len(entries))
            return results

        request.kind = "disable"
        self._start_worker(request, disable)

    def _cancel_before_mutation(self, request):
        """同步宿主事件可能在文件操作开始前停用插件所有者。"""
        if self._active is not request or request.task.done:
            return True
        if request.task._cancel_requested or self._stopping:
            self._complete(request, cancelled=request.ids, error=tr("操作已取消"))
            return True
        return False

    def _start_worker(self, request, run_io, *, must_finish=False, check_cancel=True):
        """建立并连接文件工作线程，按策略处理启动前取消和必须完成的任务。"""
        if check_cancel and self._cancel_before_mutation(request):
            return
        worker = _IOWorker(run_io, self)
        worker.must_finish = must_finish
        worker.cancelled_on_start = request.task._cancel_requested and not must_finish
        request.worker = worker
        worker.progress.connect(self.progress)
        worker.owner_call.connect(self._handle_owner_call)
        worker.finished.connect(lambda: self._settle(request, worker))
        worker.start()

    def _handle_owner_call(self, call):
        """在所有者线程持久化预设补丁或步骤，保存异常并唤醒等待中的工作线程。"""
        if call.done.is_set():
            return
        try:
            pm = self._m.preset_manager
            if call.operation == "preset_patch":
                pm._get_index().update_journal_runtime_patch(*call.args)
            elif call.operation == "preset_steps":
                pm.record_preset_apply_steps(*call.args)
            else:
                raise ValueError("Unknown owner-thread operation")
        except Exception as error:
            call.error = error
        finally:
            call.done.set()

    def _settle(self, request, worker=None):
        """接收已结束工作线程的结果，在所有者线程完成状态提交和任务通知。

        拒绝过期或重入结算，启用提交失败时尝试撤销新部署，最终安排线程对象释放。"""
        if self._active is not request or request.state.get("settling"):
            return
        if worker is not None and request.worker is not worker:
            return
        worker = request.worker
        if worker.isRunning():
            return
        if request.operation.startswith(("preset_", "variant_")):
            request.state["settling"] = True
            try:
                self._preset_variants.settle(request, worker)
            finally:
                request.state["settling"] = False
                worker.deleteLater()
            return
        try:
            if request.kind == "enable":
                from src.core.operation_preflight import OperationPreflightError

                if isinstance(worker.exception, OperationPreflightError):
                    self._m.report_operation_failure(worker.exception)
                receipt = getattr(worker, "source_receipt", None)
                if receipt is not None:
                    receipt.recheck()
                mod = self._m.finalize_enable(
                    request.ids[0], bool(worker.result), deploy_path=str(worker.result or ""), source_receipt=receipt
                )
                if mod is not None and mod.is_enabled:
                    self._complete(request, succeeded=request.ids)
                elif request.task._cancel_requested:
                    self._complete(request, cancelled=request.ids, error=tr("操作已取消"))
                else:
                    self._rollback_new_deployment(request)
                    self._complete(request, failed=request.ids, error=worker.error or tr("部署失败"))
            else:
                results = worker.result or []
                succeeded = self._m.commit_disable_batch(results)
                attempted = {r.mod_id for r in results}
                failed = [r.mod_id for r in results if r.mod_id not in succeeded]
                remaining = [mid for mid in request.ids if mid not in attempted]
                cancelled = remaining if request.task._cancel_requested else []
                if not cancelled:
                    failed.extend(remaining)
                error = worker.error or "; ".join(r.error for r in results if r.error)
                self._complete(request, succeeded=succeeded, failed=failed, cancelled=cancelled, error=error)
        except Exception as error:
            logger.exception("插件操作提交失败")
            if request.kind == "enable":
                self._rollback_new_deployment(request)
            from src.core.operation_preflight import OperationPreflightError

            if isinstance(error, OperationPreflightError):
                self._m.report_operation_failure(error)
            self._complete(request, failed=request.ids, error=str(error))
        finally:
            worker.deleteLater()

    def _rollback_new_deployment(self, request):
        """核对目标部署身份后移除本次新产物，清理失败时保留日志供审查。"""
        target = request.worker.result
        if not target:
            return
        try:
            self._m._storage._check_deployment_identity(target, request.ids[0])
            self._m._storage.remove_deploy_path(target)
        except (OSError, ValueError) as error:
            logger.error("撤销插件新部署失败，保留记录供后台审查: %s", error)

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        """取消队列并等待活动任务完成必要结算；达到时限或发生结算重入时返回 False。"""
        self._assert_thread()
        self._stopping = True
        for tasks in self._owned_tasks.values():
            for task in tasks:
                task._detach()
        self._owned_tasks.clear()
        for request in list(self._queue):
            request.task._detach()
            request.task.cancel()
        while self._queue:
            request = self._queue.popleft()
            self._complete(request, cancelled=request.ids, error=tr("宿主正在退出"))
        deadline = time.monotonic() + timeout_ms / 1000
        while self._active is not None:
            request = self._active
            request.task._detach()
            request.task.cancel()
            if request.state.get("settling"):
                # 同步发布回调仍处于当前结算栈中，应返回后再处理关闭请求。
                self._stopping = False
                return False
            if request.worker is not None:
                worker = request.worker
                if worker.pending_call is not None:
                    self._handle_owner_call(worker.pending_call)
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                if not worker.wait(min(20, remaining)):
                    # 关闭超时时窗口继续保留，当前请求保持锁定，结算后可接收新请求。
                    if time.monotonic() >= deadline:
                        self._stopping = False
                        return False
                    continue
                self._settle(request, worker)
            else:
                self._complete(request, cancelled=request.ids, error=tr("宿主正在退出"))
        return True
