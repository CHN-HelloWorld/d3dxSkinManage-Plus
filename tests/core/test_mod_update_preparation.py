import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtTest import QSignalSpy

from src.core import mod_index, mod_update_service
from tests.core.test_mod_update_game_switch import _update
from tests.core.test_mod_update_game_switch import game_updates as game_updates
from tests.helpers import registered_games as registered_games


def _clocked_metadata(monkeypatch, manager, after_first):
    """为真实游标每行读取记录四毫秒开销，不延长测试实际运行时间。"""
    elapsed = [0.0]
    consumed, closed = [], []
    original = manager._index.iter_mod_info

    def entries(*args, **kwargs):
        try:
            for mid, info in original(*args, **kwargs):
                consumed.append(mid)
                elapsed[0] += 0.004
                if len(consumed) == 1:
                    QTimer.singleShot(0, after_first)
                yield mid, info
        finally:
            closed.append(True)

    monkeypatch.setattr(manager._index, "iter_mod_info", entries)
    monkeypatch.setattr(
        mod_update_service,
        "time",
        SimpleNamespace(
            monotonic=time.monotonic,
            perf_counter=lambda: elapsed[0],
        ),
    )
    return consumed, closed


@pytest.mark.parametrize("ignored", [False, True])
def test_schedule_reserves_the_run_and_yields_between_metadata_rows(
    game_updates,
    monkeypatch,
    qtbot,
    ignored,
):
    manager, service, mods, pending = game_updates
    if ignored:
        for mod in mods["game-a"]:
            service.ignore_mod(mod.mod_id)
    observed = []
    consumed, closed = _clocked_metadata(
        monkeypatch,
        manager,
        lambda: observed.append((len(consumed), len(pending), service.check_snapshot())),
    )
    started, finished = QSignalSpy(service.check_started), QSignalSpy(service.check_finished)
    changes = QSignalSpy(service.status_changed)

    assert service.schedule_check_updates(force=True)
    snapshot = service.check_snapshot()
    assert snapshot["checking"] and snapshot["phase"] == "preparing"
    assert snapshot["started_at"] > 0 and snapshot["finished_at"] == 0
    assert consumed == pending == [] and started.count() == 0
    assert changes.count() == 1, "a window can show busy before any metadata is read"
    assert not service.schedule_check_updates(force=False)
    assert not service.check_updates(force=True)

    qtbot.waitUntil(lambda: bool(observed))
    count, submitted, during = observed[0]
    assert count == 1 and submitted == 0, "even filtered rows must yield to the event loop"
    assert during["checking"] and during["phase"] == "preparing"
    qtbot.waitUntil(lambda: bool(pending) or finished.count() > 0)
    assert len(consumed) == 2 and closed == [True]
    assert [started.at(index) for index in range(started.count())] == [[0 if ignored else 2]]
    assert service.check_snapshot()["started_at"] == snapshot["started_at"]
    assert service.check_snapshot()["phase"] == ("done" if ignored else "connecting")


def test_cancelled_preparation_closes_its_cursor_and_cannot_start_over_a_new_run(
    game_updates,
    monkeypatch,
    qtbot,
):
    manager, service, mods, pending = game_updates
    first, second = mods["game-a"]
    previous = _update(first)
    service._status[first.mod_id] = previous
    service._failures[second.mod_id] = "previous failure"
    cancellations = []

    def cancel_and_restart():
        assert service.cancel_check()
        cancellations.append(service.check_snapshot())
        assert service.get_update_status() == {first.mod_id: previous}
        assert service.failures() == {second.mod_id: "previous failure"}
        assert service.schedule_check_updates(force=False, only_failed=True)

    consumed, closed = _clocked_metadata(monkeypatch, manager, cancel_and_restart)
    finished = QSignalSpy(service.check_finished)
    assert service.schedule_check_updates(force=True)
    qtbot.waitUntil(lambda: bool(pending))
    assert len(pending) == 1, "the old scheduled callback cannot submit a second check"
    assert len(consumed) == 3 and closed == [True, True]
    assert set(service._check_jobs) == {second.mod_id}
    assert cancellations[0]["phase"] == "cancelled"
    assert finished.count() == 1 and finished.at(0)[0]["cancelled"]
    assert manager.get_mod(first.mod_id).info.custom_data["gamebanana"]["last_checked_iso"] == ""


