import asyncio
import zipfile
from datetime import datetime

import httpx
import pytest
from PySide6.QtTest import QSignalSpy

from src.core.mod_update_service import ModUpdateService
from src.services.gamebanana.cache import GbApiCache
from src.services.gamebanana.client import GameBananaClient
from tests.core.test_mod_update_game_switch import _update
from tests.core.test_mod_update_game_switch import game_updates as game_updates
from tests.helpers import registered_games as registered_games


def test_cancel_restores_completed_results_without_writing_partial_timestamps(game_updates):
    manager, service, mods, _ = game_updates
    first, second = mods["game-a"]
    previous = _update(second)
    service._status[second.mod_id] = previous
    service._failures[first.mod_id] = "previous failure"
    finished = QSignalSpy(service.check_finished)
    assert service.check_updates(force=True)
    started = service.check_snapshot()
    assert started["checking"] and started["phase"] == "connecting"
    assert started["total"] == 2 and started["done"] == 0
    assert started["started_at"] > 0 and started["finished_at"] == 0

    service._checker.progress.emit(0, 2)
    service._checker.mod_update_found.emit(first.mod_id, _update(first))
    service._checker.progress.emit(1, 2)
    during = service.check_snapshot()
    assert during["phase"] == "checking"
    assert (during["done"], during["found"], during["failed"]) == (1, 1, 0)
    assert during["last_progress_at"] >= started["started_at"]
    assert service.get_update_status(completed_only=True) == {second.mod_id: previous}
    assert first.mod_id in service.get_update_status(), "existing live status consumers keep their contract"
    assert manager.get_mod(first.mod_id).info.custom_data["gamebanana"]["last_checked_iso"] == ""

    assert service.cancel_check()
    assert not service.cancel_check(), "cancellation is terminal and emitted once"
    assert service.get_update_status() == {second.mod_id: previous}
    assert service.failures() == {first.mod_id: "previous failure"}
    stopped = service.check_snapshot()
    assert not stopped["checking"] and stopped["phase"] == "cancelled"
    assert stopped["started_at"] == started["started_at"]
    assert stopped["finished_at"] >= stopped["last_progress_at"]
    assert finished.count() == 1 and finished.at(0)[0]["cancelled"] is True

    service._checker.mod_update_found.emit(first.mod_id, _update(first))
    service._checker.mod_up_to_date.emit(second.mod_id)
    service._checker.mod_check_failed.emit(second.mod_id, "late failure")
    service._checker.progress.emit(2, 2)
    service._checker.finished.emit({"found": 2, "up_to_date": 0, "failed": 0})
    assert service.check_snapshot() == stopped
    assert service.get_update_status() == {second.mod_id: previous}
    assert service.failures() == {first.mod_id: "previous failure"}
    assert finished.count() == 1


def test_success_commits_last_checked_and_keeps_terminal_snapshot(game_updates):
    manager, service, mods, pending = game_updates
    first, second = mods["game-a"]
    service.check_updates(force=True)
    service._checker.mod_up_to_date.emit(first.mod_id)
    service._checker.mod_check_failed.emit(second.mod_id, "connection failed")
    service._checker.progress.emit(2, 2)
    assert manager.get_mod(first.mod_id).info.custom_data["gamebanana"]["last_checked_iso"] == ""
    service._checker.finished.emit({"found": 0, "up_to_date": 1, "failed": 1})
    pending[0].set_result(None)
    snapshot = service.check_snapshot()
    assert snapshot["phase"] == "partial" and not snapshot["checking"]
    assert (snapshot["total"], snapshot["done"], snapshot["failed"]) == (2, 2, 1)
    assert snapshot["finished_at"] >= snapshot["started_at"] > 0
    assert manager.get_mod(first.mod_id).info.custom_data["gamebanana"]["last_checked_iso"]
    assert manager.get_mod(second.mod_id).info.custom_data["gamebanana"]["last_checked_iso"] == ""
    assert service.check_snapshot() == snapshot


