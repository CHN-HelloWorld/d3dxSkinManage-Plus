from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QSignalSpy

from tests.core.mod_update_fakes import UpdateDownloadBoundary
from tests.core.test_mod_update_game_switch import _update
from tests.core.test_mod_update_game_switch import game_updates as game_updates
from tests.core.test_mod_update_transaction import _archive
from tests.helpers import registered_games as registered_games


def _finish_check(manager, service, pending, *, found=(), failures=()):
    infos = {mod.mod_id: _update(mod) for mod in found}
    failed_ids = {mod.mod_id for mod in failures}
    assert service.check_updates(force=True)
    for mod in manager.get_mods():
        if mod.mod_id in infos:
            service._checker.mod_update_found.emit(mod.mod_id, infos[mod.mod_id])
        elif mod.mod_id in failed_ids:
            service._checker.mod_check_failed.emit(mod.mod_id, "Connection failed")
        else:
            service._checker.mod_up_to_date.emit(mod.mod_id)
    total = len(manager.get_mods())
    service._checker.progress.emit(total, total)
    service._checker.finished.emit(
        {
            "found": len(infos),
            "failed": len(failed_ids),
            "up_to_date": total - len(infos) - len(failed_ids),
        }
    )
    pending[-1].set_result(None)


def _complete(service, mod, archive, qtbot):
    info = service.get_update_status()[mod.mod_id]
    service._finish_apply(
        mod.mod_id,
        archive,
        info.latest_file,
        mod.info.custom_data["gamebanana"],
        game_id=service._mm.current_game_id,
    )
    qtbot.waitUntil(
        lambda: service.update_task_snapshots()[mod.mod_id]["phase"] in {"done", "failed"},
        timeout=15000,
    )
    assert service.update_task_snapshots()[mod.mod_id]["phase"] == "done"


@pytest.fixture
def completed_update(game_updates, tmp_path, qtbot):
    manager, service, mods, pending = game_updates
    first = mods["game-a"][0]
    original = Path(first.path).read_bytes()
    archive = _archive(tmp_path / "completed-update.zip", b"updated body")
    _finish_check(manager, service, pending, found=[first])
    _complete(service, first, archive, qtbot)
    return manager, service, mods, pending, archive, original


def test_acknowledging_all_successes_resets_snapshot_and_preserves_installed_content(completed_update, qtbot):
    manager, service, mods, pending, archive, _original = completed_update
    first, second = mods["game-a"]
    _finish_check(manager, service, pending, found=[second])
    _complete(service, second, archive, qtbot)
    assert set(service.update_task_snapshots()) == {first.mod_id, second.mod_id}
    assert service.check_snapshot()["phase"] == "done"
    assert service.check_snapshot()["found"] == 1
    metadata = deepcopy(manager.get_mod(first.mod_id).info.custom_data)
    archive_bytes = archive.read_bytes()
    changed, finished = QSignalSpy(service.status_changed), QSignalSpy(service.check_finished)
    observed = []
    service.status_changed.connect(
        lambda: observed.append(
            (
                service.update_task_snapshots(),
                service.update_task_infos(),
                service.check_snapshot()["phase"],
            )
        )
    )

    assert service.acknowledge_completed_updates()

    assert service.update_task_snapshots() == service.update_task_infos() == {}
    assert service.check_snapshot() == {
        "checking": False,
        "phase": "idle",
        "started_at": 0.0,
        "last_progress_at": 0.0,
        "finished_at": 0.0,
        "total": 0,
        "done": 0,
        "found": 0,
        "failed": 0,
        "retry_only": False,
    }
    assert observed == [({}, {}, "idle")]
    assert changed.count() == 1 and finished.count() == 0
    assert service.updatable_ids() == set() and service.failures() == {}
    assert manager.get_mod(first.mod_id).info.custom_data == metadata
    assert archive.read_bytes() == archive_bytes
    assert not service.acknowledge_completed_updates(), "acknowledgement is not a repeatable reset"
    assert changed.count() == 1
    assert manager.get_mod(first.mod_id).info.version == "v2"
    assert Path(manager.get_mod(first.mod_id).path).read_bytes() == archive_bytes


def test_no_completed_tasks_does_not_erase_a_check_result(game_updates):
    manager, service, _, pending = game_updates
    assert not service.acknowledge_completed_updates()
    _finish_check(manager, service, pending)
    before = service.check_snapshot()
    changed = QSignalSpy(service.status_changed)
    assert not service.acknowledge_completed_updates()
    assert service.check_snapshot() == before
    assert changed.count() == 0


@pytest.mark.parametrize("check_game", ["game-a", "game-b"])
def test_a_pending_check_keeps_completed_history(completed_update, check_game):
    manager, service, _, _, _, _ = completed_update
    before = service.update_task_snapshots()
    manager.set_game(check_game)
    service.load_status()
    assert service.check_updates(force=True)
    manager.set_game("game-a")
    service.load_status()
    assert not service.acknowledge_completed_updates()
    assert service.update_task_snapshots() == before
    assert service.is_checking()


def test_failed_check_results_remain_retryable_after_an_update(completed_update):
    manager, service, mods, pending, _, _ = completed_update
    second = mods["game-a"][1]
    _finish_check(manager, service, pending, failures=[second])
    before = service.check_snapshot()
    tasks = service.update_task_snapshots()
    assert not service.acknowledge_completed_updates()
    assert service.failures() == {second.mod_id: "Connection failed"}
    assert service.check_snapshot() == before
    assert service.update_task_snapshots() == tasks


