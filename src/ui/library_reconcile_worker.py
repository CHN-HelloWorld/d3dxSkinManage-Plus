"""串行执行文件探测，并按时间预算在所有者线程应用。"""

from __future__ import annotations

import math
import time
from dataclasses import replace

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from src.core.library_snapshot import ProbeResult, ProbeToken, probe_library
from src.ui.library_work_budget import LibraryWorkBudget
from src.utils.logger import logger


class _LocalEventWindow:
    """下一批审计前先让前景提交和绘制获得执行机会。"""

    def __init__(self):
        """初始化局部文件事件的让行截止时间，初始状态不延迟扫描。"""
        self.until = 0.0

    def defer(self):
        """把后台审计让行窗口延长至当前时刻之后八十毫秒。"""
        self.until = time.monotonic() + 0.080

    def remaining_ms(self):
        """返回让行窗口剩余毫秒数，向上取整且不小于零。"""
        return max(0, math.ceil((self.until - time.monotonic()) * 1000))

    def checkpoint(self, cancelled):
        """在局部事件让行窗口内短暂等待并反复检查取消，返回是否应终止扫描。"""
        while not cancelled():
            remaining = self.until - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(remaining, 0.010))
        return True


class LibraryReconcileWorker(QThread):
    """请求仅包含值，线程结束后消费结果。"""

    source_observed = Signal(object)

    def __init__(self, request, parent=None):
        """保存只读探测请求，初始化结果、耗时和独立后台工作预算。"""
        super().__init__(parent)
        self.request = request
        self.result = None
        self.elapsed_ms = 0.0
        self.local_event_window = None
        self._work_budget = LibraryWorkBudget()

    def _checkpoint(self):
        """优先为局部事件处理让行，再按线程预算检查取消并释放执行机会。"""
        window = self.local_event_window
        if window.checkpoint(self.isInterruptionRequested) if window is not None else self.isInterruptionRequested():
            return True
        return self._work_budget.checkpoint(self.isInterruptionRequested)

    def run(self):
        """先报告局部来源观察，再执行库探测；异常转为 ProbeResult 错误并记录后台耗时。"""
        start = time.perf_counter()
        try:
            if self.request.mode == "source" or (self.request.mode == "full" and self.request.changed_paths):
                from src.core.library_inventory import observe_source_paths

                observations = observe_source_paths(self.request, self._checkpoint)
                if observations and not self.isInterruptionRequested():
                    self.source_observed.emit((self.request, observations))
            self.result = probe_library(self.request, should_cancel=self._checkpoint)
        except Exception as error:
            self.result = ProbeResult(self.request, error=f"{type(error).__name__}: {error}")
        finally:
            self.elapsed_ms = (time.perf_counter() - start) * 1000


