import pytest

from src.services.gamebanana.models import GbModFile
from src.services.gamebanana.update_checker import UpdateInfo
from tests.core.mod_update_fakes import UpdateDownloadBoundary
from tests.core.test_mod_update_service import _make_service, _meta
from tests.core.test_mod_update_service import sample_archive as sample_archive
from tests.helpers import registered_games as registered_games


@pytest.fixture
def update_service(temp_data_dir, qapp, qtbot):
    manager, config, service = _make_service()
    yield manager, config, service
    try:
        qtbot.waitUntil(service.shutdown_updates, timeout=10000)
    finally:
        if manager._ini_sync is not None:
            manager._ini_sync.stop()


def test_undo_ignore_restores_status_and_preserves_older_ignored_files(update_service, sample_archive):
    mm, _, service = update_service
    character = mm.add_character(name="示例角色丁", node_type="character")
    mod = mm.import_mod(source_path=str(sample_archive), node_id=character.id, gb_meta=_meta())
    mod.info.custom_data["gamebanana"]["ignored_file_ids"] = [7]
    mm.update_mod(mod)
    file = GbModFile(
        file_id=11,
        file_name="new.zip",
        description="",
        download_url="http://x",
        size_bytes=100,
        date_added_ts=2000,
        md5_checksum="bbb",
        av_result="clean",
    )
    info = UpdateInfo(
        local_mod_id=mod.mod_id,
        gb_mod_id=123,
        current_version="v1",
        current_file_ts=1000,
        latest_file=file,
        latest_version="v2",
        candidate_files=[file],
        changelog=[],
        page_url="http://p",
    )
    service._status[mod.mod_id] = info
    token = service.ignore_version(mod.mod_id)
    assert token is not None, "必须记录本次忽略的快照才能精确撤销"
    assert mod.mod_id not in service.updatable_ids()
    assert service.undo_ignore(token)
    assert mod.mod_id in service.updatable_ids()
    assert mm.get_mod(mod.mod_id).info.custom_data["gamebanana"]["ignored_file_ids"] == [7]
    token = service.ignore_mod(mod.mod_id)
    assert service.undo_ignore(token)
    restored = mm.get_mod(mod.mod_id).info.custom_data["gamebanana"]
    assert not restored.get("ignored") and restored["ignored_file_ids"] == [7]


def test_undo_ignore_rejects_a_different_game(update_service, sample_archive):
    mm, _, service = update_service
    character = mm.add_character(name="示例角色丁", node_type="character")
    mod = mm.import_mod(source_path=str(sample_archive), node_id=character.id, gb_meta=_meta())
    token = service.ignore_mod(mod.mod_id)
    assert token is not None
    mm.set_game("other-game")
    assert service.undo_ignore(token) is False


def test_canceled_download_can_retry_without_a_stale_apply_callback(update_service, sample_archive, qtbot):
    from types import SimpleNamespace

    from src.services.gamebanana.downloader import GbDownloadTask

    class Downloads(UpdateDownloadBoundary):
        def __init__(self, task):
            self.task = task

        def request(self, profile, file, *, game_id, consumer=None):
            return self.task

        def retry(self, file_id):
            assert file_id == self.task.file.file_id
            self.task.resume()

        def cancel(self, file_id):
            assert file_id == self.task.file.file_id
            self.task.cancel()

    mm, _, service = update_service
    character = mm.add_character(name="示例角色丁", node_type="character")
    mod = mm.import_mod(source_path=str(sample_archive), node_id=character.id, gb_meta=_meta())
    file = GbModFile(
        file_id=11,
        file_name="new.zip",
        description="",
        download_url="http://x",
        size_bytes=100,
        date_added_ts=2000,
        md5_checksum="bbb",
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
        page_url="http://p",
    )
    downloads = Downloads(
        GbDownloadTask(SimpleNamespace(mod_id=123, name="示例角色丁"), file, sample_archive, game_id="game_a")
    )
    service.set_download_manager(downloads)
    failures, canceled, applied, other_listener = [], [], [], []
    service.update_failed.connect(lambda mid, error: failures.append((mid, error)))
    service.update_cancelled.connect(canceled.append)
    service.update_applied.connect(lambda mid, updated: applied.append(mid))
    downloads.task.finished.connect(other_listener.append)
    service.apply_update(mod.mod_id)
    downloads.task.cancel()
    assert canceled == [mod.mod_id] and failures == []
    service.apply_update(mod.mod_id)
    assert downloads.task.state == "queued", "重试必须重新调度取消的任务"
    downloads.task.finished.emit(sample_archive)
    qtbot.waitUntil(lambda: bool(applied or failures), timeout=10000)
    assert failures == [] and canceled == [mod.mod_id]
    assert applied == [mod.mod_id]
    assert mm.get_mod(mod.mod_id).info.version == "v2"
    assert other_listener == [sample_archive], "只能解除更新服务自己注册的回调"


def test_removing_a_paused_download_allows_a_new_update(update_service, temp_data_dir, sample_archive, monkeypatch):
    from src.services.gamebanana.downloader import GbDownloadManager
    from tests.core.test_mod_update_game_switch import _update

    monkeypatch.setattr(GbDownloadManager, "_start_loop", lambda self: None)
    monkeypatch.setattr(GbDownloadManager, "_schedule", lambda self, task: None)
    mm, _, service = update_service
    character = mm.add_character(name="示例角色丁", node_type="character")
    mod = mm.import_mod(str(sample_archive), character.id, gb_meta=_meta())
    info = _update(mod)
    service._status[mod.mod_id] = info
    downloads = GbDownloadManager(temp_data_dir / "downloads")
    service.set_download_manager(downloads)
    failures = []
    service.update_failed.connect(lambda mid, error: failures.append(mid))
    service.apply_update(mod.mod_id)
    original = downloads.get_task(info.latest_file.file_id)
    downloads.pause(info.latest_file.file_id)
    downloads.delete_task(info.latest_file.file_id)
    assert failures == [mod.mod_id]
    service.apply_update(mod.mod_id)
    current = downloads.get_task(info.latest_file.file_id)
    assert current is not None and current is not original
    assert current.state == "queued"
