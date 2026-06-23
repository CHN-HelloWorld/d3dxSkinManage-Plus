import shutil
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import QTimer

from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def test_local_event_defers_audit_snapshot_without_cancelling_it(qtbot, mm, monkeypatch):
    from src.ui import library_reconcile_worker as module

    captures = []
    original = mm.iter_library_probe_request

    def capture(token):
        captures.append(token)
        return original(token)

    monkeypatch.setattr(mm, "iter_library_probe_request", capture)
    controller = module.LibraryReconcileController(mm, busy=lambda: False)
    try:
        controller.yield_to_local_events()
        controller.schedule()
        assert controller.pending and controller.worker is None
        assert not captures
        qtbot.waitUntil(lambda: controller.metrics["completed"] == 1)
        assert len(captures) == 1
        assert controller.metrics["discarded"] == 0
    finally:
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


def test_local_event_pauses_running_audit_at_checkpoint(qtbot, mm, monkeypatch):
    from src.ui import library_reconcile_worker as module

    entered, proceed, checked = Event(), Event(), Event()
    original = module.probe_library

    def probe(request, *, should_cancel):
        entered.set()
        assert proceed.wait(5)
        cancelled = should_cancel()
        checked.set()
        assert not cancelled
        return original(request, should_cancel=should_cancel)

    monkeypatch.setattr(module, "probe_library", probe)
    controller = module.LibraryReconcileController(mm, busy=lambda: False)
    try:
        controller.schedule()
        qtbot.waitUntil(entered.is_set)
        worker = controller.worker
        controller.yield_to_local_events()
        proceed.set()
        qtbot.wait(20)
        assert not checked.is_set()
        assert controller.worker is worker
        qtbot.waitUntil(lambda: controller.metrics["completed"] == 1)
        assert checked.is_set()
        assert controller.metrics["discarded"] == 0
    finally:
        proceed.set()
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


