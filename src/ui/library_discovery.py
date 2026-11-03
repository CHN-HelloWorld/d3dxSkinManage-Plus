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
from src.utils.runtime_metrics import MetricSamples


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


class LibraryDiscoveryController(QObject):
    """协调探测审计、分片快照捕获、后台发现和界面纳管规划，隔离过期游戏与来源代次。"""

    stopped = Signal()
    observed = Signal(object)
    failed = Signal(str, object)

    def __init__(self, manager, probe, *, busy, consume, parent=None):
        """绑定探测器与消费回调，建立捕获、规划、准备和重试定时器及审计信号连接。"""
        super().__init__(parent)
        self.manager, self.probe = manager, probe
        self.busy, self.consume = busy, consume
        self.worker = None
        self.pending = None
        self.active = None
        self._prepared = None
        self._capture_iterator = None
        self._probe_ready = False
        self._accepted_inventory = None
        self.closing = False
        self.planning = False
        self._source_generation = 0
        self.metrics = dict(
            started=0,
            completed=0,
            discarded=0,
            capture_ms=MetricSamples(),
            capture_slice_ms=MetricSamples(),
            worker_ms=MetricSamples(),
            planning_slice_ms=MetricSamples(),
        )
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._plan_slice)
        self._prepare_timer = QTimer(self)
        self._prepare_timer.setSingleShot(True)
        self._prepare_timer.timeout.connect(self._prepare)
        self._capture_timer = QTimer(self)
        self._capture_timer.setSingleShot(True)
        self._capture_timer.timeout.connect(self._capture_slice)
        self._error_retry = QTimer(self)
        self._error_retry.setSingleShot(True)
        self._error_retry.timeout.connect(self._schedule_probe)
        probe.settled.connect(self._after_probe)
        if hasattr(probe, "invalidated"):
            probe.invalidated.connect(self._invalidate)
        if hasattr(probe, "failed"):
            probe.failed.connect(self._probe_failed)

    def _probe_failed(self, error):
        """取消当前发现流程，并优先携带手动请求上下文报告探测失败。"""
        options = self.active or self.pending
        if self.pending and self.pending["manual"]:
            options = self.pending
        self.cancel(cancel_probe=False)
        if options is not None:
            self.failed.emit(error, options)

    def _invalidate(self):
        """撤销已接受的审计清单，并取消依赖全量审计的在途扫描。"""
        self._probe_ready = False
        self._accepted_inventory = None
        if self.worker is not None and self.active and self.active["changed_paths"] is None:
            self.worker.requestInterruption()

    def reset_scope(self):
        """增加来源范围代次并清除待处理请求，使旧范围的发现结果失效。"""
        self._source_generation += 1
        self.pending = None
        if self.worker is not None:
            self.worker.requestInterruption()

    def schedule(
        self,
        game_id,
        *,
        trigger,
        changed_paths=None,
        manual=False,
        discover=True,
        publish="on-change",
        external_copy_paths=(),
        **unused,
    ):
        """将触发来源转换为全量或局部发现选项，并合并手动审计与外部复制路径。"""
        if self.closing:
            return
        self._error_retry.stop()
        wants = discover and self.manager.trigger_discovers(trigger, changed_paths)
        new = dict(
            game_id=game_id,
            trigger=trigger,
            manual=manual,
            discover=wants,
            external_copy_paths=set(external_copy_paths),
            force_source_audit=manual or trigger == "manual",
            publish=publish,
            changed_paths=None if trigger != "watcher" else set(changed_paths or ()),
        )
        self._enqueue(new)

    def _enqueue(self, new, *, start=True):
        """合并同一游戏的待处理请求，保留更强审计和发布要求，并按需安排准备。"""
        game_id = new["game_id"]
        old = self.pending
        if old and old["game_id"] == game_id:
            new.setdefault("external_copy_paths", set()).update(old.get("external_copy_paths", ()))
            new["manual"] |= old["manual"]
            new["discover"] |= old["discover"]
            new["force_source_audit"] |= old["force_source_audit"]
            if old["publish"] == "always":
                new["publish"] = "always"
            if old["changed_paths"] is None or new["changed_paths"] is None:
                new["changed_paths"] = None
            else:
                new["changed_paths"].update(old["changed_paths"])
        self.pending = new
        if start:
            # 延后一个所有者事件周期采集，使同步到达的刷新和监控请求先合并范围。
            self._prepare_timer.start(0)

    def _schedule_probe(self):
        """局部发现直接安排准备，全量请求按所需强度启动库探测。"""
        options = self.pending or self.active
        if self.closing or options is None:
            return
        if options and options["changed_paths"] is not None:
            self._prepare_timer.start(0)
            return
        if options and options["force_source_audit"]:
            self.probe.schedule(force_source_audit=True)
        else:
            self.probe.schedule()

    def _prepare(self):
        """安排必要审计并尝试启动发现；准备异常时保留请求并延迟重试。"""
        try:
            if self.pending and self.pending["changed_paths"] is None:
                self._schedule_probe()
            self._start_discovery(probe_ready=False)
        except Exception:
            logger.exception("Could not prepare library discovery")
            self._retry(delay=1000)

    def _after_probe(self):
        """在审计完成后推进发现流程，异常时转为延迟重试。"""
        try:
            self._start_after_probe()
        except Exception:
            logger.exception("Could not prepare library discovery")
            self._retry(delay=1000)

    def _start_after_probe(self):
        # 较弱的后续轮询不撤销当前代次已接受的证据。
        """核验审计模式和强度后接受清单，恢复等待审计的完成或规划阶段。"""
        if self._probe_ready and self._current():
            return
        options = self.active or self.pending
        if options is not None:
            mode = self.probe.last_mode
            accepted_modes = {"full"}
            if options["changed_paths"] is not None:
                accepted_modes.add("source")
            strong = self.probe.last_force_source_audit
            if mode not in accepted_modes or (options["force_source_audit"] and not strong):
                # 事件探测可能先于完整审计结束，手动刷新仍要求完整检查，部署证据不能授权源导入。
                self._probe_ready = False
                return
        if self.active is not None:
            self._probe_ready = True
            self._accepted_inventory = getattr(self.probe, "source_inventory", None)
            if getattr(self, "_awaiting_audit", False):
                self._awaiting_audit = False
                self._finish()
                return
            if self._prepared is not None:
                self._begin_plan()
            return
        self._accepted_inventory = getattr(self.probe, "source_inventory", None)
        self._start_discovery(probe_ready=True)

    def _start_discovery(self, *, probe_ready):
        """仅为空闲且仍属当前游戏的请求建立发现状态，并分片捕获后台扫描所需快照。"""
        if (
            self.closing
            or self.active is not None
            or self.worker
            or self.planning
            or not self.pending
            or self._error_retry.isActive()
        ):
            return
        options = self.pending
        if options["game_id"] != self.manager.current_game_id:
            self.pending = None
            return
        if (
            options["discover"]
            and options["changed_paths"] is None
            and not options["force_source_audit"]
            and not probe_ready
        ):
            # 启动恢复更新身份位置后再采集发现输入，避免重复读取或使用过期路径重新审计。
            return
        self.pending, self.active = None, options
        self._probe_ready = probe_ready
        if not probe_ready:
            self._accepted_inventory = None
        self._prepared = None
        self._stamp = self.manager.library_probe_stamp
        self._generation = self.probe.generation
        self._discovery_generation = self._source_generation
        self.report = ReconcileReport(game_id=options["game_id"], trigger=options["trigger"])
        if not options["discover"]:
            self._finish()
            return
        try:
            self._capture_started = time.perf_counter()
            inventory = getattr(self.probe, "source_inventory", None)
            self._capture_iterator = iter_capture_discovery(
                self.manager,
                options["changed_paths"],
                source_inventory=inventory,
                verify_markers=options["force_source_audit"] and inventory is None,
                external_copy_paths=options.get("external_copy_paths", ()),
                audit_owns_known_checks=options["force_source_audit"] or probe_ready,
            )
            self._capture_slice()
        except Exception:
            logger.exception("Could not start library discovery")
            self._retry(delay=1000)

    def _cancel_capture(self):
        """停止快照时间片并关闭未完成的索引迭代器。"""
        self._capture_timer.stop()
        iterator, self._capture_iterator = self._capture_iterator, None
        if iterator is not None:
            iterator.close()

    def _capture_slice(self):
        """按八毫秒预算推进快照捕获，结束后启动扫描；请求过期或读取失败时重新调度。"""
        if self._capture_iterator is None:
            return
        start = time.perf_counter()
        try:
            if not self._current():
                self.metrics["discarded"] += 1
                self._retry()
                return
            while (time.perf_counter() - start) * 1000 < 8:
                next(self._capture_iterator)
            self._capture_timer.start(0)
        except StopIteration as completed:
            self._capture_iterator = None
            self.metrics["capture_ms"].append((time.perf_counter() - self._capture_started) * 1000)
            self._start_worker(completed.value)
        except Exception:
            logger.exception("Could not capture library discovery snapshot")
            self._retry(delay=1000)
        finally:
            self.metrics["capture_slice_ms"].append((time.perf_counter() - start) * 1000)

    def _start_worker(self, request):
        """启动持有快照的发现线程，并仅为仍有效的请求转发观察路径。"""
        self._discovery_request = request
        self.worker = DiscoveryWorker(request, self)
        self.worker.finished.connect(self._finished)
        self.worker.observed.connect(lambda paths: self.observed.emit(paths) if self._current() else None)
        self.metrics["started"] += 1
        self.worker.start()

    def _current(self):
        """核验关闭状态、游戏、来源代次及全量请求的库与探测版本是否仍匹配。"""
        try:
            return (
                not self.closing
                and self.active is not None
                and self.active["game_id"] == self.manager.current_game_id
                and (getattr(self, "_discovery_generation", 0) == getattr(self, "_source_generation", 0))
                and (
                    self.active["changed_paths"] is not None
                    or (self._stamp == self.manager.library_probe_stamp and self._generation == self.probe.generation)
                )
            )
        except Exception:
            return False

    def _valid(self):
        """返回当前规划是否仍满足发现请求的范围和版本约束。"""
        return self._current()

    def _retry(self, *, delay=0):
        """撤销当前准备状态并重新合并请求，优先接续已有完整审计，必要时延迟重试。"""
        self._cancel_capture()
        active, self.active = self.active, None
        self.planning = False
        self._prepared = None
        self._probe_ready = False
        if active and active["game_id"] == self.manager.current_game_id and not self.closing:
            # 并发导入可能使发现结果失效，按当前已接受证据重新采集路径；显式请求仍单独调度。
            join = (
                not delay
                and self.pending is None
                and active["changed_paths"] is None
                and getattr(self.probe, "current_full_audit_state", lambda *a, **kw: None)(
                    active["game_id"], force_source_audit=active["force_source_audit"]
                )
            )
            if join:
                self._prepare_timer.stop()
                self._enqueue(active, start=False)
                if join == "settled":
                    self._start_after_probe()
                return
            self._enqueue(active, start=not delay)
        elif self.pending and not self.closing and not delay:
            self._schedule_probe()
        if delay and self.pending and not self.closing:
            self._error_retry.start(delay)

    def _finished(self):
        """回收发现线程并记录耗时；错误或过期结果重新排队，有效结果交给规划阶段。"""
        worker, self.worker = self.worker, None
        self.metrics["completed"] += 1
        self.metrics["worker_ms"].append(worker.elapsed_ms)
        worker.deleteLater()
        if self.closing:
            self.stopped.emit()
            return
        if not self._current() or worker.error:
            self.metrics["discarded"] += 1
            failed = worker.error and not isinstance(worker.error, InterruptedError)
            if failed:
                logger.warning("Library discovery preserved current state: %s", worker.error)
            self._retry(delay=1000 if failed else 0)
            return
        self._prepared = worker.result
        self._begin_plan()

    def _begin_plan(self):
        """等待所需审计并核验清单一致性，将稳定候选交给分片规划，变化候选重新发现。"""
        if (
            self.active
            and not self._probe_ready
            and (self.active["force_source_audit"] or self.active["changed_paths"] is None)
        ):
            # 启动时可能发现离线移动的已登记容器，先恢复身份归属再导入。
            return
        if not self._current():
            self.metrics["discarded"] += 1
            self._retry()
            return
        if self._probe_ready and self.active["force_source_audit"] and not self._discovery_request.verify_markers:
            before, accepted = self._discovery_request.source_inventory, self._accepted_inventory
            matches = (
                before is accepted
                or (before is None and accepted is not None and not accepted.entries)
                or (
                    before is not None
                    and accepted is not None
                    and before.root == accepted.root
                    and before.entries == accepted.entries
                    and before.signature == accepted.signature
                )
            )
            if not matches:
                # 完整审计负责身份校验，发现语义变化时按该冻结结果重新准备，不重复全量扫描。
                self.metrics["discarded"] += 1
                self._prepared = None
                self._start_worker(
                    replace(
                        self._discovery_request,
                        source_inventory=accepted,
                        verify_markers=accepted is None,
                        audit_owns_known_checks=(
                            accepted is not None and accepted.root == path_key(self._discovery_request.root)
                        ),
                    )
                )
                return
        descriptors, self.report.invalid_containers = self._prepared
        deferred = {item["path"] for item in descriptors if item.get("_deferred")}
        if deferred:
            self.schedule(self.active["game_id"], trigger="watcher", changed_paths=deferred)
        descriptors = [item for item in descriptors if not item.get("_deferred")]
        self._prepared = None
        self._descriptors = iter(descriptors)
        self._planning_snapshot = None
        self.planning = True
        self._plan_slice()

    def _plan_slice(self):
        """在界面时间片内将候选来源转换为纳管请求，受保护路径延期，已有容器按身份变更处理。"""
        if not self._valid():
            self._retry()
            return
        from src.core.mod_intake import IntakeTargetMissingError, ModIntakeService

        service = ModIntakeService(self.manager)
        start = time.perf_counter()
        try:
            while True:
                if not self.planning:
                    return
                item = next(self._descriptors)
                source = Path(item["path"])
                if self.manager.is_library_path_protected(source):
                    self.schedule(self.active["game_id"], trigger="watcher", changed_paths={str(source)})
                    if (time.perf_counter() - start) * 1000 >= 8:
                        self._timer.start(0)
                        return
                    continue
                if self.manager._index.paths.locate(str(source)) is not None:
                    owner = self.manager._index.paths.locate(str(source))
                    if owner[0] == "mod":
                        mod = self.manager.get_mod(owner[1])
                        if mod is not None and Path(mod.path) != source:
                            from src.core.watcher import FileChange

                            service = getattr(self.manager, "_local_source_events", None)
                            delta = self.manager.apply_local_source_events(
                                [
                                    FileChange(
                                        str(self.manager._index.paths.roots[mod.game_id]),
                                        service.generation if service is not None else 0,
                                        "modified",
                                        str(source.parent / ".d3dxmod"),
                                    )
                                ]
                            )
                            self.report.claimed += int(mod.mod_id in delta.changed_ids)
                        continue  # 其他导入任务已经发布此输入。
                managed = self.manager._is_body_in_container(source)
                external_copy = str(source) in self.active.get("external_copy_paths", ())
                try:
                    self.report.intake_requests.append(
                        service.plan_request(
                            source_path=source,
                            game_id=self.active["game_id"],
                            node_id=item.get("node_id", ""),
                            # 扫描文件名只是默认值，不覆盖分享包或受管 sidecar 的正式名称。
                            move_source=external_copy or not managed,
                            context="rediscovery" if managed and not external_copy else "external",
                            external_copy=external_copy,
                            source_version=item.get("source_version"),
                        )
                    )
                except IntakeTargetMissingError:
                    pass
                if not self.planning:
                    return
                if (time.perf_counter() - start) * 1000 >= 8:
                    self._timer.start(0)
                    return
        except StopIteration:
            self._finish()
        except Exception:
            logger.exception("Could not plan discovered library inputs")
            self._retry(delay=1000)
        finally:
            self.metrics["planning_slice_ms"].append((time.perf_counter() - start) * 1000)

    def _finish(self):
        """等候强审计完成后交付发现报告与请求选项，再推进下一项待处理请求。"""
        if self.active["force_source_audit"] and not self._probe_ready:
            self._awaiting_audit = True
            self.planning = False
            return
        options, self.active = self.active, None
        self.planning = False
        self.consume(self.report, options)
        if self.pending:
            self._schedule_probe()

    def cancel(self, *, cancel_probe=True):
        """丢弃当前刷新尚未提交的暂存项，允许新的刷新开始。"""
        self._source_generation += 1
        self._cancel_capture()
        self.pending = self.active = None
        self._prepared = None
        self._probe_ready = False
        self._accepted_inventory = None
        self._awaiting_audit = False
        self.planning = False
        self._timer.stop()
        self._prepare_timer.stop()
        self._error_retry.stop()
        if self.worker:
            self.worker.requestInterruption()
        if cancel_probe:
            self.probe.cancel()

    def stop(self):
        """关闭发现控制器并取消当前流程，同时保留独立探测器的生命周期。"""
        self.closing = True
        self.cancel(cancel_probe=False)

    def resume(self):
        """解除关闭状态，并为当前游戏重新安排一次手动强审计。"""
        self.closing = False
        if self.manager.current_game_id:
            self.schedule(self.manager.current_game_id, trigger="manual")
