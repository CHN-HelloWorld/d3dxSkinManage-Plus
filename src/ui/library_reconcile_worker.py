"""串行执行文件探测，并按时间预算在所有者线程应用。"""

from __future__ import annotations

import math
import time
from dataclasses import replace

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from src.core.library_snapshot import ProbeResult, ProbeToken, probe_library
from src.ui.library_work_budget import LibraryWorkBudget
from src.utils.logger import logger
from src.utils.runtime_metrics import MetricSamples


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
            snapshot_ms=MetricSamples(),
            snapshot_slice_ms=MetricSamples(),
            apply_slice_ms=MetricSamples(),
            worker_ms=MetricSamples(),
            slow_items=MetricSamples(),
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
            self.metrics["snapshot_slice_ms"].append((time.perf_counter() - start) * 1000)

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

    def _apply_slice(self):
        """在界面时间预算内应用探测步骤并收集增量事件，代次变化或异常时保存未发布状态并重试。"""
        if self._iterator is None:
            return
        start = time.perf_counter()
        self._inside_apply = True
        try:
            if not self._valid(self._result):
                self._discard_application()
                return
            while True:
                generation = self.generation
                item_start = time.perf_counter()
                self.manager._library_probe_events = self._events
                try:
                    step = next(self._iterator)
                finally:
                    self.manager._library_probe_events = None
                elapsed = (time.perf_counter() - item_start) * 1000
                if elapsed > 8:
                    self.metrics["slow_items"].append(dict(phase=step.phase, id=step.item_id, ms=elapsed))
                self._changed |= step.changed
                self._removed += step.removed
                if step.changed and step.item_id:
                    self._changed_ids.add(step.item_id)
                if step.removed and step.item_id:
                    self._removed_ids.add(step.item_id)
                # 原子修改可推进数据库和物理计数，界面失效不能冒充该操作的变更。
                if generation != self.generation or self.closing:
                    self._discard_application()
                    return
                self._stamp = self.manager.library_probe_stamp
                if (time.perf_counter() - start) * 1000 >= self.APPLY_BUDGET_MS:
                    self._continuation.start(0)
                    return
        except StopIteration:
            self._finish_application()
        except Exception:
            logger.exception("Library result application stopped; preserving remaining rows")
            self._discard_application()
        finally:
            self.metrics["apply_slice_ms"].append((time.perf_counter() - start) * 1000)
            self._inside_apply = False
            if self.closing:
                self.stop()

    def _discard_application(self):
        """终止当前结果迭代，保留已应用但未发布的事件，并按请求有效性重新排入完整审计。"""
        self._invalidate_operation_trust()
        self._source_recovery_claim = None
        self.metrics["discarded"] += 1
        retry = (
            not self.closing
            and not self.paused
            and self._result is not None
            and self._result.token.request_id > self._cancelled_request_id
        )
        if retry:
            self.pending = True
            self._full_pending = True
            self._force_source_audit_pending |= self._result.request.force_source_audit
            self._source_paths.update(self._result.request.changed_paths)
            self._source_events.extend(self._result.request.events)
        if self._result is not None and (self._changed or self._events):
            # 已提交项继续有效，接受新扫描后才发布，避免过期完成事件触发刷新。
            self._unpublished[self._result.token.game_id] = (self._changed, self._removed, self._events)
        iterator, self._iterator = self._iterator, None
        if iterator is not None:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()
        self._result = None
        self._events = []
        self.applying = False
        if self.closing:
            if self.worker is None and not self._inside_apply:
                self.stopped.emit()
        elif retry:
            self._retry.start(1000)

    def _finish_application(self):
        """完成来源恢复与增量发布，逐项核验通知有效性，并按审计结果更新操作信任和完成标记。"""
        result = self._result
        self._accept_source_recovery(result)
        self.last_mode = result.request.mode
        self.last_force_source_audit = result.request.force_source_audit
        game_id = result.token.game_id
        changed, removed, events = self._changed, self._removed, self._events
        if result.source_inventory is not None:
            self.source_inventory = result.source_inventory
        self.changed_ids = None if self._incomplete_delta else tuple(self._changed_ids)
        self.removed_ids = None if self._incomplete_delta else tuple(self._removed_ids)
        self._iterator = None
        self._result = None
        self._events = []
        # 回调期间继续保持忙状态，插件可能进入嵌套事件循环。
        try:
            if changed:
                self.changed.emit(game_id, removed)
            for offset, (event, payload) in enumerate(events):
                if not self._valid(result):
                    self._unpublished[game_id] = (False, 0, events[offset:])
                    if result.token.request_id > self._cancelled_request_id:
                        self.pending = True
                    break
                self.manager._emit(event, **payload)
        finally:
            self.applying = False
            self.changed_ids = self.removed_ids = None
        if not self.closing and result.token.request_id > self._cancelled_request_id:
            if self._can_accept_operation_trust(result):
                self.manager.accept_operation_trust(game_id)
            else:
                self._invalidate_operation_trust()
            # 只保留已接受的令牌和证据强度，不额外保存完整记录快照。
            self._settled_full_audit = (
                (result.token, result.request.force_source_audit)
                if result.request.mode == "full" and self._valid(result)
                else None
            )
            self.source_observed.emit((game_id, ()))
            self.settled.emit()
            if self.pending:
                self._retry.start(1000 if game_id in self._unpublished else 0)

    def _can_accept_operation_trust(self, result):
        """工作线程成功不代表已变化或不完整的文件库可信。"""
        from src.core.library_snapshot import path_key

        request, inventory = result.request, result.source_inventory
        if (
            result.error
            or result.cancelled
            or request.mode != "full"
            or not self._valid(result)
            or inventory is None
            or self._operation_review_claim != self._operation_review_revision
            or self.pending
            or self._source_paths
            or self._source_events
            or self._deployment_pending
            or self._full_pending
            or self._unpublished
            or self.manager.has_pending_source_cleanup()
            or path_key(inventory.root) != path_key(request.library_root)
            or path_key(str(self.manager._index.paths.roots.get(result.token.game_id, "")))
            != path_key(request.library_root)
            or any(entry.error for entry in inventory.entries)
            or len(result.checks) != len(request.rows)
        ):
            return False
        for row, check in zip(request.rows, result.checks):
            if check.path != row.path or check.state not in {"exists", "missing"}:
                return False
            if check.state == "missing":
                current = self.manager.get_mod(row.mod_id)
                # 已删除记录和成功认领的移动已经完成，原路径重新出现的源仍需检查。
                if current is not None and (current.path == row.path or row.mod_id not in self._changed_ids):
                    return False
        service = getattr(self.manager, "_local_source_events", None)
        if service is not None and (getattr(service, "_tainted", False) or getattr(service, "_missing", None)):
            return False
        if getattr(self.manager, "_deferred_source_events", None) or getattr(
            self.manager, "_deferred_deployment_events", None
        ):
            return False
        host = self.parent()
        if host is not None:
            if any(
                getattr(host, name, None) for name in ("_watcher_events", "_deployment_events", "_watcher_restarts")
            ):
                return False
            for name in ("_file_watcher", "_deploy_watcher"):
                watcher = getattr(host, name, None)
                needed = name == "_file_watcher" or bool(request.deploy_roots)
                if watcher is not None and needed and not watcher.is_running():
                    return False
        return True

    def cancel(self):
        """取消当前轮次，不关闭控制器或撤销已提交项。"""
        self._cancelled_request_id = self.request_id
        self.generation += 1
        self.pending = False
        self._source_paths.clear()
        self._source_events.clear()
        self._deployment_pending = self._full_pending = False
        self._force_source_audit_pending = False
        self._settled_full_audit = None
        self._source_recovery_claim = None
        self._retry.stop()
        self._continuation.stop()
        self._snapshot_timer.stop()
        iterator, self._snapshot_iterator = self._snapshot_iterator, None
        if iterator is not None:
            iterator.close()
        self._snapshot_ready = None
        self.invalidated.emit()
        if self.worker is not None:
            self.worker.requestInterruption()
        # 重入取消时先完成当前原子项，再关闭生成器并丢弃剩余工作。
        if self.applying and not self._inside_apply:
            self._discard_application()

    def pause(self, error=""):
        """失败后暂停读写，等待宿主修复或重新选择根目录。"""
        self.paused = True
        self.last_error = str(error)
        self.cancel()
        if self.last_error:
            self.failed.emit(self.last_error)

    def reset_failure(self):
        """根目录变更或显式恢复成功后允许新工作。"""
        self.paused = False
        self.last_error = ""

    def stop(self):
        """请求停止但不等待文件操作，保留 Qt 所有权。"""
        self._invalidate_operation_trust()
        self.closing = True
        self.generation += 1
        self.pending = False
        self._source_paths.clear()
        self._source_events.clear()
        self._deployment_pending = self._full_pending = False
        self._force_source_audit_pending = False
        self.source_inventory = None
        self._retry.stop()
        self._continuation.stop()
        self._snapshot_timer.stop()
        if self._snapshot_iterator is not None:
            self._snapshot_iterator.close()
            self._snapshot_iterator = None
        self._snapshot_ready = None
        # 重入关闭请求须保留窗口，直到原子发布返回外层槽函数。
        if self._inside_apply:
            return
        self._iterator = None
        self._result = None
        self._events = []
        self.applying = False
        if self.worker is not None:
            self.worker.requestInterruption()
        else:
            self.stopped.emit()

    def resume(self):
        """解除关闭和失败暂停状态，并安排一次完整库探测。"""
        self.closing = False
        self.reset_failure()
        self.schedule()