def test_periodic_ticks_are_covered_by_running_audit(qtbot, mm, monkeypatch):
    from src.ui import library_reconcile_worker as module

    entered, release = Event(), Event()
    original = module.probe_library

    def probe(request, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(request, **kwargs)

    monkeypatch.setattr(module, "probe_library", probe)
    controller = module.LibraryReconcileController(mm, busy=lambda: False)
    try:
        controller.schedule()
        qtbot.waitUntil(entered.is_set)
        for _ in range(10):
            controller.schedule(periodic=True)
        assert not controller.pending
        release.set()
        qtbot.waitUntil(lambda: controller.metrics["completed"] == 1)
        assert controller.metrics["started"] == 1
        assert not controller.pending
    finally:
        release.set()
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


def test_snapshot_slices_yield_to_ui_and_discard_intervening_changes(qtbot, mm, monkeypatch):
    import time

    from src.ui import library_reconcile_worker as module

    original_capture = mm.iter_library_probe_request
    original_probe = module.probe_library
    captures, requests, nodes = [], [], []

    def capture(token):
        captures.append(token)
        result = yield from original_capture(token)
        for _ in range(5):
            time.sleep(0.005)
            yield None
        return result

    def probe(request, **kwargs):
        requests.append(request)
        return original_probe(request, **kwargs)

    monkeypatch.setattr(mm, "iter_library_probe_request", capture)
    monkeypatch.setattr(module, "probe_library", probe)
    controller = module.LibraryReconcileController(mm, busy=lambda: False)
    QTimer.singleShot(0, lambda: nodes.append(mm.add_character(name="During snapshot", node_type="character")))
    try:
        controller.schedule()
        assert controller.worker is None and controller._snapshot_iterator is not None
        qtbot.waitUntil(lambda: controller.metrics["completed"] == 1)
        assert len(captures) == 2
        assert len(requests) == 1 and requests[0].token == captures[1]
        assert any(location.node_id == nodes[0].id for location in requests[0].scan_locations)
    finally:
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


def test_blocked_probe_keeps_heartbeat_and_coalesces_timers(qtbot, mm, monkeypatch):
    from src.ui import library_reconcile_worker as module

    entered, release = Event(), Event()
    original = module.probe_library

    def blocked(request, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(request, **kwargs)

    monkeypatch.setattr(module, "probe_library", blocked)
    controller = module.LibraryReconcileController(mm, busy=lambda: False)
    beats = []
    timer = QTimer()
    timer.setInterval(1)
    timer.timeout.connect(lambda: beats.append(1))
    timer.start()
    try:
        controller.schedule()
        qtbot.waitUntil(entered.is_set)
        controller.schedule()
        controller.schedule()
        qtbot.waitUntil(lambda: len(beats) >= 3)
        assert controller.metrics["started"] == 1
        release.set()
        qtbot.waitUntil(lambda: controller.metrics["completed"] == 2)
        assert controller.metrics["started"] == 2
        assert controller.metrics["max_active"] == 1
    finally:
        release.set()
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)
        timer.stop()


def test_game_round_trip_discards_blocked_result(qtbot, mm, tmp_path, monkeypatch):
    from src.ui import library_reconcile_worker as module

    char = mm.add_character(name="Probe", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "round_trip")
    entered, release = Event(), Event()
    original = module.probe_library

    def blocked(request, **kwargs):
        result = original(request, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(module, "probe_library", blocked)
    controller = module.LibraryReconcileController(mm, busy=lambda: False)
    try:
        controller.schedule()
        qtbot.waitUntil(entered.is_set)
        mm.set_game("game_b")
        mm.set_game("game_a")
        release.set()
        qtbot.waitUntil(lambda: controller.metrics["discarded"] == 1)
        assert mm.get_mod(mod.mod_id) is not None
    finally:
        release.set()
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


@pytest.mark.parametrize("mutation", ["raw_sql", "rollback", "preset", "root", "intake"])
def test_completed_mutation_invalidates_result(qtbot, mm, tmp_path, monkeypatch, mutation):
    from src.ui import library_reconcile_worker as module

    char = mm.add_character(name="Probe", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "mutation")
    shutil.rmtree(Path(mod.path).parent)
    entered, release, retry_release = Event(), Event(), Event()
    original = module.probe_library

    def blocked(request, **kwargs):
        result = original(request, **kwargs)
        if entered.is_set():
            assert retry_release.wait(5)
            return result
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(module, "probe_library", blocked)
    controller = module.LibraryReconcileController(mm, busy=lambda: False)
    before = mm._index.change_stamp
    try:
        controller.schedule()
        qtbot.waitUntil(entered.is_set)
        if mutation in ("raw_sql", "rollback"):
            db = mm._index._db()
            db.execute("UPDATE mods SET folder_name=? WHERE mod_id=?", ("updated", mod.mod_id))
            db.rollback() if mutation == "rollback" else db.commit()
        elif mutation == "preset":
            mm.preset_manager.ensure_default("game_a")
        elif mutation == "root":
            mm._storage.set_migoto_mods_dir(tmp_path / "OtherMods")
        else:
            mm.register_inflight_mod_id("preparing")
            mm.unregister_inflight_mod_id("preparing")
        after_mutation = mm._index.change_stamp
        release.set()
        qtbot.waitUntil(lambda: controller.metrics["discarded"] == 1)
        assert mm.get_mod(mod.mod_id) is not None
        assert mm._index.change_stamp == after_mutation
        if mutation in ("raw_sql", "rollback"):
            assert after_mutation != before
    finally:
        release.set()
        retry_release.set()
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


def test_stop_retains_running_worker_without_waiting(qtbot, mm, monkeypatch):
    from src.ui import library_reconcile_worker as module

    entered, release = Event(), Event()
    original = module.probe_library

    def blocked(request, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(request, **kwargs)

    monkeypatch.setattr(module, "probe_library", blocked)
    controller = module.LibraryReconcileController(mm, busy=lambda: False)
    try:
        controller.schedule()
        qtbot.waitUntil(entered.is_set)
        worker = controller.worker
        controller.stop()
        assert controller.worker is worker
        assert worker.isRunning()
        assert worker.isInterruptionRequested()
        release.set()
        qtbot.waitUntil(lambda: controller.worker is None)
        assert controller.metrics["started"] == 1
    finally:
        release.set()
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


def test_busy_settling_defers_probe_until_next_request(qtbot, mm):
    from src.ui.library_reconcile_worker import LibraryReconcileController

    busy = [True]
    controller = LibraryReconcileController(mm, busy=lambda: busy[0])
    controller.schedule()
    assert controller.worker is None
    assert controller.pending
    busy[0] = False
    controller.schedule()
    qtbot.waitUntil(lambda: controller.metrics["completed"] == 1)
    controller.stop()


def test_controller_applies_real_missing_source_and_publishes_once(qtbot, mm, tmp_path):
    from src.ui.library_reconcile_worker import LibraryReconcileController

    char = mm.add_character(name="Probe", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "gone")
    shutil.rmtree(Path(mod.path).parent)
    controller = LibraryReconcileController(mm, busy=lambda: False)
    published = []
    controller.changed.connect(lambda *args: published.append(args))
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: len(published) == 1)
        assert mm.get_mod(mod.mod_id) is None
        assert published == [("game_a", 1)]
        controller.schedule()
        qtbot.waitUntil(lambda: controller.metrics["completed"] == 2)
        assert published == [("game_a", 1)]
    finally:
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


def test_mainwindow_close_waits_asynchronously_and_resumes_after_rejected_close(qtbot, mm, monkeypatch):
    from PySide6.QtWidgets import QMainWindow

    from src.ui import library_reconcile_worker as module
    from src.ui.main_window import MainWindow

    class Window(QMainWindow):
        closeEvent = MainWindow.closeEvent
        _on_library_probe_stopped = MainWindow._on_library_probe_stopped

        def _close_after_library_probe(self, event):
            self.confirmations += 1
            event.ignore()

    window = Window()
    window.confirmations = 0
    window._library_probe_close_pending = False
    window._library_probe = module.LibraryReconcileController(mm, busy=lambda: False, parent=window)
    window._library_probe.stopped.connect(window._on_library_probe_stopped)
    entered, release = Event(), Event()
    original = module.probe_library

    def blocked(request, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(request, **kwargs)

    monkeypatch.setattr(module, "probe_library", blocked)
    window.show()
    try:
        window._library_probe.schedule()
        qtbot.waitUntil(entered.is_set)
        window.close()
        assert window.isVisible()
        assert window.confirmations == 0
        release.set()
        qtbot.waitUntil(lambda: window.confirmations == 1)
        assert not window._library_probe.closing
        assert window.isVisible()
    finally:
        release.set()
        window._library_probe.stop()
        qtbot.waitUntil(lambda: window._library_probe.worker is None)
        window._close_after_library_probe = lambda event: event.accept()
        window.close()


def test_both_mainwindow_timers_share_controller(qtbot, mm, monkeypatch):
    from types import SimpleNamespace

    from src.ui import library_reconcile_worker as module
    from src.ui.main_window import MainWindow

    entered, release = Event(), Event()
    original = module.probe_library

    def blocked(request, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(request, **kwargs)

    monkeypatch.setattr(module, "probe_library", blocked)
    controller = module.LibraryReconcileController(mm, busy=lambda: False)
    window = SimpleNamespace(_game_service=SimpleNamespace(has_active_game=lambda: True), _library_probe=controller)
    try:
        MainWindow._reconcile_source_state(window)
        qtbot.waitUntil(entered.is_set)
        MainWindow._reconcile_deploy_state(window)
        assert controller.metrics["started"] == 1
        assert not controller.pending
    finally:
        release.set()
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


def test_batch_detects_intervening_sql_and_publishes_committed_changes_after_retry(qtbot, mm, tmp_path, monkeypatch):
    from src.ui.library_reconcile_worker import LibraryReconcileController

    char = mm.add_character(name="Probe", node_type="character")
    mods = [_import_mod(mm, char.id, tmp_path, name) for name in ("first", "second")]
    for mod in mods:
        shutil.rmtree(Path(mod.path).parent)
    controller = LibraryReconcileController(mm, busy=lambda: False)
    controller.APPLY_BUDGET_MS = 0
    published = []
    controller.changed.connect(lambda *args: published.append(args))
    apply = mm.apply_library_probe_result
    injected = []

    def raw_write():
        mm._index._db().execute("UPDATE mods SET folder_name=?", ("changed during apply",))
        mm._index._db().commit()

    def interleave(result):
        for step in apply(result):
            if step.changed and not injected:
                injected.append(True)
                QTimer.singleShot(0, raw_write)
            yield step

    monkeypatch.setattr(mm, "apply_library_probe_result", interleave)
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: controller.metrics["discarded"] == 1)
        assert len(mm._index.get_library_rows("game_a")) == 1
        assert published == []
        controller.schedule()
        qtbot.waitUntil(lambda: bool(published))
        assert mm._index.get_library_rows("game_a") == ()
        assert published == [("game_a", 2)]
    finally:
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


def test_plugin_callback_write_defers_remaining_publication(qtbot, mm, tmp_path):
    from src.ui.library_reconcile_worker import LibraryReconcileController

    char = mm.add_character(name="Probe", node_type="character")
    mods = [_import_mod(mm, char.id, tmp_path, name) for name in ("callback_a", "callback_b")]
    for mod in mods:
        shutil.rmtree(Path(mod.path).parent)
    events = []

    def plugin(event, **payload):
        if event == "mod_deleted":
            events.append(payload["mod_id"])
            if len(events) == 1:
                mm._index._db().execute("UPDATE nodes SET name=name")
                mm._index._db().commit()

    mm.add_event_listener(plugin)
    controller = LibraryReconcileController(mm, busy=lambda: False)
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: bool(events))
        assert len(events) == 1
        controller.schedule()
        qtbot.waitUntil(lambda: len(events) == 2)
        assert set(events) == {mod.mod_id for mod in mods}
    finally:
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


def test_probe_checkpoint_yields_and_observes_cancellation_during_yield(qapp, monkeypatch):
    from src.ui import library_reconcile_worker as module

    worker = module.LibraryReconcileWorker(None)
    current = [0.0]
    cancelled = [False]
    monkeypatch.setattr(module.time, "monotonic", lambda: current[0])
    monkeypatch.setattr(worker, "isInterruptionRequested", lambda: cancelled[0])
    assert worker._checkpoint() is False
    current[0] = 1.0

    def release_gil(_seconds):
        cancelled[0] = True

    monkeypatch.setattr(module.time, "sleep", release_gil)
    assert worker._checkpoint() is True


def test_missing_deployment_root_ends_probe_and_suppresses_periodic_retries(qtbot, mm, tmp_path):
    from src.ui.library_reconcile_worker import LibraryReconcileController

    root = tmp_path / "deployment"
    root.mkdir()
    mm.set_deploy_dir(str(root))
    moved = root.with_name("moved-deployment")
    root.rename(moved)
    controller = LibraryReconcileController(mm, busy=lambda: False)
    errors = []
    controller.failed.connect(errors.append)
    try:
        controller.schedule(force_source_audit=True)
        qtbot.waitUntil(lambda: controller.metrics["completed"] == 1)
        assert len(errors) == 1 and "FileNotFoundError" in errors[0]
        assert controller.paused and not controller.pending
        for _ in range(4):
            controller.schedule(periodic=True)
        qtbot.wait(1100)
        assert controller.metrics["started"] == 1
        assert not controller._retry.isActive()
        moved.rename(root)
        controller.reset_failure()
        controller.schedule(force_source_audit=True)
        qtbot.waitUntil(lambda: controller.metrics["completed"] == 2 and not controller.applying)
        assert not controller.paused and not controller.last_error
        assert controller.current_full_audit_state(mm.current_game_id, force_source_audit=True) == "settled"
    finally:
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


def test_cancel_closes_probe_snapshot_and_does_not_start_worker(qtbot, mm, monkeypatch):
    import time

    from src.ui.library_reconcile_worker import LibraryReconcileController

    original = mm.iter_library_probe_request
    closed = []

    def capture(token):
        try:
            time.sleep(0.010)
            yield None
            return (yield from original(token))
        finally:
            closed.append(True)

    monkeypatch.setattr(mm, "iter_library_probe_request", capture)
    controller = LibraryReconcileController(mm, busy=lambda: False)
    try:
        controller.schedule(force_source_audit=True)
        assert controller._snapshot_iterator is not None and controller.worker is None
        controller.cancel()
        qtbot.wait(30)
        assert closed == [True] and controller.metrics["started"] == 0
        assert not controller.pending and not controller._snapshot_timer.isActive()
        controller.schedule(force_source_audit=True)
        qtbot.waitUntil(lambda: controller.metrics["completed"] == 1)
    finally:
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


def test_cancelled_probe_does_not_retry_or_poison_immediate_new_refresh(qtbot, mm, monkeypatch):
    from src.ui import library_reconcile_worker as module

    entered, release = Event(), Event()
    original = module.probe_library
    calls = []

    def blocked(request, **kwargs):
        calls.append(request)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
            raise FileNotFoundError("obsolete root")
        return original(request, **kwargs)

    monkeypatch.setattr(module, "probe_library", blocked)
    controller = module.LibraryReconcileController(mm, busy=lambda: False)
    errors = []
    controller.failed.connect(errors.append)
    try:
        controller.schedule(force_source_audit=True)
        qtbot.waitUntil(entered.is_set)
        controller.schedule()
        controller.cancel()
        assert not controller.pending and not controller.closing
        assert controller.worker.isInterruptionRequested()
        controller.schedule(force_source_audit=True)
        release.set()
        qtbot.waitUntil(lambda: controller.metrics["completed"] == 2 and not controller.applying)
        assert len(calls) == 2 and not errors
        assert not controller.pending and not controller.paused
    finally:
        release.set()
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


@pytest.mark.parametrize("reentrant", [False, True])
def test_cancel_during_apply_preserves_committed_rows_without_continuing(qtbot, mm, tmp_path, monkeypatch, reentrant):
    from src.ui.library_reconcile_worker import LibraryReconcileController

    char = mm.add_character(name="Cancel", node_type="character")
    mods = [_import_mod(mm, char.id, tmp_path, name) for name in ("cancel_a", "cancel_b")]
    for mod in mods:
        shutil.rmtree(Path(mod.path).parent)
    controller = LibraryReconcileController(mm, busy=lambda: False)
    controller.APPLY_BUDGET_MS = 0
    original = mm.apply_library_probe_result
    committed = []

    def apply(result):
        for step in original(result):
            if step.changed and not committed:
                committed.append(step.item_id)
                if reentrant:
                    controller.cancel()
                else:
                    QTimer.singleShot(0, controller.cancel)
            yield step

    monkeypatch.setattr(mm, "apply_library_probe_result", apply)
    try:
        controller.schedule(force_source_audit=True)
        qtbot.waitUntil(lambda: bool(committed) and not controller.applying)
        qtbot.wait(1100)
        assert len(mm._index.get_library_rows(mm.current_game_id)) == 1
        assert mm.get_mod(committed[0]) is None
        assert controller.metrics["started"] == 1 and not controller.pending
        assert not controller.closing
        controller.schedule(force_source_audit=True)
        qtbot.waitUntil(lambda: controller.metrics["completed"] == 2 and not controller.applying)
        assert not mm._index.get_library_rows(mm.current_game_id)
    finally:
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)
