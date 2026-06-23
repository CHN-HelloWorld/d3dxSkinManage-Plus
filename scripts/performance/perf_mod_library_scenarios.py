"""在隔离环境中测量 MainWindow 操作场景。

性能采样须使用桌面 Qt 平台。--offscreen-smoke 仅检查调用链，最多创建 30 个 Mod。
脚本通过任务屏障复现过期结果和关闭竞争；暂停任务时的心跳仅反映响应性，
不代表磁盘吞吐量。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import tempfile
import threading
import time
import traceback
from contextlib import ExitStack, contextmanager
from functools import wraps
from pathlib import Path
from unittest.mock import patch

import perf_mod_library as bench

SECOND_GAME = "scenario_second"


class ScenarioRunner:
    """在隔离主窗口中执行可重复操作，采集心跳、阶段耗时和异常，并管理任务屏障及清理。"""

    def __init__(self, manager, root, result, save, *, offscreen_smoke=False):
        """核验 Qt 平台并安装异常、消息和心跳采集，初始化阶段测量及屏障释放状态。"""
        from PySide6.QtCore import Qt, QTimer, qInstallMessageHandler
        from PySide6.QtWidgets import QApplication, QMessageBox

        self.manager, self.root, self.result, self.save = manager, root, result, save
        self.app = QApplication.instance() or QApplication([])
        self.app.setQuitOnLastWindowClosed(False)
        platform = self.app.platformName().lower()
        if platform in ("offscreen", "minimal") and not offscreen_smoke:
            raise RuntimeError("formal scenarios require a real desktop Qt platform")
        result["qt_platform"] = platform
        result["formal_gui"] = not offscreen_smoke and platform not in ("offscreen", "minimal")
        result["qt_callback_errors"] = self.errors = []
        result["qt_messages"] = self.messages = []
        self.releases = []
        self.samples = []
        self.phase_events = []
        self.phase_stack = ExitStack()
        self.window = None
        self.closed = False
        self.previous = time.perf_counter()
        self.old_hook = sys.excepthook

        def exception_hook(kind, value, tb):
            """保存逃逸的 Python 回调异常，供等待和场景验收判定失败。"""
            self.errors.append("".join(traceback.format_exception(kind, value, tb)))

        def qt_message(kind, context, message):
            """限量记录 Qt 消息，并将运行中线程遭到销毁的警告登记为场景错误。"""
            if len(self.messages) < 200:
                self.messages.append({"kind": str(kind), "message": message})
            if "QThread: Destroyed while thread" in message:
                self.errors.append(message)

        sys.excepthook = exception_hook
        self.old_message_handler = qInstallMessageHandler(qt_message)
        self.qt_message_handler = qt_message  # 保持 Python 回调存活。
        self.heartbeat = QTimer()
        self.heartbeat.setTimerType(Qt.TimerType.PreciseTimer)
        self.heartbeat.setInterval(16)

        def beat():
            """采集心跳间隔，记录并关闭意外消息框，防止无人值守测试被阻塞。"""
            now = time.perf_counter()
            self.samples.append((now, (now - self.previous) * 1000))
            self.previous = now
            # 正常样本不应弹出提示；关闭意外消息框，以便输出错误记录并避免无人值守时阻塞。
            modal = self.app.activeModalWidget()
            if isinstance(modal, QMessageBox):
                self.errors.append(f"Unexpected message box: {modal.windowTitle()}: {modal.text()}")
                modal.done(0)

        self.heartbeat.timeout.connect(beat)
        self.heartbeat.start()

    def open(self):
        """创建真实主窗口并核验其使用隔离管理器，等待启动稳定后安装阶段计时。"""
        from src.ui.main_window import MainWindow

        self.window = MainWindow()
        self.window.resize(1280, 800)
        self.window.show()
        if self.result["formal_gui"]:
            self.window.raise_()
            self.window.activateWindow()
        if self.window._mod_manager is not self.manager:
            raise RuntimeError("MainWindow manager escaped isolated fixture")
        self.wait_idle("startup")
        self.install_phase_timers()

    def install_phase_timers(self):
        """测量实际绑定调用；父子调用的重叠耗时不能直接相加。"""
        from PySide6.QtCore import QThread

        from src.core import preset_deployment_plan

        groups = (
            (
                self.window,
                "window",
                (
                    "_reconcile_library",
                    "_refresh_current",
                    "_load_characters",
                    "_refresh_overview_mods",
                    "_reload_overview_level",
                    "_refresh_gallery",
                    "_refresh_preset_combo",
                    "_apply_overview_mods",
                ),
            ),
            (
                self.manager,
                "manager",
                (
                    "reconcile_library",
                    "reconcile_deployment_locations",
                    "claim_relocated_containers",
                    "reconcile_missing_sources",
                    "_discover_library_mods",
                    "_cleanup_orphaned_managed_artifacts",
                ),
            ),
            (
                self.manager.preset_manager,
                "preset",
                (
                    "prepare_preset_apply",
                    "commit_preset_apply",
                    "create_preset",
                    "_save",
                    "_capture_references",
                    "_resolve_references",
                    "_read_plan_mods",
                    "_before_plan_io",
                    "_before_plan_commit",
                    "_after_plan_commit",
                ),
            ),
            (preset_deployment_plan, "preset_plan", ("build_preset_deployment_plan", "validate_plan_filesystem")),
            (
                self.manager._index,
                "index",
                ("get_all_mods", "get_mods_by_character", "get_library_rows", "get_deployments"),
            ),
            (self.manager.variants, "variants", ("_preset_members", "collect", "restore")),
            (
                self.window._overview_page,
                "overview",
                ("set_current_mods", "_refresh_current_level", "_build_display_items", "_refresh_cards"),
            ),
            (self.window._overview_refresh, "overview_refresh", ("apply",)),
        )
        for owner, prefix, names in groups:
            for name in names:
                original = getattr(owner, name)

                @wraps(original)
                def measured(*args, _original=original, _label=f"{prefix}.{name}", **kwargs):
                    """调用原阶段函数，仅当执行线程是应用主线程时记录该次耗时。"""
                    start = time.perf_counter()
                    try:
                        return _original(*args, **kwargs)
                    finally:
                        if QThread.currentThread() == self.app.thread():
                            self.phase_events.append((_label, (time.perf_counter() - start) * 1000))

                self.phase_stack.enter_context(patch.object(owner, name, measured))

    def wait_until(self, predicate, description, timeout_ms=30000, *, check_errors=True):
        """通过真实 Qt 事件循环等待，在 Qt 槽外传播条件检查异常。"""
        from PySide6.QtCore import QEventLoop, QTimer

        loop = QEventLoop()
        polling, deadline = QTimer(), QTimer()
        failure, complete = [], []
        polling.setInterval(10)
        deadline.setSingleShot(True)

        def check():
            """轮询目标条件和已记录错误，在完成或异常时退出局部事件循环。"""
            try:
                if check_errors and self.errors:
                    raise RuntimeError(self.errors[-1])
                if predicate():
                    complete.append(True)
                    loop.quit()
            except BaseException as error:
                failure.append(error)
                loop.quit()

        polling.timeout.connect(check)
        deadline.timeout.connect(loop.quit)
        check()
        if not complete and not failure:
            polling.start()
            deadline.start(timeout_ms)
            loop.exec()
        polling.stop()
        deadline.stop()
        if failure:
            raise failure[0]
        if not complete:
            try:
                self.record_timeout(description, timeout_ms)
            except Exception as error:
                self.result.setdefault("timeout_diagnostic_errors", []).append(repr(error))
            raise TimeoutError(f"timed out after {timeout_ms} ms: {description}")

    def record_timeout(self, description, timeout_ms):
        """在最初截止时间记录控制器归属和 Python 调用栈。"""
        window = self.window
        discovery = window._library_discovery
        probe = window._library_probe
        workers = {
            "discovery": discovery.worker,
            "probe": probe.worker,
            "intake": window._intake_worker,
            "preset": window._preset_apply_worker,
        }

        def options(value):
            """提取探测请求中用于诊断调度的游戏、触发源和发现选项。"""
            if value is None:
                return None
            return {key: value.get(key) for key in ("game_id", "trigger", "manual", "discover")}

        state = {
            "host_busy": window._host_library_busy(),
            "discovery_active": options(discovery.active),
            "discovery_pending": options(discovery.pending),
            "discovery_planning": discovery.planning,
            "discovery_closing": discovery.closing,
            "probe_applying": probe.applying,
            "probe_generation": probe.generation,
            "overview_active": window._overview_refresh.active,
            "watcher_debounce": window._watcher_debounce.isActive(),
            "workers": {
                name: {"running": worker.isRunning(), "finished": worker.isFinished()}
                for name, worker in workers.items()
                if worker is not None
            },
        }
        names = {thread.ident: thread.name for thread in threading.enumerate()}
        threads = []
        for ident, frame in sys._current_frames().items():
            owners = set()
            scans = []
            current = frame
            while current is not None:
                owner = current.f_locals.get("self")
                owners.update(name for name, worker in workers.items() if worker is not None and owner is worker)
                if current.f_code.co_name in ("_discover_library_mods", "scan_library_directory"):
                    values = current.f_locals
                    scans.append(
                        {
                            "function": current.f_code.co_name,
                            "entry": str(values.get("entry", "")),
                            "directory": str(values.get("char_lib", values.get("char_dir", ""))),
                            "found": len(values.get("discovered", values.get("results", ()))),
                        }
                    )
                current = current.f_back
            threads.append(
                {
                    "id": ident,
                    "name": names.get(ident, "Qt/native thread"),
                    "workers": sorted(owners),
                    "scans": scans,
                    "stack": traceback.format_stack(frame),
                }
            )
        self.result.setdefault("timeouts", []).append(
            {
                "description": description,
                "timeout_ms": timeout_ms,
                "state": state,
                "threads": threads,
            }
        )
        self.save()

    def wait_idle(self, description, *, check_errors=True):
        """等待主窗口事务、库探测、发现、总览刷新和监控防抖均停止活动。"""
        window = self.window
        self.wait_until(
            lambda: (
                not window._host_library_busy()
                and not window._library_probe.applying
                and window._library_probe.worker is None
                and window._library_discovery.worker is None
                and window._library_discovery.active is None
                and window._library_discovery.pending is None
                and not window._overview_refresh.active
                and not window._watcher_debounce.isActive()
            ),
            description,
            check_errors=check_errors,
        )

    def await_beats(self, count=6):
        """等待新增指定数量的心跳，返回实际增加的样本数以验证界面仍有响应。"""
        before = len(self.samples)
        self.wait_until(lambda: len(self.samples) >= before + count, "heartbeat progress")
        return len(self.samples) - before

    def metrics(self):
        """深复制当前库探测指标，避免后续更新改变比较基线。"""
        return copy.deepcopy(self.window._library_probe_metrics)

    def step(self, name, entry, operation, *, controlled_barrier=False):
        """执行单个操作场景，保存心跳及后台指标差值，异常路径也记录操作尾部采样。"""
        self.wait_idle(f"before {name}")
        before = len(self.samples)
        phases_before = len(self.phase_events)
        metrics_before = self.metrics()
        discovery_before = copy.deepcopy(self.window._library_discovery.metrics)
        overview_before = copy.deepcopy(self.window._overview_refresh.metrics)
        start = time.perf_counter()
        self.previous = start  # 排除操作前校验测试样本的耗时。
        item = {"scenario": name, "entry": entry, "controlled_barrier": controlled_barrier, "completed": False}
        self.result["scenarios"].append(item)
        self.save()
        try:
            item["checks"] = operation() or {}
            item["completed"] = True
        finally:
            operation_end = time.perf_counter()
            operation_error = sys.exc_info()[1]
            tail_before = len(self.samples)
            tail_error = None
            try:
                # 同步操作可能早于 Qt 延迟心跳返回；保留该心跳样本，但不将等待时间计入操作耗时。
                self.wait_until(lambda: len(self.samples) > tail_before, "post-operation heartbeat", check_errors=False)
            except BaseException as error:
                tail_error = error
                item["completed"] = False
                item["heartbeat_tail_error"] = repr(error)
            gaps = [gap for _, gap in self.samples[before:]]
            item["wall_s"] = operation_end - start
            item["heartbeat_samples"] = len(gaps)
            item["heartbeat_interval_ms"] = bench.percentiles(gaps) if gaps else None
            item["heartbeat_delay_ms"] = bench.percentiles([max(0.0, gap - 16) for gap in gaps]) if gaps else None
            after = self.metrics()
            item["probe_counts"] = {
                key: after[key] - metrics_before[key] for key in ("started", "completed", "discarded")
            }
            item["max_active_periodic_workers"] = after["max_active"]
            item["periodic_snapshot_ms"] = after["snapshot_ms"][len(metrics_before["snapshot_ms"]) :]
            item["periodic_apply_slice_ms"] = after["apply_slice_ms"][len(metrics_before["apply_slice_ms"]) :]
            item["periodic_slow_items"] = after["slow_items"][len(metrics_before["slow_items"]) :]
            discovery_after = self.window._library_discovery.metrics
            item["discovery"] = {
                key: (value - discovery_before[key] if isinstance(value, int) else value[len(discovery_before[key]) :])
                for key, value in discovery_after.items()
            }
            item["overview_refresh"] = {
                key: (value - overview_before[key] if isinstance(value, int) else value[len(overview_before[key]) :])
                for key, value in self.window._overview_refresh.metrics.items()
            }
            phases = {}
            for label, elapsed in self.phase_events[phases_before:]:
                phase = phases.setdefault(label, {"calls": 0, "sum_ms": 0.0, "max_ms": 0.0})
                phase["calls"] += 1
                phase["sum_ms"] += elapsed
                phase["max_ms"] = max(phase["max_ms"], elapsed)
            item["main_thread_phases"] = phases
            self.save()
            if tail_error is not None and operation_error is None:
                raise tail_error
        if item["max_active_periodic_workers"] > 1:
            raise AssertionError("overlapping periodic probes")
        bench.emit("scenario", scenario=name, completed=True, seconds=item["wall_s"])

    @contextmanager
    def blocked_probe(self):
        """在一个真实只读探测完成后、返回前暂停该任务。"""
        from src.ui import library_reconcile_worker as module

        self.wait_idle("before blocked probe")
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = module.probe_library
        held = []

        def probe(request, **kwargs):
            """完成真实探测后阻塞首个结果，提供可控的过期结果竞态，屏障超时则报告失败。"""
            result = original(request, **kwargs)
            if not held:
                held.append(result)
                entered.set()
                if not release.wait(30):
                    raise TimeoutError("scenario probe barrier was not released")
            return result

        with patch.object(module, "probe_library", probe):
            self.window._reconcile_source_state()
            self.wait_until(entered.is_set, "probe entered barrier")
            if held[0].error:
                raise RuntimeError(held[0].error)
            try:
                yield release, held[0]
            finally:
                release.set()
                self.wait_until(lambda: self.window._library_probe.worker is None, "blocked probe thread finished")

    def check_mod(self, mod_id, *, enabled=None):
        """核验指定 Mod 的源身份、预览及部署身份，并可检查预期启用状态。"""
        from PySide6.QtGui import QImage

        from src.core.mod_identity import read_deployment_identity, read_source_mod_id

        mod = self.manager.get_mod(mod_id)
        if mod is None:
            raise AssertionError(f"missing Mod: {mod_id}")
        assert Path(mod.path).exists()
        assert read_source_mod_id(mod.path) == mod.mod_id
        assert mod.preview_images and all(not QImage(path).isNull() for path in mod.preview_images)
        if enabled is not None:
            assert mod.is_enabled is enabled
        if mod.deployed_path:
            identity = read_deployment_identity(mod.deployed_path)
            assert identity and identity["mod_id"] == mod.mod_id
        if mod.is_enabled:
            assert mod.deployed_path and Path(mod.deployed_path).is_dir()
        return mod

    def close(self):
        """释放所有受控屏障并安全关闭窗口，最后恢复消息处理、异常钩子和阶段补丁。"""
        from PySide6.QtCore import qInstallMessageHandler

        for release in self.releases:
            release.set()
        try:
            if self.window is not None:
                if not self.closed:
                    self.wait_idle("settle before cleanup close", check_errors=False)
                # 关闭场景只隐藏窗口；恢复路径或删除样本前仍须销毁 Qt 对象树。
                bench.close_gui_window(self.window, self.app)
                self.closed = True
        finally:
            self.heartbeat.stop()
            self.phase_stack.close()
            sys.excepthook = self.old_hook
            qInstallMessageHandler(self.old_message_handler)


def run_scenarios(manager, root, count, result, save, *, offscreen_smoke):
    """准备隔离的双游戏样本，依次验证启停、预设、导入、外部变化、切换及关闭竞态。"""
    from src.core.game_config_store import GameConfigStore
    from src.core.mod_manager import ReconcileReport

    result["fixture"] = bench.build_library(manager, root, count)
    rebuild = manager.rebuild_index_from_filesystem(bench.GAME)
    assert rebuild.registered == count and not rebuild.invalid_metadata
    _, mods = bench.check_library(manager, count)
    original_identities = {mod.mod_id for mod in mods}
    first, second = mods[:2]
    store = GameConfigStore()
    second_deploy = root / "SecondMods"
    second_deploy.mkdir()
    store.set_games(
        store.get_games()
        + [
            {
                "id": SECOND_GAME,
                "name": "Scenario Second",
                "name_en": "Scenario Second",
                "mods_path": str(second_deploy),
                "resource_dir": SECOND_GAME,
            }
        ]
    )
    manager._game_service.reload_games()
    runner = ScenarioRunner(manager, root, result, save, offscreen_smoke=offscreen_smoke)
    imported = []
    try:
        runner.open()
        window = runner.window
        window._overview_page._navigate_to(first.node_id)
        runner.wait_idle("overview category opened")

        def toggle(enabled):
            """通过主窗口启停入口操作首个 Mod，等待部署结束并验证源及部署状态。"""
            window._on_overview_mod_toggle(first.mod_id)
            runner.wait_until(
                lambda: manager.get_mod(first.mod_id).is_enabled == enabled and not window._deploy_workers,
                f"toggle enabled={enabled}",
            )
            mod = runner.check_mod(first.mod_id, enabled=enabled)
            return {
                "enabled": mod.is_enabled,
                "identity_and_previews_valid": True,
                "deployment_path": mod.deployed_path,
            }

        runner.step("enable", "GUI MainWindow._on_overview_mod_toggle", lambda: toggle(True))
        runner.step("disable", "GUI MainWindow._on_overview_mod_toggle", lambda: toggle(False))

        def presets():
            """创建两组测试预设并通过界面入口切换，核验日志收敛及对应 Mod 的启用状态。"""
            pm = manager.preset_manager
            a = pm.create_preset(bench.GAME, "Scenario A", {first.node_id: [first.mod_id]})
            b = pm.create_preset(bench.GAME, "Scenario B", {second.node_id: [second.mod_id]})
            for preset, active, inactive in ((a, first, second), (b, second, first)):
                window._on_preset_apply(preset.id)
                runner.wait_until(
                    lambda: (
                        window._preset_apply_worker is None
                        and pm.get_current(bench.GAME).id == preset.id
                        and not pm.has_pending_apply(bench.GAME)
                    ),
                    "preset committed",
                )
                runner.check_mod(active.mod_id, enabled=True)
                runner.check_mod(inactive.mod_id, enabled=False)
                runner.wait_idle("preset discovery and refresh settled")
            return {
                "switches": 2,
                "journal_settled": True,
                "active_mod": second.mod_id,
                "preset_creation": "core PresetManager.create_preset fixture setup",
                "identity_and_previews_valid": True,
            }

        runner.step("preset_switch", "GUI MainWindow._on_preset_apply (core preset setup)", presets)

        def intake():
            """阻塞真实导入准备结果，验证界面响应、在途预览保留和过期探测丢弃，再放行提交。"""
            source = root / "inputs" / "Incoming"
            source.mkdir(parents=True)
            (source / "mod.ini").write_text("[Constants]\nglobal $scenario = 900001\n", encoding="utf-8")
            (source / "preview.png").write_bytes(bench.PREVIEW)
            (source / "positions.buf").write_bytes(b"scenario" * (128 * 1024))
            service = window._intake_service
            request = service.plan_request(source_path=source, game_id=bench.GAME, node_id=first.node_id)
            entered, release = threading.Event(), threading.Event()
            runner.releases.append(release)
            prepared = []
            original = service.prepare

            def prepare(*args, **kwargs):
                """执行真实导入准备并保留结果，在提交前等待测试屏障放行。"""
                item = original(*args, **kwargs)
                prepared.append(item)
                entered.set()
                if not release.wait(30):
                    raise TimeoutError("scenario intake barrier was not released")
                return item

            with runner.blocked_probe() as (probe_release, evidence):
                discarded = runner.metrics()["discarded"]
                with patch.object(service, "prepare", prepare):
                    report = ReconcileReport(game_id=bench.GAME, trigger="manual", intake_requests=[request])
                    assert window._start_intake_batch(report, manual=False)
                    runner.wait_until(entered.is_set, "actual intake prepare finished")
                    item = prepared[0]
                    assert manager.get_mod(item.mod.mod_id) is None
                    assert item.mod.mod_id in manager.inflight_mod_ids()
                    window._overview_page._on_mod_card_clicked(first.mod_id)
                    beats = runner.await_beats()
                    assert window._overview_page._selected_mod.mod_id == first.mod_id
                    probe_release.set()
                    runner.wait_until(
                        lambda: runner.metrics()["discarded"] > discarded,
                        "old result discarded while intake awaits commit",
                    )
                    assert manager.get_mod(item.mod.mod_id) is None
                    assert all(Path(path).exists() for path in item.mod.preview_images)
                    release.set()
                    runner.wait_until(
                        lambda: window._intake_worker is None and not window._intake_settling,
                        "intake main-thread commit and settling",
                    )
                    imported.append(runner.check_mod(item.mod.mod_id, enabled=False))
            return {
                "heartbeat_ticks_during_held_prepared_item": beats,
                "selection_responded": True,
                "inflight_cache_preserved": True,
                "stale_probe_discarded": True,
                "committed_mod": imported[0].mod_id,
                "source_token": vars(evidence.token),
                "barrier": "after actual prepare; before main-thread commit",
                "planning": "core ModIntakeService.plan_request",
            }

        runner.step(
            "intake_response", "GUI MainWindow._start_intake_batch + real IntakeWorker", intake, controlled_barrier=True
        )

        def rename():
            """模拟源容器外部改名，等待正常对账更新路径并核验身份及部署保持有效。"""
            before = runner.check_mod(second.mod_id, enabled=True)
            container = Path(before.path).parent
            renamed = container.with_name("ExternallyRenamed")
            container.rename(renamed)
            expected_path = str(renamed / Path(before.path).name)
            window._reconcile_source_state()
            runner.wait_until(lambda: manager.get_mod(second.mod_id).path == expected_path, "external rename reflected")
            after = runner.check_mod(second.mod_id, enabled=True)
            assert after.mod_id == before.mod_id
            return {
                "mod_id_preserved": True,
                "deployment_identity_preserved": True,
                "source_path_updated": True,
                "previews_valid": True,
            }

        runner.step("external_rename", "filesystem rename; normal GUI watcher/periodic consumers", rename)

        def deletion():
            """删除隔离样本的源容器，等待正常消费者清理记录、所属部署及预设引用。"""
            target = runner.check_mod(second.mod_id, enabled=True)
            bench.owned_remove(Path(target.path).parent, root)
            window._reconcile_source_state()
            runner.wait_until(lambda: manager.get_mod(second.mod_id) is None, "external deletion reflected")
            locations = manager._storage.scan_deployments(force=True)
            assert target.mod_id not in locations
            assert all(
                second.mod_id not in values
                for preset in manager.preset_manager.get_all_presets(bench.GAME)
                for values in preset.character_mods.values()
            )
            return {
                "record_removed": True,
                "owned_deployment_removed": True,
                "all_preset_references_removed": True,
                "verification": "read-only DB/preset reads and core deployment inventory",
            }

        runner.step("external_delete", "filesystem delete; normal GUI watcher/periodic consumers", deletion)

        def switch_game():
            """在旧游戏探测结果等待发布时切换游戏，验证过期结果被丢弃且两游戏数据保持隔离。"""
            before = {row.mod_id for row in manager._index.get_library_rows(bench.GAME)}
            with runner.blocked_probe() as (release, evidence):
                discarded = runner.metrics()["discarded"]
                window._on_game_changed(SECOND_GAME)
                assert manager.current_game_id == SECOND_GAME
                release.set()
                runner.wait_until(lambda: runner.metrics()["discarded"] > discarded, "old-game result discarded")
                assert manager._index.get_library_rows(SECOND_GAME) == ()
                assert {row.mod_id for row in manager._index.get_library_rows(bench.GAME)} == before
            window._on_game_changed(bench.GAME)
            runner.wait_idle("return to original game")
            return {
                "round_trip": [bench.GAME, SECOND_GAME, bench.GAME],
                "old_game_result_discarded": True,
                "original_game_rows_preserved": True,
                "second_game_empty": True,
                "source_token": vars(evidence.token),
            }

        runner.step("game_switch_stale_result", "GUI MainWindow._on_game_changed", switch_game, controlled_barrier=True)

        remaining = manager._index.get_all_mods(bench.GAME)
        for mod in remaining:
            runner.check_mod(mod.mod_id)
        expected = original_identities - {second.mod_id} | {imported[0].mod_id}
        assert {mod.mod_id for mod in remaining} == expected
        assert len(remaining) == count
        result["final_library"] = {
            "registered": len(remaining),
            "expected_identities_match": True,
            "all_source_and_preview_references_valid": True,
        }

        def close_active():
            """在探测结果被阻塞时请求关闭窗口，验证界面继续响应并在工作退出后完成关闭。"""
            with runner.blocked_probe() as (release, evidence):
                worker = window._library_probe.worker
                window.close()
                assert window.isVisible() and window._library_probe.worker is worker
                assert worker.isInterruptionRequested()
                beats = runner.await_beats(3)
                release.set()
                runner.wait_until(
                    lambda: not window.isVisible() and window._library_probe.worker is None,
                    "window closes after probe finished",
                )
            runner.closed = True
            return {
                "close_deferred_while_probe_running": True,
                "heartbeat_ticks_while_close_deferred": beats,
                "worker_released_before_window_close": True,
                "source_token": vars(evidence.token),
            }

        runner.step("close_during_probe", "GUI MainWindow.close / closeEvent", close_active, controlled_barrier=True)
        result["library_probe_metrics"] = runner.metrics()
        assert result["library_probe_metrics"]["max_active"] <= 1
        assert not runner.errors
    finally:
        runner.close()


def main(argv=None):
    """校验场景规模，使用新输出目录执行隔离测试并保存完成、错误和清理结果。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=300)
    parser.add_argument("--offscreen-smoke", action="store_true", help="non-formal wiring check; requires count <= 30")
    args = parser.parse_args(argv)
    if args.count < 3 or (args.offscreen_smoke and args.count > 30):
        parser.error("count must be >= 3; offscreen smoke count must be <= 30")
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output / "results.json"
    if output.exists():
        raise FileExistsError(output)
    result = {
        "completed": False,
        "count": args.count,
        "version": bench.source_version(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scenarios": [],
        "notes": [
            "Functional action coverage; does not replace three 60-second scale samples.",
            "No mouse automation: actual GUI slots run under a real Qt event loop.",
            "Fixture construction and initial rebuild are outside action timing.",
            "Event barriers hold real prepared intake/probe results for reproducible races.",
            "Controlled-barrier heartbeat samples are not disk-throughput measurements.",
            "Main-thread phase timers include nested calls; sums are not additive.",
            "Only temporary game/config/database/Mod/cache directories are writable inputs.",
        ],
    }

    def save():
        """写出当前场景报告，使操作完成前后的检查点及错误信息可供复查。"""
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    save()
    with tempfile.TemporaryDirectory(prefix="d3dx-scenarios-") as temporary:
        anchor = Path(temporary)
        root = anchor / "case"
        result["isolated_root"] = str(root)
        try:
            with bench.isolated_manager(root) as manager:
                run_scenarios(manager, root, args.count, result, save, offscreen_smoke=args.offscreen_smoke)
                result["completed"] = True
        except BaseException as error:
            result["error"] = {"message": str(error), "traceback": traceback.format_exc()}
        finally:
            if root.exists():
                bench.owned_remove(root, anchor)
            result["isolation_cleaned"] = not root.exists()
            save()
    return 0 if result["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