def test_switching_games_masks_old_progress_and_cancel_does_not_restore_foreign_results(game_updates):
    manager, service, mods, _ = game_updates
    first = mods["game-a"][0]
    service.check_updates(force=True)
    service._checker.mod_update_found.emit(first.mod_id, _update(first))
    service._checker.progress.emit(1, 2)
    manager.set_game("game-b")
    service.load_status()
    snapshot = service.check_snapshot()
    assert snapshot["checking"] and snapshot["phase"] == "idle"
    assert snapshot["total"] == snapshot["done"] == snapshot["found"] == 0
    assert snapshot["started_at"] == snapshot["last_progress_at"] == snapshot["finished_at"] == 0.0
    finished = QSignalSpy(service.check_finished)
    assert service.get_update_status(completed_only=True) == {}
    assert service.cancel_check()
    assert not service.is_checking()
    assert service.updatable_ids() == set() and service.failures() == {}
    assert finished.count() == 0


@pytest.mark.parametrize("scheduled", [False, True])
def test_empty_retry_finishes_once_without_starting_a_full_check(game_updates, qtbot, scheduled):
    _, service, mods, pending = game_updates
    first = mods["game-a"][0]
    service._status[first.mod_id] = _update(first)
    started = QSignalSpy(service.check_started)
    finished = QSignalSpy(service.check_finished)
    check = service.schedule_check_updates if scheduled else service.check_updates
    assert check(force=True, only_failed=True)
    qtbot.waitUntil(lambda: not service.is_checking())
    assert len(pending) == 0
    assert [started.at(index) for index in range(started.count())] == [[0]]
    assert [finished.at(index) for index in range(finished.count())] == [[{"found": 0, "up_to_date": 0, "failed": 0}]]
    snapshot = service.check_snapshot()
    assert snapshot["phase"] == "done" and not snapshot["checking"]
    assert snapshot["retry_only"] and snapshot["total"] == snapshot["done"] == 0
    assert snapshot["finished_at"] >= snapshot["started_at"] > 0
    assert service.updatable_ids() == {first.mod_id}


def test_cancelling_in_started_handler_does_not_submit_work(game_updates):
    _, service, _, pending = game_updates
    service.check_started.connect(lambda total: service.cancel_check())
    finished = QSignalSpy(service.check_finished)
    assert service.check_updates(force=True)
    assert pending == []
    assert finished.count() == 1 and finished.at(0)[0]["cancelled"]
    assert not service.is_checking()


def test_ignore_during_check_survives_cancel_and_drops_late_results(game_updates):
    manager, service, mods, _ = game_updates
    first, second = mods["game-a"]
    service._status = {mod.mod_id: _update(mod) for mod in (first, second)}
    service.check_updates(force=True)
    assert service.ignore_mod(first.mod_id) is not None
    service._checker.mod_update_found.emit(first.mod_id, _update(first))
    assert first.mod_id not in service.get_update_status(completed_only=True)
    assert service.cancel_check()
    assert service.updatable_ids() == {second.mod_id}
    assert manager.get_mod(first.mod_id).info.custom_data["gamebanana"]["ignored"]


def test_undo_ignore_during_check_survives_cancel(game_updates):
    _, service, mods, _ = game_updates
    first = mods["game-a"][0]
    service._status[first.mod_id] = _update(first)
    token = service.ignore_version(first.mod_id)
    assert token is not None
    service.check_updates(force=True)
    assert service.undo_ignore(token)
    # 检查使用的是先前忽略列表，不能隐藏检查期间刚恢复提醒的版本。
    service._checker.mod_up_to_date.emit(first.mod_id)
    assert first.mod_id in service.get_update_status(completed_only=True)
    assert service.cancel_check()
    assert first.mod_id in service.updatable_ids()


