from __future__ import annotations

import hashlib
import shutil
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal

from src.services.gamebanana.downloader import GbDownloadTask
from tests.core.mod_update_fakes import UpdateDownloadBoundary
from tests.core.test_mod_update_controls import several_updates as several_updates
from tests.core.test_mod_update_controls import update_service as update_service
from tests.core.test_mod_update_transaction import update_case as update_case
from tests.helpers import registered_games as registered_games


@pytest.fixture
def controlled_pipeline(several_updates, temp_data_dir, monkeypatch):
    """保留真实规划和提交，手动控制 HTTP 及后台任务继续执行。"""
    manager, mods, service = several_updates
    source_archive = service._download_manager.has_local_md5("")
    processes = {}

    class ControlledProcess(QObject):
        progress = Signal(dict)
        succeeded = Signal(dict)
        failed = Signal(str)
        canceled = Signal()

        def __init__(self, plan, parent=None):
            super().__init__(parent)
            self.plan = plan
            self.running = False
            self.paused = False
            processes[plan["mod_id"]] = self

        def start(self):
            self.running = True
            self.progress.emit({"phase": "extracting", "stage_progress": 0.1})

        def pause(self):
            self.paused = True
            return True

        def resume(self):
            self.paused = False

        def cancel(self):
            self.running = False
            self.canceled.emit()

        def shutdown(self):
            if self.running:
                self.cancel()

        def finish(self):
            from src.core.mod_update_worker import prepare_update

            prepared = prepare_update(self.plan, lambda _message: None)
            self.running = False
            self.succeeded.emit(prepared)

    class Downloads(QObject, UpdateDownloadBoundary):
        queue_changed = Signal()

        def __init__(self):
            super().__init__()
            self.tasks = {}
            self.cached = False

        def request(self, profile, file, *, game_id, consumer=None):
            if self.cached:
                return None
            task = GbDownloadTask(profile, file, temp_data_dir / f"download-{file.file_id}.zip", game_id=game_id)
            task._set_state("downloading")
            self.tasks[file.file_id] = task
            return task

        def has_local_md5(self, _md5):
            return source_archive if self.cached else None

        def get_task(self, file_id):
            return self.tasks.get(file_id)

        def pause(self, file_id):
            self.tasks[file_id].pause()

        def resume(self, file_id):
            self.tasks[file_id]._set_state("downloading")

        def cancel(self, file_id):
            self.tasks[file_id].cancel()

        def finish(self, index):
            file = service._status[mods[index].mod_id].latest_file
            task = self.tasks[file.file_id]
            shutil.copyfile(source_archive, task.archive_path)
            task._set_state("done")
            task.finished.emit(task.archive_path)
            self.queue_changed.emit()

    downloads = Downloads()
    monkeypatch.setattr("src.core.mod_update_process.UpdateProcess", ControlledProcess)
    service.set_download_manager(downloads)
    for index, mod in enumerate(mods):
        info = service._status[mod.mod_id]
        info.latest_file = replace(info.latest_file, file_id=11 + index)
        info.candidate_files = [info.latest_file]

    def start():
        for mod in mods:
            service.apply_update(mod.mod_id)

    def phases():
        snapshots = service.update_task_snapshots()
        return [snapshots[mod.mod_id]["phase"] for mod in mods]

    yield SimpleNamespace(
        config=service._config,
        manager=manager,
        mods=mods,
        service=service,
        downloads=downloads,
        processes=processes,
        start=start,
        phases=phases,
    )
    service.shutdown_updates()


def test_finishing_download_releases_network_slot_while_import_is_running(controlled_pipeline):
    case = controlled_pipeline
    case.config.set("gamebanana.update_check.concurrency", 1)
    case.config.set("import.parallel_workers", 1)
    case.start()

    case.downloads.finish(0)
    assert case.phases() == ["extracting", "downloading", "queued", "queued"]
    case.downloads.finish(1)
    assert case.phases() == ["extracting", "queued", "downloading", "queued"]
    waiting = case.service.update_task_snapshots()[case.mods[1].mod_id]
    assert waiting["downloaded_bytes"] == waiting["total_bytes"] > 0
    assert waiting["detail"] == "等待导入"

    case.processes[case.mods[0].mod_id].finish()
    assert case.phases() == ["done", "extracting", "downloading", "queued"]
    assert case.manager.get_mod(case.mods[0].mod_id).info.version == "v2"


def test_imports_can_exceed_download_limit_but_obey_import_workers(controlled_pipeline):
    case = controlled_pipeline
    case.config.set("gamebanana.update_check.concurrency", 1)
    case.config.set("import.parallel_workers", 3)
    case.start()

    for index in range(3):
        assert case.phases()[index] == "downloading"
        case.downloads.finish(index)
    assert case.phases() == ["extracting", "extracting", "extracting", "downloading"]
    case.downloads.finish(3)
    assert case.phases() == ["extracting", "extracting", "extracting", "queued"]


def test_cached_downloads_still_obey_import_limit(controlled_pipeline):
    case = controlled_pipeline
    case.downloads.cached = True
    case.config.set("gamebanana.update_check.concurrency", 4)
    case.config.set("import.parallel_workers", 1)
    case.start()

    assert case.phases() == ["extracting", "queued", "queued", "queued"]
    case.processes[case.mods[0].mod_id].finish()
    assert case.phases() == ["done", "extracting", "queued", "queued"]


