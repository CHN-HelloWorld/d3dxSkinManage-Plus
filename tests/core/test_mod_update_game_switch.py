import json
import zipfile
from concurrent.futures import Future

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QSignalSpy

from src.core.config import Config
from src.core.mod_manager import ModManager
from src.core.mod_update_service import ModUpdateService
from src.services.gamebanana.models import GbInstallMetadata
from src.services.gamebanana.update_checker import UpdateInfo
from tests.core.mod_update_fakes import UpdateDownloadBoundary
from tests.helpers import (
    register_games,
    reset_game_service,
)
from tests.helpers import (
    registered_games as registered_games,
)


def _update(mod):
    return UpdateInfo.from_dict(
        {
            "local_mod_id": mod.mod_id,
            "gb_mod_id": 123,
            "current_file_ts": 1000,
            "latest_file": {
                "file_id": 11,
                "file_name": "update.zip",
                "date_added_ts": 2000,
                "version": "v2",
                "md5_checksum": "new",
            },
            "latest_version": "v2",
        }
    )


@pytest.fixture
def game_updates(qapp, qtbot, temp_data_dir):
    register_games("game-a", "game-b")
    manager = ModManager()
    mods = {}
    for gid in ("game-a", "game-b"):
        manager.set_game(gid)
        character = manager.add_character(name="Character", node_type="character")
        mods[gid] = []
        for index in range(2):
            archive = temp_data_dir / f"{gid}-{index}.zip"
            with zipfile.ZipFile(archive, "w") as zipped:
                zipped.writestr("mod.ini", f"[Constants]\n; {gid}-{index}\nx={index}\n")
            mod = manager.import_mod(
                str(archive),
                character.id,
                gb_meta=GbInstallMetadata(
                    mod_id=123,
                    file_id=10,
                    file_md5="old",
                    file_ts=1000,
                    page_url="https://gamebanana.com/mods/123",
                    version="v1",
                    submitter_id=1,
                    submitter_name="Author",
                    last_checked_iso="",
                ),
            )
            assert mod is not None
            mods[gid].append(mod)
    manager.set_game("game-a")
    pending = []

    def submit(_coroutine_factory):
        # 暂停网络请求，随后由真实检查器发出 Qt 信号。
        future = Future()
        pending.append(future)
        return future

    service = ModUpdateService(manager, Config(), object(), submit=submit)
    yield manager, service, mods, pending
    try:
        qtbot.waitUntil(service.shutdown_updates, timeout=10000)
    finally:
        for future in pending:
            if not future.done():
                future.set_result(None)
        if manager._ini_sync is not None:
            manager._ini_sync.stop()
        reset_game_service()


def test_late_check_results_cannot_repopulate_the_next_game(game_updates, temp_data_dir):
    manager, service, mods, pending = game_updates
    first, second = mods["game-a"]
    service.check_updates(force=True)
    service._checker.mod_update_found.emit(first.mod_id, _update(first))
    service._checker.mod_check_failed.emit(second.mod_id, "old game failure")

    manager.set_game("game-b")
    service.load_status()
    assert service.updatable_ids() == set()
    assert service.failures() == {}
    assert service.check_updates(force=True) is False, "a pending check must keep its original game"
    before = manager.get_mod(second.mod_id).info.custom_data.copy()
    finished = QSignalSpy(service.check_finished)
    progress = QSignalSpy(service.progress)

    service._checker.mod_update_found.emit(second.mod_id, _update(second))
    service._checker.mod_up_to_date.emit(first.mod_id)
    service._checker.mod_check_failed.emit(second.mod_id, "late failure")
    service._checker.progress.emit(2, 2)
    service._checker.finished.emit({"found": 2, "up_to_date": 0, "failed": 0})
    pending[0].set_result(None)

    assert service.updatable_ids() == set()
    assert service.failures() == {}
    assert finished.count() == progress.count() == 0
    assert {mod.mod_id for mod in manager.get_mods()} == {mod.mod_id for mod in mods["game-b"]}
    assert manager.get_mod(second.mod_id).info.custom_data == before
    assert not (temp_data_dir / "data/game-b/update_status.json").exists()
    saved_a = json.loads((temp_data_dir / "data/game-a/update_status.json").read_text(encoding="utf-8"))
    assert set(saved_a) == {first.mod_id}, "results received before switching must stay with their game"

    service.check_updates(force=True)
    current = mods["game-b"][0]
    service._checker.mod_update_found.emit(current.mod_id, _update(current))
    service._checker.finished.emit({"found": 1, "up_to_date": 0, "failed": 0})
    assert service.updatable_ids() == {current.mod_id}
    assert finished.count() == 1


