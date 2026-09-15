import hashlib
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal

from src.core.config import Config
from src.core.mod_update_service import ModUpdateService
from src.services.gamebanana.models import GbModFile
from src.services.gamebanana.update_checker import UpdateInfo
from tests.core.mod_update_fakes import UpdateDownloadBoundary
from tests.core.test_mod_update_transaction import _archive, _metadata
from tests.core.test_mod_update_transaction import update_case as update_case
from tests.helpers import registered_games as registered_games


@pytest.fixture
def update_service(update_case):
    manager, mod, archive = update_case

    class Downloads(UpdateDownloadBoundary):
        def request(self, profile, file, *, game_id, consumer=None):
            return None

        def has_local_md5(self, md5):
            return archive

    config = Config()
    config.set("gamebanana.update_check.concurrency", 3)
    service = ModUpdateService(manager, config, object())
    file = GbModFile(
        file_id=11,
        file_name="update.zip",
        description="",
        download_url="https://example.test/update.zip",
        size_bytes=archive.stat().st_size,
        date_added_ts=2000,
        md5_checksum="",
        av_result="clean",
        version="v2",
    )
    service._status[mod.mod_id] = UpdateInfo(
        local_mod_id=mod.mod_id,
        gb_mod_id=123,
        current_version="v1",
        current_file_ts=1000,
        latest_file=file,
        latest_version="v2",
        candidate_files=[file],
        changelog=[],
        page_url="",
    )
    service.set_download_manager(Downloads())
    yield manager, mod, service
    if hasattr(service, "shutdown_updates"):
        service.shutdown_updates()


def test_pause_cached_update_blocks_commit_and_resume_finishes(update_service, qtbot):
    manager, old, service = update_service
    assert callable(getattr(service, "pause_update", None)), "更新服务缺少暂停接口"
    service.apply_update(old.mod_id)
    assert service.pause_update(old.mod_id)
    assert service.update_task_snapshots()[old.mod_id]["phase"] == "paused"
    qtbot.wait(220)
    assert manager.get_mod(old.mod_id).info.version == "v1"
    assert service.resume_update(old.mod_id)
    qtbot.waitUntil(lambda: service.update_task_snapshots()[old.mod_id]["phase"] in {"done", "failed"}, timeout=15000)
    snapshot = service.update_task_snapshots()[old.mod_id]
    assert snapshot["phase"] == "done", snapshot
    assert manager.get_mod(old.mod_id).info.version == "v2"


def test_dismissing_stop_confirmation_keeps_preexisting_pause(update_service, qtbot):
    manager, old, service = update_service
    assert callable(getattr(service, "begin_stop_confirmation", None)), "更新服务缺少确认暂停接口"
    service.apply_update(old.mod_id)
    service.pause_update(old.mod_id)
    token = service.begin_stop_confirmation([old.mod_id])
    service.finish_stop_confirmation(token, confirmed=False)
    assert service.update_task_snapshots()[old.mod_id]["phase"] == "paused"
    qtbot.wait(150)
    assert manager.get_mod(old.mod_id).info.version == "v1"


def test_confirmation_pauses_then_terminates_without_changing_original(update_service, qtbot):
    manager, old, service = update_service
    assert callable(getattr(service, "begin_stop_confirmation", None)), "更新服务缺少确认暂停接口"
    before = Path(old.path).read_bytes()
    service.apply_update(old.mod_id)
    token = service.begin_stop_confirmation([old.mod_id])
    assert service.update_task_snapshots()[old.mod_id]["phase"] == "paused"
    qtbot.wait(150)
    assert Path(old.path).read_bytes() == before
    service.finish_stop_confirmation(token, confirmed=True)
    qtbot.waitUntil(lambda: service.update_task_snapshots()[old.mod_id]["phase"] == "canceled", timeout=5000)
    assert manager.get_mod(old.mod_id).info.version == "v1"
    assert Path(old.path).read_bytes() == before
    assert old.mod_id in service.updatable_ids()