def test_switching_games_during_preparation_cannot_start_old_candidates(game_updates, qtbot):
    manager, service, mods, pending = game_updates
    first = mods["game-a"][0]
    service._status[first.mod_id] = _update(first)
    service._save_status()
    finished = QSignalSpy(service.check_finished)
    assert service.schedule_check_updates(force=True)
    manager.set_game("game-b")
    service.load_status()
    assert service.schedule_check_updates(force=True)
    qtbot.waitUntil(lambda: bool(pending))
    assert len(pending) == 1 and finished.count() == 0
    assert set(service._check_jobs) == {mod.mod_id for mod in mods["game-b"]}
    assert service.updatable_ids() == set()
    assert service.cancel_check()
    manager.set_game("game-a")
    service.load_status()
    assert service.updatable_ids() == {first.mod_id}


def test_game_change_in_started_handler_does_not_submit_prepared_jobs(game_updates, qtbot):
    manager, service, _, pending = game_updates
    service.check_started.connect(lambda total: manager.set_game("game-b"))
    assert service.schedule_check_updates(force=True)
    qtbot.waitUntil(lambda: not service.is_checking())
    assert pending == []
    assert service.check_snapshot()["phase"] == "idle"


def test_ignoring_an_already_prepared_mod_preserves_the_change_on_cancel(
    game_updates,
    monkeypatch,
    qtbot,
):
    manager, service, mods, pending = game_updates
    service._status = {mod.mod_id: _update(mod) for mod in mods["game-a"]}
    ignored = []

    def ignore_first():
        ignored.append(consumed[0])
        assert service.ignore_mod(consumed[0]) is not None

    consumed, _ = _clocked_metadata(monkeypatch, manager, ignore_first)
    assert service.schedule_check_updates(force=True)
    qtbot.waitUntil(lambda: bool(pending))
    remaining = {mod.mod_id for mod in mods["game-a"]} - set(ignored)
    assert set(service._check_jobs) == remaining
    assert service.cancel_check()
    assert service.updatable_ids() == remaining
    assert manager.get_mod(ignored[0]).info.custom_data["gamebanana"]["ignored"]


def test_preparation_error_restores_results_and_finishes_with_an_error(game_updates, qtbot, monkeypatch):
    manager, service, mods, pending = game_updates
    first, second = mods["game-a"]
    previous = _update(first)
    service._status[first.mod_id] = previous
    service._failures[second.mod_id] = "previous failure"
    original = manager._index.iter_mod_info

    def broken_metadata(*args, **kwargs):
        yield next(original(*args, **kwargs))
        raise ValueError("broken local metadata")

    monkeypatch.setattr(manager._index, "iter_mod_info", broken_metadata)
    finished = QSignalSpy(service.check_finished)
    assert service.schedule_check_updates(force=True)
    qtbot.waitUntil(lambda: not service.is_checking())
    assert pending == []
    assert finished.count() == 1 and finished.at(0)[0]["error"] == "broken local metadata"
    snapshot = service.check_snapshot()
    assert snapshot["phase"] == "failed" and snapshot["error"] == "broken local metadata"
    assert snapshot["finished_at"] >= snapshot["started_at"] > 0
    assert service.get_update_status() == {first.mod_id: previous}
    assert service.failures() == {second.mod_id: "previous failure"}
    assert manager.get_mod(first.mod_id).info.custom_data["gamebanana"]["last_checked_iso"] == ""
    monkeypatch.setattr(manager._index, "iter_mod_info", original)
    assert service.schedule_check_updates(force=True)
    qtbot.waitUntil(lambda: bool(pending))


def test_cancellation_from_preparing_notification_does_not_start_a_timer_run(game_updates, qtbot):
    _, service, _, pending = game_updates
    service.status_changed.connect(
        lambda: service.cancel_check() if service.check_snapshot()["phase"] == "preparing" else None,
    )
    finished = QSignalSpy(service.check_finished)
    assert service.schedule_check_updates(force=True)
    assert not service.is_checking()
    qtbot.wait(1)
    assert pending == [] and finished.count() == 1


def test_unsorted_metadata_iteration_is_lazy_and_default_keeps_name_order(game_updates, monkeypatch):
    manager, _, mods, _ = game_updates
    first, second = mods["game-a"]
    conn = manager._index._db()
    conn.execute("UPDATE mods SET name=? WHERE mod_id=?", ("z", first.mod_id))
    conn.execute("UPDATE mods SET name=? WHERE mod_id=?", ("a", second.mod_id))
    conn.commit()
    decoded = []
    original_loads = mod_index.json.loads

    def loads(value, *args, **kwargs):
        decoded.append(value)
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(mod_index.json, "loads", loads)
    rows = manager._index.iter_mod_info("game-a", ordered=False)
    assert decoded == []
    assert next(rows)[0] in {first.mod_id, second.mod_id}
    assert len(decoded) == 1
    rows.close()
    assert [mid for mid, _ in manager._index.iter_mod_info("game-a")] == [second.mod_id, first.mod_id]
