import importlib.util
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QRunnable, Qt, QThread, QThreadPool, QTimer

from scripts.performance import perf_mod_library as bench


def test_worker_counter_tracks_new_detached_threads_without_rescanning_widgets(qapp, monkeypatch):
    ready, release = Event(), Event()

    class Worker(QThread):
        def run(self):
            ready.set()
            release.wait(3)

    window = QObject()
    page = QObject(window)
    child, detached = Worker(window), Worker()
    child.start()
    assert ready.wait(1)
    original_start = vars(QThread)["start"]
    try:
        with bench.RunningWorkerCounter(window, page, qapp) as counter:

            def forbidden(*args, **kwargs):
                raise AssertionError("heartbeat must not traverse the widget QObject tree")

            monkeypatch.setattr(window, "findChildren", forbidden)
            monkeypatch.setattr(qapp, "findChildren", forbidden)
            assert counter.count() == 1
            ready.clear()
            detached.start()
            assert ready.wait(1)
            assert counter.count() == 2
            assert counter.count() == 2
        assert vars(QThread)["start"] is original_start
    finally:
        release.set()
        assert child.wait(2000)
        assert detached.wait(2000)


@pytest.mark.parametrize("controller", ["_reconcile", "_library_probe", "_library_discovery"])
def test_worker_counter_includes_preexisting_controller_thread_and_pool(qapp, controller):
    ready, pool_ready, release = Event(), Event(), Event()

    class Worker(QThread):
        def run(self):
            ready.set()
            release.wait(3)

    class Work(QRunnable):
        def run(self):
            pool_ready.set()
            release.wait(3)

    window = QObject()
    page = QObject(window)
    worker = Worker()
    setattr(window, controller, SimpleNamespace(worker=worker))
    worker.start()
    assert ready.wait(1)
    pool = QThreadPool.globalInstance()
    try:
        with bench.RunningWorkerCounter(window, page, qapp) as counter:
            pool.start(Work())
            assert pool_ready.wait(1)
            assert counter.count() == 2
    finally:
        release.set()
        assert worker.wait(2000)
        assert pool.waitForDone(2000)


def test_worker_counter_restores_start_after_failure_and_ignores_deleted_threads(qapp):
    import shiboken6

    original_start = vars(QThread)["start"]
    window = QObject()
    worker = QThread(window)
    with pytest.raises(RuntimeError, match="sample failed"):
        with bench.RunningWorkerCounter(window, QObject(window), qapp) as counter:
            shiboken6.delete(worker)
            assert counter.count() == 0
            raise RuntimeError("sample failed")
    assert vars(QThread)["start"] is original_start


@pytest.mark.parametrize("outcome", ["return", "close", "raise"])
@pytest.mark.parametrize("tail_timeout", [False, True])
def test_scenario_records_next_heartbeat_without_adding_wait_to_operation_wall(
    qapp, monkeypatch, outcome, tail_timeout
):
    scripts = Path(__file__).parents[2] / "scripts" / "performance"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("scenario_sampling_test", scripts / "perf_mod_library_scenarios.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runner = module.ScenarioRunner.__new__(module.ScenarioRunner)
    clock = [10.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    probe = dict(started=0, completed=0, discarded=0, max_active=1, snapshot_ms=[], apply_slice_ms=[], slow_items=[])
    discovery = dict(started=0, completed=0, discarded=0, capture_ms=[], worker_ms=[], planning_slice_ms=[])
    runner.window = SimpleNamespace(
        _library_probe_metrics=probe,
        _library_discovery=SimpleNamespace(metrics=discovery),
        _overview_refresh=SimpleNamespace(metrics=dict(completed=0, discarded=0, slice_ms=[])),
    )
    runner.samples, runner.phase_events, runner.errors = [], [], []
    runner.result = {"scenarios": []}
    runner.save = lambda: None
    runner.wait_idle = lambda description: None
    runner.closed = False
    runner.heartbeat = QTimer()
    runner.heartbeat.setTimerType(Qt.TimerType.PreciseTimer)
    runner.heartbeat.setInterval(16)

    def beat():
        clock[0] = 10.25
        runner.samples.append((clock[0], (clock[0] - runner.previous) * 1000))
        runner.previous = clock[0]

    runner.heartbeat.timeout.connect(beat)
    runner.heartbeat.start()
    if tail_timeout:

        def fail_wait(predicate, description, timeout_ms=30000, *, check_errors=True):
            assert timeout_ms == 30000
            assert description == "post-operation heartbeat"
            assert not predicate()
            raise TimeoutError("missing heartbeat")

        runner.wait_until = fail_wait

    def operation():
        time.sleep(0.025)
        clock[0] = 10.05
        runner.closed = outcome == "close"
        if outcome == "raise":
            raise RuntimeError("original operation failure")
        return {"done": True}

    try:
        if outcome == "raise":
            with pytest.raises(RuntimeError, match="original operation failure"):
                runner.step("tail", "test", operation)
        elif tail_timeout:
            with pytest.raises(TimeoutError, match="missing heartbeat"):
                runner.step("tail", "test", operation)
        else:
            runner.step("tail", "test", operation)
        item = runner.result["scenarios"][0]
        if tail_timeout:
            assert item["heartbeat_samples"] == 0
            assert "missing heartbeat" in item["heartbeat_tail_error"]
        else:
            assert item["heartbeat_samples"] >= 1
            assert item["heartbeat_interval_ms"]["max"] == pytest.approx(250)
        assert item["wall_s"] == pytest.approx(0.05)
        assert item["completed"] == (outcome != "raise" and not tail_timeout)
    finally:
        runner.heartbeat.stop()