def test_dismissing_stop_confirmation_resumes_previously_running_update(update_service, qtbot):
    manager, old, service = update_service
    assert callable(getattr(service, "begin_stop_confirmation", None)), "更新服务缺少确认暂停接口"
    service.apply_update(old.mod_id)
    token = service.begin_stop_confirmation([old.mod_id])
    service.finish_stop_confirmation(token, confirmed=False)
    qtbot.waitUntil(lambda: service.update_task_snapshots()[old.mod_id]["phase"] in {"done", "failed"}, timeout=15000)
    assert service.update_task_snapshots()[old.mod_id]["phase"] == "done"
    assert manager.get_mod(old.mod_id).info.version == "v2"


@pytest.fixture
def several_updates(update_service, temp_data_dir):
    manager, old, service = update_service
    mods = [old]
    for index in range(1, 4):
        archive = _archive(temp_data_dir / f"old-{index}.zip", f"body {index}".encode())
        mod = manager.import_mod(str(archive), old.node_id, gb_meta=_metadata("v1", 10))
        assert mod is not None
        info = deepcopy(service._status[old.mod_id])
        info.local_mod_id = mod.mod_id
        service._status[mod.mod_id] = info
        mods.append(mod)
    return manager, mods, service


def test_batch_pause_covers_queue_and_resuming_queued_item_obeys_limit(several_updates, qtbot):
    manager, mods, service = several_updates
    service._config.set("import.parallel_workers", 1)
    for mod in mods:
        service.apply_update(mod.mod_id)
    assert service.pause_all_updates()
    assert {state["phase"] for state in service.update_task_snapshots().values()} == {"paused"}
    service.resume_update(mods[1].mod_id)
    qtbot.wait(150)
    assert service.update_task_snapshots()[mods[1].mod_id]["phase"] == "queued"
    assert all(manager.get_mod(mod.mod_id).info.version == "v1" for mod in mods)
    service.resume_update(mods[0].mod_id)
    qtbot.waitUntil(
        lambda: all(service.update_task_snapshots()[mod.mod_id]["phase"] == "done" for mod in mods[:2]), timeout=15000
    )
    assert all(service.update_task_snapshots()[mod.mod_id]["phase"] == "paused" for mod in mods[2:])
    service.resume_all_updates()
    qtbot.waitUntil(lambda: not service.active_update_ids(), timeout=15000)
    assert all(manager.get_mod(mod.mod_id).info.version == "v2" for mod in mods)


def test_single_pause_and_stop_leave_other_updates_running(several_updates, qtbot):
    manager, mods, service = several_updates
    for mod in mods[:3]:
        service.apply_update(mod.mod_id)
    service.pause_update(mods[0].mod_id)
    qtbot.waitUntil(
        lambda: all(service.update_task_snapshots()[mod.mod_id]["phase"] in {"done", "failed"} for mod in mods[1:3]),
        timeout=15000,
    )
    states = service.update_task_snapshots()
    assert all(states[mod.mod_id]["phase"] == "done" for mod in mods[1:3]), {
        mod.mod_id: (states[mod.mod_id]["phase"], states[mod.mod_id]["error"]) for mod in mods[1:3]
    }
    assert manager.get_mod(mods[0].mod_id).info.version == "v1"
    token = service.begin_stop_confirmation([mods[0].mod_id])
    assert token.count == 1
    service.finish_stop_confirmation(token, confirmed=True)
    qtbot.waitUntil(lambda: service.update_task_snapshots()[mods[0].mod_id]["phase"] == "canceled", timeout=5000)
    assert manager.get_mod(mods[0].mod_id).info.version == "v1"
    assert all(manager.get_mod(mod.mod_id).info.version == "v2" for mod in mods[1:3])


class ControlledTransfer(QObject):
    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)
    state_changed = Signal(str)
    state = "downloading"
    is_running = False

    def set_state(self, state):
        self.state = state
        self.state_changed.emit(state)


