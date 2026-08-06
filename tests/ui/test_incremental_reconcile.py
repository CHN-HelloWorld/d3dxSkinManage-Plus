from pathlib import Path
from threading import Event

import pytest

from tests.core.test_library_reconcile import mm as mm


def _import_mod(manager, node_id, tmp_path, name):
    source = tmp_path / f"src_{name}"
    source.mkdir()
    (source / "mod.ini").write_text(f"[Constants]\nx={name}\n", encoding="utf-8")
    mod = manager.import_mod(source_path=str(source), node_id=node_id, name=name)
    assert mod is not None
    return mod


def test_event_preempts_complete_audit_and_only_publishes_real_difference(qtbot, mm, tmp_path, monkeypatch):
    from dataclasses import replace

    from src.core.library_snapshot import ProbeToken, probe_library
    from src.ui import library_reconcile_worker as module

    char = mm.add_character(name="A", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "source")
    initial = probe_library(replace(mm.create_library_probe_request(ProbeToken("game_a", 0, 0)), mode="full"))
    entered, release = Event(), Event()
    real = module.probe_library
    requests, published = [], []

    def blocked(request, *, should_cancel):
        requests.append(request)
        if len(requests) == 1:
            entered.set()
            assert release.wait(5)
        return real(request, should_cancel=should_cancel)

    monkeypatch.setattr(module, "probe_library", blocked)
    controller = module.LibraryReconcileController(mm, busy=lambda: False)
    controller.source_inventory = initial.source_inventory
    controller.changed.connect(lambda *args: published.append((args, controller.changed_ids)))
    old = Path(mod.path).parent
    new = old.with_name("Renamed")
    try:
        controller.schedule()
        qtbot.waitUntil(entered.is_set)
        old.rename(new)
        controller.schedule_changes((str(old), str(new)))
        assert controller.worker.isInterruptionRequested()
        release.set()
        qtbot.waitUntil(lambda: mm.get_mod(mod.mod_id).path == str(new / Path(mod.path).name))
        qtbot.waitUntil(lambda: controller.worker is None and not controller.pending and not controller.applying)
        assert requests[1].mode == "source"
        assert published == [(("game_a", 0), (mod.mod_id,))]
        assert controller.changed_ids is None
        assert controller.metrics["max_active"] == 1
    finally:
        release.set()
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


def test_missing_watcher_notification_still_converges_on_full_probe(qtbot, mm, tmp_path):
    from src.ui.library_reconcile_worker import LibraryReconcileController

    char = mm.add_character(name="A", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "source")
    controller = LibraryReconcileController(mm, busy=lambda: False)
    try:
        controller.schedule()
        qtbot.waitUntil(lambda: controller.worker is None and not controller.applying)
        old = Path(mod.path).parent
        new = old.with_name("Unreported")
        old.rename(new)
        controller.schedule()
        qtbot.waitUntil(lambda: mm.get_mod(mod.mod_id).path == str(new / Path(mod.path).name))
    finally:
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


@pytest.mark.parametrize("force_audit", [False, True])
def test_observed_missing_path_precedes_identity_check_but_not_commit(qtbot, mm, tmp_path, monkeypatch, force_audit):
    from dataclasses import replace

    from src.core import library_inventory
    from src.core.library_snapshot import ProbeToken, probe_library
    from src.ui.library_reconcile_worker import LibraryReconcileController

    char = mm.add_character(name="A", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "source")
    initial = probe_library(replace(mm.create_library_probe_request(ProbeToken("game_a", 0, 0)), mode="full"))
    entered, release = Event(), Event()
    original = library_inventory.scan_source_inventory

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(library_inventory, "scan_source_inventory", blocked)
    busy = [force_audit]
    controller = LibraryReconcileController(mm, busy=lambda: busy[0])
    controller.source_inventory = initial.source_inventory
    observed = []
    controller.source_observed.connect(observed.append)
    old = Path(mod.path).parent
    new = old.with_name("Renamed")
    old.rename(new)
    try:
        controller.schedule_changes((str(old), str(new)))
        if force_audit:
            controller.schedule(force_source_audit=True)
            busy[0] = False
            controller.schedule()
        qtbot.waitUntil(lambda: entered.is_set() and bool(observed))
        assert observed[0][1][0].state == "missing"
        assert observed[0][1][0].mod_id == mod.mod_id
        assert mm.get_mod(mod.mod_id).path == mod.path
        release.set()
        qtbot.waitUntil(lambda: mm.get_mod(mod.mod_id).path != mod.path)
        qtbot.waitUntil(lambda: observed[-1] == ("game_a", ()))
    finally:
        release.set()
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