def test_an_update_applied_during_check_is_not_resurrected_by_cancel(game_updates, tmp_path, qtbot):
    manager, service, mods, _ = game_updates
    first = mods["game-a"][0]
    info = _update(first)
    service._status[first.mod_id] = info
    archive = tmp_path / "new-content.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("mod.ini", "[Constants]\nx=42\n")
    service.check_updates(force=True)
    applied, failures = QSignalSpy(service.update_applied), QSignalSpy(service.update_failed)
    service._finish_apply(
        first.mod_id, archive, info.latest_file, first.info.custom_data["gamebanana"], game_id="game-a"
    )
    qtbot.waitUntil(lambda: applied.count() + failures.count() > 0, timeout=10000)
    assert [failures.at(index) for index in range(failures.count())] == []
    assert applied.count() == 1 and applied.at(0)[0] == first.mod_id
    service._checker.mod_update_found.emit(first.mod_id, _update(first))
    assert service.cancel_check()
    assert manager.get_mod(first.mod_id).info.version == "v2"
    assert first.mod_id not in service.updatable_ids()


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("scheduled", [False, True])
async def test_retry_checks_only_existing_failed_local_ids(
    game_updates,
    tmp_path,
    respx_mock,
    qtbot,
    force,
    scheduled,
):
    manager, previous_service, mods, _ = game_updates
    first, second = mods["game-a"]
    # 最近强制检查失败不应覆盖之前成功检查的时间。
    first.info.custom_data["gamebanana"]["last_checked_iso"] = datetime.now().isoformat()
    manager.update_mod(first)
    client = GameBananaClient(request_interval_ms=0, cache=GbApiCache(persist_dir=tmp_path / "cache"))
    tasks = []

    def submit(factory):
        task = asyncio.create_task(factory())
        tasks.append(task)
        return task

    service = ModUpdateService(manager, previous_service._config, client, submit=submit)
    service._failures = {first.mod_id: "network failed", "removed-local-mod": "stale failure"}
    service._status[second.mod_id] = _update(second)
    route = respx_mock.get("https://gamebanana.com/apiv11/Mod/Multi").respond(
        200,
        json=[{"_idRow": 123, "_aFiles": []}],
    )
    finished = QSignalSpy(service.check_finished)
    try:
        check = service.schedule_check_updates if scheduled else service.check_updates
        assert check(force=force, only_failed=True)
        qtbot.waitUntil(lambda: bool(tasks))
        assert service.check_snapshot()["total"] == 1
        await tasks[0]
        assert [finished.at(index) for index in range(finished.count())] == [
            [{"found": 0, "up_to_date": 1, "failed": 0}]
        ]
        assert service.check_snapshot()["done"] == 1
        assert service.failures() == {}
        assert service.updatable_ids() == {second.mod_id}, "the successful copy is outside this retry"
        assert route.call_count == 1
        assert route.calls[0].request.url.params["_csvRowIds"] == "123"
    finally:
        await client.aclose()


async def test_cancel_aborts_a_real_pending_request_and_allows_a_new_check(game_updates, tmp_path, respx_mock):
    manager, previous_service, _, _ = game_updates
    client = GameBananaClient(request_interval_ms=0, cache=GbApiCache(persist_dir=tmp_path / "cache"))
    entered, cancelled = asyncio.Event(), asyncio.Event()
    tasks = []
    requests = 0

    async def respond(request):
        nonlocal requests
        requests += 1
        if requests == 1:
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return httpx.Response(200, json=[{"_idRow": 123, "_aFiles": []}])

    def submit(factory):
        task = asyncio.create_task(factory())
        tasks.append(task)
        return task

    respx_mock.get("https://gamebanana.com/apiv11/Mod/Multi").mock(side_effect=respond)
    service = ModUpdateService(manager, previous_service._config, client, submit=submit)
    finished = QSignalSpy(service.check_finished)
    try:
        assert service.check_updates(force=True)
        await asyncio.wait_for(entered.wait(), 1)
        assert service.cancel_check()
        assert not service.check_snapshot()["checking"]
        assert service.check_updates(force=True), "the cancelled run must not retain the busy flag"
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.gather(*tasks, return_exceptions=True)
        assert finished.count() == 2
        assert finished.at(0)[0] == {"found": 0, "up_to_date": 0, "failed": 0, "cancelled": True}
        assert finished.at(1)[0] == {"found": 0, "up_to_date": 2, "failed": 0}
        assert requests == 2 and service.check_snapshot()["phase"] == "done"
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.aclose()