class SharedDownloads(UpdateDownloadBoundary):
    def __init__(self):
        self.task = ControlledTransfer()
        self.pauses = 0
        self.cancels = 0

    def get_task(self, file_id):
        return self.task

    def request(self, profile, file, *, game_id, consumer=None):
        return self.task

    def pause(self, file_id):
        self.pauses += 1
        self.task.set_state("paused")

    def resume(self, file_id):
        self.task.set_state("downloading")

    def cancel(self, file_id):
        self.cancels += 1
        self.task.set_state("canceled")


def test_resuming_shared_transfer_keeps_other_explicitly_paused_update(several_updates):
    _, mods, service = several_updates
    downloads = SharedDownloads()
    service.set_download_manager(downloads)
    for mod in mods[:2]:
        service.apply_update(mod.mod_id)
    service.pause_update(mods[0].mod_id)
    assert downloads.pauses == 0, "另一项仍需同一文件时应继续共享传输"
    service.pause_update(mods[1].mod_id)
    assert downloads.pauses == 1
    service.resume_update(mods[0].mod_id)
    snapshots = service.update_task_snapshots()
    assert snapshots[mods[0].mod_id]["phase"] == "downloading"
    assert snapshots[mods[1].mod_id]["phase"] == "paused", "恢复共享传输不能解除另一项的显式暂停"
    token = service.begin_stop_confirmation([mods[1].mod_id])
    service.finish_stop_confirmation(token, confirmed=True)
    assert downloads.cancels == 0, "停止一个更新不能取消另一个仍使用的共享下载"
    assert service.update_task_snapshots()[mods[0].mod_id]["phase"] == "downloading"


def test_shutdown_retains_staging_until_native_process_has_exited(update_service, monkeypatch, qtbot):
    import src.core.mod_update_process as process_module

    class SlowExitProcess(QObject):
        progress = Signal(dict)
        succeeded = Signal(dict)
        failed = Signal(str)
        canceled = Signal()
        running = True

        def __init__(self, plan, parent=None):
            super().__init__(parent)
            self.work_root = Path(plan["work_root"])

        def start(self):
            pass

        def cancel(self):
            pass

        def shutdown(self):
            pass

    monkeypatch.setattr(process_module, "UpdateProcess", SlowExitProcess)
    _, mod, service = update_service
    service.apply_update(mod.mod_id)
    process = service._update_tasks.findChild(SlowExitProcess)
    assert service.shutdown_updates() is False
    qtbot.wait(60)
    assert process.work_root.is_dir(), "原生进程仍运行时不能清理它的暂存路径"
    assert service.update_task_snapshots()[mod.mod_id]["phase"] == "stopping"
    process.running = False
    process.canceled.emit()
    assert service.shutdown_updates() is True
    assert not process.work_root.exists()


def test_different_file_ids_sharing_cache_keep_it_until_all_updates_finish(several_updates, qtbot):
    from src.services.gamebanana.downloader import GbDownloadManager
    from src.utils.paths import get_gb_download_dir

    manager, mods, service = several_updates
    contents = service._download_manager.has_local_md5("").read_bytes()
    checksum = hashlib.md5(contents).hexdigest()
    archive = get_gb_download_dir() / f"{checksum}__update.zip"
    archive.write_bytes(contents)
    downloads = GbDownloadManager(get_gb_download_dir())
    service.set_download_manager(downloads)
    try:
        for index, mod in enumerate(mods[:2]):
            info = service._status[mod.mod_id]
            info.latest_file = replace(info.latest_file, file_id=11 + index, md5_checksum=checksum)
            service.apply_update(mod.mod_id)
        service.pause_update(mods[1].mod_id)
        qtbot.waitUntil(
            lambda: service.update_task_snapshots()[mods[0].mod_id]["phase"] in {"done", "failed"}, timeout=15000
        )
        assert service.update_task_snapshots()[mods[0].mod_id]["phase"] == "done"
        assert archive.is_file(), "另一项仍使用相同缓存时，不能因 file_id 不同就删除文件"
        service.resume_update(mods[1].mod_id)
        qtbot.waitUntil(lambda: not service.active_update_ids(), timeout=15000)
        assert all(manager.get_mod(mod.mod_id).info.version == "v2" for mod in mods[:2])
        assert not archive.exists()
    finally:
        service.shutdown_updates()
        downloads.shutdown()