def test_cancelled_merged_full_retains_all_source_paths_and_events(qtbot, mm, monkeypatch):
    from src.ui import library_reconcile_worker as module

    entered, release = Event(), Event()
    busy = [True]
    requests = []
    original = module.probe_library
    first = str(mm._get_library_root("game_a") / "first")
    second = str(mm._get_library_root("game_a") / "second")

    def blocked(request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            entered.set()
            assert release.wait(5)
        return original(request, **kwargs)

    monkeypatch.setattr(module, "probe_library", blocked)
    controller = module.LibraryReconcileController(mm, busy=lambda: busy[0])
    try:
        controller.schedule_changes((first,), events=("first-event",))
        controller.schedule(force_source_audit=True)
        busy[0] = False
        controller.schedule()
        qtbot.waitUntil(entered.is_set)
        controller.schedule_changes((second,), events=("second-event",))
        release.set()
        qtbot.waitUntil(lambda: controller.worker is None and not controller.pending and not controller.applying)
        assert [r.mode for r in requests] == ["full", "full"]
        assert requests[-1].force_source_audit
        assert set(requests[-1].changed_paths) == {first, second}
        assert set(requests[-1].events) == {"first-event", "second-event"}
    finally:
        release.set()
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


@pytest.mark.parametrize("event_first", [False, True])
def test_explicit_audit_merges_source_event_in_one_complete_probe(qtbot, mm, tmp_path, monkeypatch, event_first):
    from src.ui import library_reconcile_worker as module

    entered, release = Event(), Event()
    requests, settled, results = [], [], []
    char = mm.add_character(name="Audit", node_type="character")
    affected = _import_mod(mm, char.id, tmp_path, "affected")
    unrelated = _import_mod(mm, char.id, tmp_path, "unrelated")
    event = ("source-event", affected.path)
    original = module.probe_library

    def blocked(request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            entered.set()
            assert release.wait(5)
        result = original(request, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(module, "probe_library", blocked)
    controller = module.LibraryReconcileController(mm, busy=lambda: False)
    controller.settled.connect(lambda: settled.append((controller.last_mode, controller.last_force_source_audit)))
    try:
        if event_first:
            controller.schedule_changes((affected.path,), events=(event,))
        else:
            controller.schedule(force_source_audit=True)
        qtbot.waitUntil(entered.is_set)
        if event_first:
            controller.schedule(force_source_audit=True)
        else:
            controller.schedule_changes((affected.path,), events=(event,))
        assert controller.worker.isInterruptionRequested()
        release.set()
        qtbot.waitUntil(lambda: not controller.pending and controller.worker is None and not controller.applying)
        assert [(r.mode, r.force_source_audit) for r in requests] == [
            ("source", False) if event_first else ("full", True),
            ("full", True),
        ]
        assert requests[-1].changed_paths == (affected.path,)
        assert requests[-1].events == (event,)
        assert {check.path for check in results[-1].checks if check.state == "exists"} == {
            affected.path,
            unrelated.path,
        }
        assert settled == [("full", True)]
    finally:
        release.set()
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)


def test_explicit_audit_preempts_weak_cleanup_and_ordinary_poll_stays_cached(qtbot, mm, monkeypatch):
    from src.ui import library_reconcile_worker as module

    entered, release = Event(), Event()
    requests, accepted = [], []
    original = module.probe_library

    def blocked(request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            entered.set()
            assert release.wait(5)
        return original(request, **kwargs)

    monkeypatch.setattr(module, "probe_library", blocked)
    controller = module.LibraryReconcileController(mm, busy=lambda: False)
    controller.settled.connect(lambda: accepted.append(controller.last_force_source_audit))
    try:
        controller.schedule()
        qtbot.waitUntil(entered.is_set)
        assert not requests[0].force_source_audit
        controller.schedule(force_source_audit=True)
        assert controller.worker.isInterruptionRequested()
        release.set()
        qtbot.waitUntil(lambda: not controller.pending and controller.worker is None and not controller.applying)
        assert [r.force_source_audit for r in requests] == [False, True]
        assert accepted == [True]
    finally:
        release.set()
        controller.stop()
        qtbot.waitUntil(lambda: controller.worker is None)
