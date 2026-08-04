from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject

from src.ui.library_reconcile_worker import LibraryReconcileController
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def trusted(mm):
    return getattr(mm, "_operation_trusted", False) == ("game_a", str(mm._index.paths.roots["game_a"]))


def stop(controller, qtbot):
    controller.stop()
    qtbot.waitUntil(lambda: controller.worker is None)


def test_complete_current_review_accepts_operation_trust(qtbot, mm):
    controller = LibraryReconcileController(mm, busy=lambda: False)
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: controller._settled_full_audit is not None)
        assert trusted(mm)
    finally:
        stop(controller, qtbot)


@pytest.mark.parametrize("event", ["invalidate", "local_event", "source", "deployment", "stop", "game"])
def test_external_change_or_stop_immediately_invalidates_trust(qtbot, mm, event):
    busy = [False]
    controller = LibraryReconcileController(mm, busy=lambda: busy[0])
    mm.accept_operation_trust("game_a")
    assert trusted(mm)
    busy[0] = True
    try:
        if event == "invalidate":
            controller.invalidate()
        elif event == "local_event":
            controller.yield_to_local_events()
        elif event in ("source", "deployment"):
            controller.schedule_changes(("changed",), deployment=event == "deployment")
        elif event == "stop":
            controller.stop()
        else:
            controller._sync_stamp()
            mm.accept_operation_trust("game_a")
            mm.set_game("game_b")
            controller._sync_stamp()
        assert not getattr(mm, "_operation_trusted", False)
    finally:
        stop(controller, qtbot)


@pytest.mark.parametrize(
    "case",
    [
        "source",
        "deployment",
        "error",
        "cancelled",
        "unchecked",
        "entry_error",
        "wrong_root",
        "missing_retry",
        "tainted",
        "pending_native_missing",
        "deferred_events",
    ],
)
def test_incomplete_or_uncertain_result_never_accepts_trust(qtbot, mm, tmp_path, monkeypatch, case):
    from src.ui import library_reconcile_worker as module

    node = mm.add_character("trust")
    _import_mod(mm, node.id, tmp_path, "trust")
    original = module.probe_library

    def probe(request, **kwargs):
        result = original(request, **kwargs)
        assert not result.error
        if case in ("source", "deployment"):
            return replace(result, request=replace(request, mode=case))
        if case == "error":
            return replace(result, error="inaccessible source")
        if case == "cancelled":
            return replace(result, cancelled=True)
        if case == "unchecked":
            return replace(result, checks=tuple(replace(item, state="unchecked") for item in result.checks))
        if case == "entry_error":
            return replace(
                result,
                source_inventory=replace(result.source_inventory, entries=(SimpleNamespace(error="invalid identity"),)),
            )
        if case == "wrong_root":
            return replace(result, source_inventory=replace(result.source_inventory, root="other-library"))
        return result

    monkeypatch.setattr(module, "probe_library", probe)
    if case == "missing_retry":
        monkeypatch.setattr(mm, "has_pending_source_cleanup", lambda: True)
    elif case in ("tainted", "pending_native_missing"):
        mm._local_source_events = SimpleNamespace(
            _tainted=case == "tainted", _missing={"m": True} if case == "pending_native_missing" else {}
        )
    elif case == "deferred_events":
        mm._deferred_source_events = {"pending": None}
    controller = LibraryReconcileController(mm, busy=lambda: False)
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: controller.metrics["completed"] >= 1)
        assert not trusted(mm)
    finally:
        stop(controller, qtbot)


def test_local_event_during_full_review_requires_a_new_review_for_trust(qtbot, mm, monkeypatch):
    from src.ui import library_reconcile_worker as module

    entered, release = Event(), Event()
    original = module.probe_library

    def probe(request, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(request, **kwargs)

    monkeypatch.setattr(module, "probe_library", probe)
    controller = LibraryReconcileController(mm, busy=lambda: False)
    try:
        controller.schedule()
        qtbot.waitUntil(entered.is_set)
        controller.yield_to_local_events()
        release.set()
        qtbot.waitUntil(lambda: controller.metrics["completed"] == 1)
        assert not trusted(mm)
        controller.schedule()
        qtbot.waitUntil(lambda: controller.metrics["completed"] == 2)
        assert trusted(mm)
    finally:
        release.set()
        stop(controller, qtbot)


@pytest.mark.parametrize("state", ["queued_events", "watcher_stopped", "watcher_restarting"])
def test_undrained_host_events_or_failed_watcher_prevent_trust(qtbot, mm, state):
    host = QObject()
    if state == "queued_events":
        host._watcher_events = ["unprocessed"]
    elif state == "watcher_stopped":
        host._file_watcher = SimpleNamespace(_watch_path="library", is_running=lambda: False)
    else:
        host._watcher_restarts = {"waiting"}
    controller = LibraryReconcileController(mm, busy=lambda: False, parent=host)
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: controller.metrics["completed"] == 1)
        assert not trusted(mm)
    finally:
        stop(controller, qtbot)


def test_complete_review_accepts_trust_after_missing_archive_is_cascaded(qtbot, mm, tmp_path):
    node = mm.add_character("cleanup")
    mod = _import_mod(mm, node.id, tmp_path, "cleanup")
    Path(mod.path).unlink()
    controller = LibraryReconcileController(mm, busy=lambda: False)
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: controller._settled_full_audit is not None)
        assert mm.get_mod(mod.mod_id) is None
        assert not Path(mod.path).parent.exists()
        assert trusted(mm)
    finally:
        stop(controller, qtbot)


def test_reviewed_ui_request_reduces_checks_until_invalidation(qtbot, mm, tmp_path, monkeypatch):
    from src.models.mod import Mod
    from src.ui.main_window import _invalidates_library_probe

    mod = _import_mod(mm, mm.add_character("trust").id, tmp_path, "trusted")
    controller = LibraryReconcileController(mm, busy=lambda: False)
    calls = []
    original = Mod.compute_sha1
    monkeypatch.setattr(Mod, "compute_sha1", staticmethod(lambda path: (calls.append(path), original(path))[1]))

    @_invalidates_library_probe
    def request(window):
        return mm.require_mod_operation(mod.mod_id)

    try:
        controller.schedule()
        qtbot.waitUntil(lambda: controller._settled_full_audit is not None)
        calls.clear()
        assert request(SimpleNamespace(_library_probe=controller))
        assert calls == []
        assert not trusted(mm)
        assert mm.require_mod_operation(mod.mod_id)
        assert len(calls) == 1
    finally:
        stop(controller, qtbot)