def test_preparing_check_does_not_reconstruct_mod_paths(game_updates, monkeypatch):
    manager, service, mods, _pending = game_updates
    decoded = []
    original = manager._index._row_to_mod

    def decode(row, cache=None):
        decoded.append(row["mod_id"])
        return original(row, cache)

    monkeypatch.setattr(manager._index, "_row_to_mod", decode)
    assert service.check_updates(force=True)
    expected = {mod.mod_id for mod in mods["game-a"]}
    assert set(service._check_jobs) == expected
    assert not decoded, "checking update metadata must not reconstruct archive and preview paths"


def test_loaded_status_excludes_other_games_and_deactivation_clears_it(game_updates, temp_data_dir):
    manager, service, mods, _ = game_updates
    current = mods["game-b"][0]
    foreign = mods["game-a"][0]
    manager.set_game("game-b")
    status_path = temp_data_dir / "data/game-b/update_status.json"
    status_path.write_text(
        json.dumps({mod.mod_id: _update(mod).to_dict() for mod in (current, foreign)}), encoding="utf-8"
    )
    service.load_status()
    assert service.updatable_ids() == {current.mod_id}

    # 没有活动游戏时不读取缺少游戏归属的文件。
    (temp_data_dir / "data/update_status.json").write_text(status_path.read_text(encoding="utf-8"), encoding="utf-8")
    manager.set_game("")
    service.load_status()
    assert service.updatable_ids() == set()
    assert service._collect_jobs(force=True)[0] == []


def test_download_finishing_after_game_switch_keeps_file_for_original_game_retry(game_updates, temp_data_dir, qtbot):
    manager, service, mods, _ = game_updates
    original = mods["game-a"][0]
    other = mods["game-b"][0]
    status_paths = {}
    for gid, mod in (("game-a", original), ("game-b", other)):
        path = temp_data_dir / "data" / gid / "update_status.json"
        path.write_text(json.dumps({mod.mod_id: _update(mod).to_dict()}), encoding="utf-8")
        status_paths[gid] = path
    service.load_status()
    before = {gid: path.read_bytes() for gid, path in status_paths.items()}
    archive = temp_data_dir / "data/downloads/update.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("mod.ini", "[Constants]\nx=42\n")

    class DownloadTask(QObject):
        finished = Signal(object)
        failed = Signal(str)

    class DownloadManager(UpdateDownloadBoundary):
        cached = False
        task = DownloadTask()

        def request(self, profile, file, *, game_id="", consumer=None):
            return None if self.cached else self.task

        def has_local_md5(self, md5):
            return archive if self.cached else None

        def cancel(self, file_id):
            # 手动发出状态的下载任务没有关联网络操作。
            assert file_id == 11

    downloads = DownloadManager()
    service.set_download_manager(downloads)
    applied, failures = QSignalSpy(service.update_applied), QSignalSpy(service.update_failed)
    service.apply_update(original.mod_id)
    manager.set_game("game-b")
    service.load_status()
    downloads.cached = True
    downloads.task.finished.emit(archive)

    assert manager.get_mod(original.mod_id).info.version == "v1", (
        "the old game's update must not apply under the new game"
    )
    assert applied.count() == 0
    assert service.updatable_ids() == {other.mod_id}
    assert archive.exists(), "the completed download must remain available for retry"
    assert {gid: path.read_bytes() for gid, path in status_paths.items()} == before
    assert {mod.mod_id for mod in manager.get_mods()} == {mod.mod_id for mod in mods["game-b"]}

    manager.set_game("game-a")
    service.load_status()
    service.apply_update(original.mod_id)
    qtbot.waitUntil(lambda: applied.count() + failures.count() > 0, timeout=10000)
    assert [failures.at(index) for index in range(failures.count())] == []
    assert applied.count() == 1 and applied.at(0)[0] == original.mod_id
    assert manager.get_mod(original.mod_id).info.version == "v2"
    assert service.updatable_ids() == set()
    assert json.loads(status_paths["game-a"].read_text(encoding="utf-8")) == {}