class LibraryReconcileController(QObject):
    """串行协调库快照、后台探测与分片发布，维护审计可信状态和待重试范围。"""

    APPLY_BUDGET_MS = 8
    SNAPSHOT_BUDGET_MS = 8
    changed = Signal(str, int)
    settled = Signal()
    stopped = Signal()
    source_observed = Signal(object)
    invalidated = Signal()
    failed = Signal(str)

    def __init__(self, manager, *, busy, parent=None):
        """绑定管理器与忙碌判断，初始化探测代次、局部事件队列、发布增量和分片定时器。"""
        super().__init__(parent)
        self.manager = manager
        self._operation_review_revision = 0
        self._operation_review_claim = None
        self.invalidated.connect(self._invalidate_operation_trust)
        self._invalidate_operation_trust()
        self._host_busy = busy
        self.worker = None
        self.pending = False
        self.applying = False
        self._inside_apply = False
        self.closing = False
        self.paused = False
        self.last_error = ""
        self._cancelled_request_id = -1
        self.generation = 0
        self.request_id = 0
        self._stamp = None
        self._result = None
        self._iterator = None
        self._changed = False
        self._removed = 0
        self._events = []
        self._source_recovery_claim = None
        self._local_event_window = _LocalEventWindow()
        self._snapshot_iterator = None
        self._snapshot_ready = None
        self._snapshot_token = None
        self._snapshot_started = 0.0
        self._unpublished = {}
        self.source_inventory = None
        self.last_mode = ""
        self.last_force_source_audit = False
        self._settled_full_audit = None
        self._source_paths = set()
        self._source_events = []
        self._deployment_pending = False
        self._full_pending = False
        self._force_source_audit_pending = False
        self.changed_ids = None
        self.removed_ids = None
        self._changed_ids = set()
        self._removed_ids = set()
        self.metrics = dict(
            started=0,
            completed=0,
            discarded=0,
            max_active=0,
            snapshot_ms=[],
            apply_slice_ms=[],
            worker_ms=[],
            slow_items=[],
        )
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._start_pending)
        self._continuation = QTimer(self)
        self._continuation.setSingleShot(True)
        self._continuation.timeout.connect(self._apply_slice)
        self._snapshot_timer = QTimer(self)
        self._snapshot_timer.setSingleShot(True)
        self._snapshot_timer.timeout.connect(self._snapshot_slice)

    def invalidate(self):
        """使当前探测代次与操作信任失效，空闲可用时安排完整探测并取消旧扫描。"""
        self.generation += 1
        self.invalidated.emit()
        if self.paused or self.closing:
            return
        self.pending = True
        self._full_pending = True
        if self.worker is not None:
            self.worker.requestInterruption()

    def _invalidate_operation_trust(self):
        """增加操作审查版本，并撤销管理器对当前库状态的信任。"""
        self._operation_review_revision += 1
        self.manager.invalidate_operation_trust()

    def schedule_changes(self, paths, *, deployment=False, events=(), force_source_audit=False):
        """事件优先于完整审计，但校验证据仍覆盖完整身份空间。"""
        if self.closing or self.paused:
            return
        if force_source_audit:
            self._full_pending = True
            self._force_source_audit_pending = True
        if deployment:
            self._deployment_pending = True
        else:
            self._source_paths.update(str(path) for path in paths if path)
            self._source_events.extend(events)
        self.generation += 1
        self.invalidated.emit()
        self.pending = True
        if self.worker is not None:
            self.worker.requestInterruption()
        self._start_pending()

    def _sync_stamp(self):
        """将管理器库版本同步到控制器，版本改变时增加代次并通知审计失效。"""
        stamp = self.manager.library_probe_stamp
        if stamp != self._stamp:
            if self._stamp is not None and stamp[0] != self._stamp[0]:
                self.source_observed.emit((stamp[0], ()))
            self._stamp = stamp
            self.generation += 1
            self.invalidated.emit()

    def _busy(self):
        """返回宿主操作或管理器库事务是否阻止开始及发布探测。"""
        return self._host_busy() or self.manager.library_probe_busy()

    def yield_to_local_events(self):
        """暂停后台工作，不取消或重启完整审计。"""
        self._invalidate_operation_trust()
        self._local_event_window.defer()

    def current_full_audit_state(self, game_id, *, force_source_audit=False):
        """允许发现重试复用证据，不替代新的手动刷新。"""
        worker = self.worker
        if worker is not None and worker.isInterruptionRequested():
            return None
        request = worker.request if worker is not None else self._result.request if self._result is not None else None
        if request is not None:
            if (
                request.token.game_id == game_id
                and request.mode == "full"
                and (not force_source_audit or request.force_source_audit)
                and self._valid_token(request.token)
            ):
                return "running"
            return None
        if self.pending or self.applying or self._snapshot_iterator is not None:
            return None
        if self._settled_full_audit is not None:
            token, strong = self._settled_full_audit
            if token.game_id == game_id and (not force_source_audit or strong) and self._valid_token(token):
                return "settled"
        return None

    def schedule(self, *, force_source_audit=False, periodic=False):
        """合并完整探测请求；强来源审计可替换较弱的在途请求，暂停时保留失败提示。"""
        if self.closing:
            return
        if self.paused:
            if force_source_audit and self.last_error:
                self.failed.emit(self.last_error)
            return
        if periodic and not force_source_audit and (self.worker is not None or self.applying):
            # 定时器只要求最终检查，活动任务已能满足；过期结果自行重试。
            return
        self.pending = True
        self._full_pending = True
        self._force_source_audit_pending |= force_source_audit
        active = (
            self.worker.request
            if self.worker is not None
            else self._result.request
            if self._result is not None
            else None
        )
        if force_source_audit and active is not None and active.mode != "deployment" and not active.force_source_audit:
            # 用户请求新的完整身份审计后，较弱的在途结果不能先执行清理。
            self.generation += 1
            self.invalidated.emit()
            if self.worker is not None:
                self.worker.requestInterruption()
        self._start_pending()

    def _start_pending(self):
        """等待局部事件及事务空闲，分片捕获快照后按来源、部署或全量模式启动唯一后台探测。"""
        if (
            self.closing
            or self.paused
            or not self.pending
            or self.worker is not None
            or self.applying
            or self._snapshot_iterator is not None
        ):
            return
        remaining = self._local_event_window.remaining_ms()
        if remaining:
            self._retry.start(remaining)
            return
        try:
            if self._busy() or not self.manager.current_game_id:
                self._retry.start(1000)
                return
            self._sync_stamp()
            request, self._snapshot_ready = self._snapshot_ready, None
            if request is None:
                self.request_id += 1
                token = ProbeToken(self.manager.current_game_id, self.generation, self.request_id)
                self._snapshot_token = token
                self._snapshot_started = time.perf_counter()
                self._snapshot_iterator = self.manager.iter_library_probe_request(token)
                self._snapshot_slice()
                return
            if request.token != ProbeToken(self.manager.current_game_id, self.generation, self.request_id):
                self._retry.start(0)
                return
            start = self._snapshot_started
            if self._source_paths:
                # 完整审计已覆盖资源事件，保留路径用于提前观察与标记重读，不先重复扫描身份。
                mode = "full" if self._full_pending and self._force_source_audit_pending else "source"
                paths = tuple(self._source_paths)
                self._source_paths.clear()
                events, self._source_events = tuple(self._source_events), []
                if mode == "full":
                    self._full_pending = False
            elif self._deployment_pending:
                mode, paths = "deployment", ()
                events = ()
                self._deployment_pending = False
            else:
                mode, paths = "full", ()
                events = ()
                self._full_pending = False
            force_audit = self._force_source_audit_pending and mode != "deployment"
            request = replace(
                request,
                mode=mode,
                changed_paths=paths,
                source_inventory=self.source_inventory,
                events=events,
                force_source_audit=force_audit,
            )
            self._source_recovery_claim = self._capture_source_recovery(request)
            self._operation_review_claim = self._operation_review_revision
            self.metrics["snapshot_ms"].append((time.perf_counter() - start) * 1000)
            self.worker = LibraryReconcileWorker(request, self)
            self.worker.local_event_window = self._local_event_window
            self.worker.finished.connect(self._worker_finished)
            self.worker.source_observed.connect(self._source_observed)
            if mode == "full":
                self._force_source_audit_pending = False
            self.pending = bool(self._source_paths or self._deployment_pending or self._full_pending)
            self.metrics["started"] += 1
            self.metrics["max_active"] = max(
                self.metrics["max_active"], self.metrics["started"] - self.metrics["completed"]
            )
            self.worker.start(QThread.Priority.LowPriority)
        except Exception as error:
            logger.exception("Could not start library probe")
            self.pause(f"{type(error).__name__}: {error}")

    def _snapshot_slice(self):
        """在所有者线程分批构造不可变审计输入，拒绝混合代次。"""
        if self.closing or self._snapshot_iterator is None:
            return
        start = time.perf_counter()
        try:
            self._sync_stamp()
            if self._busy() or self._snapshot_token != ProbeToken(
                self.manager.current_game_id, self.generation, self.request_id
            ):
                self._snapshot_iterator.close()
                self._snapshot_iterator = None
                self._retry.start(0)
                return
            remaining = self._local_event_window.remaining_ms()
            if remaining:
                self._snapshot_timer.start(remaining)
                return
            while (time.perf_counter() - start) * 1000 < self.SNAPSHOT_BUDGET_MS:
                next(self._snapshot_iterator)
            self._snapshot_timer.start(0)
        except StopIteration as completed:
            self._snapshot_iterator = None
            self._snapshot_ready = completed.value
            self._start_pending()
        except Exception as error:
            logger.exception("Could not capture library probe snapshot")
            self.pause(f"{type(error).__name__}: {error}")
        finally:
            self.metrics.setdefault("snapshot_slice_ms", []).append((time.perf_counter() - start) * 1000)

    def _valid(self, result):
        """检查探测结果令牌是否仍属于当前可发布请求。"""
        return self._valid_token(result.token)

    def _valid_token(self, token):
        """核验取消边界、库版本、游戏和请求代次，忙碌或校验异常时拒绝发布。"""
        if self.closing or self.paused or token.request_id <= self._cancelled_request_id:
            return False
        try:
            self._sync_stamp()
            return not self._busy() and token == ProbeToken(
                self.manager.current_game_id, self.generation, self.request_id
            )
        except Exception:
            return False

    def _capture_source_recovery(self, request):
        """仅为匹配监控根的完整强审计保存恢复令牌，以便成功发布后恢复来源事件服务。"""
        from src.core.library_schema import directory_key

        service = getattr(self.manager, "_local_source_events", None)
        if (
            service is None
            or request.mode != "full"
            or not request.force_source_audit
            or directory_key(request.library_root) != directory_key(str(getattr(service, "_watched_root", "")))
        ):
            return None
        return request.token, request.library_root, service, service.recovery_token()

    def _accept_source_recovery(self, result):
        """仅当前且已完成的完整审计可以清除健康状态疑虑。"""
        from src.core.library_schema import directory_key

        claim, self._source_recovery_claim = self._source_recovery_claim, None
        if claim is None or result.error or result.cancelled or not self._valid(result):
            return False
        token, root, service, health_token = claim
        inventory = result.source_inventory
        if (
            result.token != token
            or result.request.mode != "full"
            or not result.request.force_source_audit
            or inventory is None
            or directory_key(result.request.library_root) != directory_key(root)
            or directory_key(inventory.root) != directory_key(root)
            or any(entry.error for entry in inventory.entries)
            or getattr(self.manager, "_local_source_events", None) is not service
        ):
            return False
        return service.accept_recovery(health_token, scope_complete=True)

    def _source_observed(self, payload):
        """核验后台观察所属请求后，向界面转发游戏与来源观察结果。"""
        request, observations = payload
        if self._valid(ProbeResult(request)):
            self.source_observed.emit((request.token.game_id, observations))

    def _worker_finished(self):
        """回收探测线程，丢弃并重排过期结果，错误时暂停，有效结果进入分片应用。"""
        worker = self.worker
        result = worker.result
        self.metrics["worker_ms"].append(worker.elapsed_ms)
        self.metrics["completed"] += 1
        self.worker = None
        worker.deleteLater()
        if self.closing:
            self.stopped.emit()
            return
        if worker.request.token.request_id <= self._cancelled_request_id:
            # 旧任务取消后到达的结果不能重试或污染新刷新请求。
            self.metrics["discarded"] += 1
            self._start_pending()
            return
        try:
            valid = result is not None and self._valid(result)
        except Exception:
            valid = False
        if not valid or result.error or result.cancelled:
            self._invalidate_operation_trust()
            self.metrics["discarded"] += 1
            if valid and result.error:
                logger.warning("Library probe preserved current state: %s", result.error)
                self.pause(result.error)
                return
            self.pending = True
            if result is not None:
                self._force_source_audit_pending |= result.request.force_source_audit
                self._source_paths.update(result.request.changed_paths)
                self._source_events.extend(result.request.events)
                if result.request.mode == "deployment":
                    self._deployment_pending = True
                elif result.request.mode != "source":
                    self._full_pending = True
            # 事件密集时证据过期属于正常情况，取消后及时重启；读写错误仍按退避规则处理。
            self._retry.start(1000 if result is None or result.error else 0)
            return
        self._result = result
        self._iterator = iter(self.manager.apply_library_probe_result(result))
        self._changed, self._removed, self._events = self._unpublished.pop(result.token.game_id, (False, 0, []))
        self._incomplete_delta = self._changed
        self._changed_ids = set()
        self._removed_ids = set()
        self.applying = True
        self._apply_slice()