def test_a_new_available_version_is_not_cleared_with_old_successes(completed_update):
    _, service, mods, pending, _, _ = completed_update
    first = mods["game-a"][0]
    info = _update(first)
    newer_file = replace(info.latest_file, file_id=12, version="v3", date_added_ts=3000)
    newer = replace(info, latest_file=newer_file, latest_version="v3", candidate_files=[newer_file])
    assert service.check_updates(force=True)
    service._checker.mod_update_found.emit(first.mod_id, newer)
    service._checker.mod_up_to_date.emit(mods["game-a"][1].mod_id)
    service._checker.finished.emit({"found": 1, "up_to_date": 1, "failed": 0})
    pending[-1].set_result(None)
    tasks = service.update_task_snapshots()
    before = service.check_snapshot()

    assert not service.acknowledge_completed_updates()
    assert service.get_update_status() == {first.mod_id: newer}
    assert service.update_task_snapshots() == tasks
    assert service.check_snapshot() == before


def test_cancelled_prior_check_does_not_block_acknowledging_known_successes(completed_update):
    _, service, _, _, _, _ = completed_update
    assert service.check_updates(force=True)
    assert service.cancel_check()
    assert service.check_snapshot()["phase"] == "cancelled"
    assert service.acknowledge_completed_updates()
    assert service.check_snapshot()["phase"] == "idle"


def test_acknowledging_success_keeps_ignored_version_and_undo(completed_update):
    manager, service, mods, pending, _, _ = completed_update
    second = mods["game-a"][1]
    _finish_check(manager, service, pending, found=[second])
    ignored = service.ignore_version(second.mod_id)
    assert ignored is not None
    before = deepcopy(manager.get_mod(second.mod_id).info.custom_data)
    assert service.acknowledge_completed_updates()
    assert manager.get_mod(second.mod_id).info.custom_data == before
    assert service.undo_ignore(ignored)
    assert service.updatable_ids() == {second.mod_id}


class _PendingTransfer(QObject):
    finished = Signal(object)
    failed = Signal(str)
    state_changed = Signal(str)
    state = "downloading"
    is_running = False

    def wait_finished(self, timeout):
        pass


class _PendingDownloads(UpdateDownloadBoundary):
    def __init__(self):
        self.task = _PendingTransfer()

    def get_task(self, file_id):
        return self.task

    def request(self, profile, file, *, game_id, consumer=None):
        return self.task

    def pause(self, file_id):
        self.task.state = "paused"
        self.task.state_changed.emit("paused")

    def cancel(self, file_id):
        self.task.state = "canceled"
        self.task.state_changed.emit("canceled")


@pytest.mark.parametrize("outcome", ["queued", "downloading", "paused", "failed", "canceled", "deferred"])
def test_unsuccessful_task_keeps_history_even_if_its_version_is_ignored(completed_update, outcome):
    manager, service, mods, pending, archive, _ = completed_update
    second = mods["game-a"][1]
    _finish_check(manager, service, pending, found=[second])
    downloads = _PendingDownloads()
    if outcome == "queued":
        downloads.task.state = "queued"
    service.set_download_manager(downloads)
    service.apply_update(second.mod_id)
    if outcome == "paused":
        assert service.pause_update(second.mod_id)
    elif outcome == "failed":
        downloads.task.failed.emit("Download failed")
    elif outcome == "canceled":
        confirmation = service.begin_stop_confirmation([second.mod_id])
        service.finish_stop_confirmation(confirmation, confirmed=True)
    elif outcome == "deferred":
        manager.set_game("game-b")
        service.load_status()
        downloads.task.finished.emit(archive)
        manager.set_game("game-a")
        service.load_status()
    assert service.update_task_snapshots()[second.mod_id]["phase"] == outcome
    assert service.ignore_version(second.mod_id) is not None
    assert service.updatable_ids() == set()
    before = service.update_task_snapshots()
    snapshot = service.check_snapshot()

    assert not service.acknowledge_completed_updates()
    assert service.update_task_snapshots() == before
    assert service.check_snapshot() == snapshot


@pytest.mark.parametrize("outcome", ["done", "failed", "downloading"])
def test_other_game_history_is_preserved_and_does_not_block_current_acknowledgement(
    completed_update,
    qtbot,
    outcome,
):
    manager, service, mods, pending, archive, _ = completed_update
    manager.set_game("game-b")
    service.load_status()
    other = mods["game-b"][0]
    _finish_check(manager, service, pending, found=[other])
    if outcome == "done":
        _complete(service, other, archive, qtbot)
    else:
        downloads = _PendingDownloads()
        service.set_download_manager(downloads)
        service.apply_update(other.mod_id)
        if outcome == "failed":
            downloads.task.failed.emit("Download failed")
    before = service.update_task_snapshots()
    assert before[other.mod_id]["phase"] == outcome
    manager.set_game("game-a")
    service.load_status()

    assert service.acknowledge_completed_updates()
    assert service.update_task_snapshots() == {}
    manager.set_game("game-b")
    service.load_status()
    assert service.update_task_snapshots() == before
