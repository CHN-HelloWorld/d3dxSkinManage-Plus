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