@pytest.mark.parametrize("control", ["pause", "confirmation"])
def test_resuming_waiting_import_does_not_bypass_import_limit(controlled_pipeline, control):
    case = controlled_pipeline
    case.config.set("gamebanana.update_check.concurrency", 4)
    case.config.set("import.parallel_workers", 1)
    case.start()
    case.downloads.finish(0)
    case.downloads.finish(1)
    waiting_id = case.mods[1].mod_id

    if control == "pause":
        case.service.pause_update(waiting_id)
        case.service.resume_update(waiting_id)
    else:
        token = case.service.begin_stop_confirmation([waiting_id])
        case.service.finish_stop_confirmation(token, confirmed=False)
    assert case.phases() == ["extracting", "queued", "downloading", "downloading"]
    token = case.service.begin_stop_confirmation([waiting_id])
    case.service.finish_stop_confirmation(token, confirmed=True)
    case.processes[case.mods[0].mod_id].finish()
    assert case.phases() == ["done", "canceled", "downloading", "downloading"]
    assert case.manager.get_mod(waiting_id).info.version == "v1"


def test_completed_download_while_paused_frees_network_slot_without_starting_import(controlled_pipeline):
    case = controlled_pipeline
    case.config.set("gamebanana.update_check.concurrency", 1)
    case.config.set("import.parallel_workers", 1)
    case.start()
    first_id = case.mods[0].mod_id
    case.service.pause_update(first_id)
    # 共享传输可能在更新暂停后发出完成信号。
    case.downloads.finish(0)
    assert case.phases() == ["paused", "downloading", "queued", "queued"]
    case.downloads.finish(1)
    case.service.resume_update(first_id)
    assert case.phases() == ["queued", "extracting", "downloading", "queued"]


@pytest.mark.parametrize("cache_source", ["checksum", "completed_task"])
def test_cached_archive_can_enter_import_while_network_slots_are_full(controlled_pipeline, cache_source):
    case = controlled_pipeline
    case.config.set("gamebanana.update_check.concurrency", 1)
    case.config.set("import.parallel_workers", 1)
    info = case.service._status[case.mods[1].mod_id]
    if cache_source == "checksum":
        case.downloads.cached = True
        checksum = hashlib.md5(case.downloads.has_local_md5("").read_bytes()).hexdigest()
        case.downloads.cached = False
        info.latest_file = replace(info.latest_file, md5_checksum=checksum)
        info.candidate_files = [info.latest_file]
    case.start()
    if cache_source == "checksum":
        case.downloads.cached = True
        case.downloads.queue_changed.emit()
    else:
        case.downloads.request(
            SimpleNamespace(mod_id=123, name="Store cache"),
            info.latest_file,
            game_id=case.manager.current_game_id,
        )
        case.downloads.finish(1)

    assert case.phases() == ["downloading", "extracting", "queued", "queued"]


def test_replacing_downloader_disconnects_previous_cache_notifications(controlled_pipeline):
    case = controlled_pipeline
    case.config.set("gamebanana.update_check.concurrency", 1)
    case.config.set("import.parallel_workers", 1)
    case.start()
    previous = case.downloads
    replacement = type(previous)()
    replacement.tasks = previous.tasks
    case.service.set_download_manager(replacement)
    case.service.set_download_manager(replacement)

    info = case.service._status[case.mods[1].mod_id]
    replacement.request(
        SimpleNamespace(mod_id=123, name="Store cache"),
        info.latest_file,
        game_id=case.manager.current_game_id,
    )
    task = replacement.tasks[info.latest_file.file_id]
    previous.cached = True
    shutil.copyfile(previous.has_local_md5(""), task.archive_path)
    task._set_state("done")
    previous.queue_changed.emit()
    assert case.phases() == ["downloading", "queued", "queued", "queued"]

    replacement.queue_changed.emit()
    assert case.phases() == ["downloading", "extracting", "queued", "queued"]


def test_update_enqueued_during_download_notification_uses_available_slot(controlled_pipeline):
    case = controlled_pipeline
    case.config.set("gamebanana.update_check.concurrency", 2)

    def enqueue_next(mid, snapshot):
        if mid == case.mods[0].mod_id and snapshot["phase"] == "downloading":
            case.service.apply_update(case.mods[1].mod_id)

    case.service.update_task_changed.connect(enqueue_next)
    case.service.apply_update(case.mods[0].mod_id)
    snapshots = case.service.update_task_snapshots()
    assert [snapshots[mod.mod_id]["phase"] for mod in case.mods[:2]] == ["downloading", "downloading"]


@pytest.mark.parametrize("import_fails", [False, True])
def test_cache_notification_during_retention_preserves_import_state(controlled_pipeline, monkeypatch, import_fails):
    from src.core import mod_update_tasks

    case = controlled_pipeline
    case.config.set("gamebanana.update_check.concurrency", 1)
    case.config.set("import.parallel_workers", 1)
    case.start()
    make_plan = mod_update_tasks.make_update_plan

    def plan(manager, mid, *args, **kwargs):
        if import_fails and mid == case.mods[0].mod_id:
            raise OSError("Update target is unavailable")
        return make_plan(manager, mid, *args, **kwargs)

    def retain(file_id, md5, *, owner, path=None):
        # 保留操作中清理未使用文件时，真实管理器可能发出该信号。
        if path is not None:
            case.downloads.queue_changed.emit()

    monkeypatch.setattr(mod_update_tasks, "make_update_plan", plan)
    monkeypatch.setattr(case.downloads, "retain", retain)
    case.downloads.finish(0)
    assert case.phases() == ["failed" if import_fails else "extracting", "downloading", "queued", "queued"]
    if import_fails:
        assert case.mods[0].mod_id not in case.service.active_update_ids()
        assert case.manager.get_mod(case.mods[0].mod_id).info.version == "v1"
        case.downloads.finish(1)
        assert case.phases() == ["failed", "extracting", "downloading", "queued"]