def test_batch_controls_are_current_game_scoped_and_exit_hold_covers_every_game(update_service, temp_data_dir, qtbot):
    manager, first, service = update_service
    template = deepcopy(service._status[first.mod_id])
    service.apply_update(first.mod_id)
    service.pause_update(first.mod_id)
    manager.set_game("game-b")
    service.load_status()
    character = manager.add_character(name="Character", node_type="character")
    old = _archive(temp_data_dir / "game-b-old.zip", b"body in another game")
    second = manager.import_mod(str(old), character.id, gb_meta=_metadata("v1", 10))
    assert second is not None
    template.local_mod_id = second.mod_id
    service._status[second.mod_id] = template
    service.apply_update(second.mod_id)
    service.pause_all_updates()
    assert service.active_update_ids() == [second.mod_id]
    assert set(service.active_update_ids(all_games=True)) == {first.mod_id, second.mod_id}
    visible = service.begin_stop_confirmation()
    assert visible.count == 1
    service.finish_stop_confirmation(visible, confirmed=False)
    assert service.update_task_snapshots()[second.mod_id]["phase"] == "paused"
    exiting = service.begin_stop_confirmation(all_games=True)
    assert exiting.count == 2
    service.finish_stop_confirmation(exiting, confirmed=True)
    qtbot.waitUntil(lambda: not service.active_update_ids(all_games=True), timeout=5000)
    assert service.update_task_snapshots()[second.mod_id]["phase"] == "canceled"
    assert manager.get_mod(second.mod_id).info.version == "v1"
    manager.set_game("game-a")
    service.load_status()
    assert service.update_task_snapshots()[first.mod_id]["phase"] == "canceled"
    assert manager.get_mod(first.mod_id).info.version == "v1"


def test_pause_during_process_creation_keeps_worker_unstarted_until_resume(update_service, qtbot):
    from src.core.mod_update_process import UpdateProcess

    manager, mod, service = update_service
    held = []

    def pause_when_created(mid, snapshot):
        process = service._update_tasks.findChild(UpdateProcess)
        if snapshot["phase"] == "verifying" and process is not None and not held:
            held.append(process)
            assert service.pause_update(mid)

    service.update_task_changed.connect(pause_when_created)
    service.apply_update(mod.mod_id)
    assert len(held) == 1
    assert held[0].pid is None, "创建期间的暂停必须阻止原生工作启动"
    assert service.update_task_snapshots()[mod.mod_id]["phase"] == "paused"
    assert manager.get_mod(mod.mod_id).info.version == "v1"
    service.resume_update(mod.mod_id)
    qtbot.waitUntil(lambda: not service.active_update_ids(), timeout=15000)
    assert service.update_task_snapshots()[mod.mod_id]["phase"] == "done"


def test_resuming_one_shared_update_does_not_auto_resume_later_external_pause(several_updates, qtbot):
    _, mods, service = several_updates
    downloads = SharedDownloads()
    downloads.task.is_running = True
    service.set_download_manager(downloads)
    for mod in mods[:2]:
        service.apply_update(mod.mod_id)
    service.pause_update(mods[0].mod_id)
    service.resume_update(mods[0].mod_id)
    downloads.task.set_state("paused")
    downloads.task.is_running = False
    qtbot.wait(40)
    assert downloads.task.state == "paused", "正常共享传输不应残留持续自动恢复的轮询"
    assert {state["phase"] for state in service.update_task_snapshots().values()} == {"paused"}
