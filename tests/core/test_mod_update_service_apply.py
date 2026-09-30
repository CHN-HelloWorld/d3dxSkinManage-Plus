import zipfile
from unittest.mock import MagicMock

import pytest

from src.core.config import Config
from src.core.mod_manager import ModManager
from src.core.mod_update_service import ModUpdateService
from src.services.gamebanana.models import GbInstallMetadata, GbModFile
from src.services.gamebanana.update_checker import UpdateInfo
from tests.core.mod_update_fakes import UpdateDownloadBoundary
from tests.helpers import registered_games as registered_games


@pytest.fixture
def sample_archive(tmp_path):
    p = tmp_path / "new.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("ModFolder/a.ini", "[X]\nk=new\n")
    return p


def _meta():
    return GbInstallMetadata(
        mod_id=123,
        file_id=10,
        file_md5="oldmd5",
        file_ts=1000,
        page_url="https://gamebanana.com/mods/123",
        version="v1.0",
        submitter_id=1,
        submitter_name="A",
        last_checked_iso="",
    )


class _FakeDM(UpdateDownloadBoundary):
    """假下载器：request 永远返回 None（命中本地缓存），has_local_md5 给出归档路径。"""

    def __init__(self, path):
        self._path = path

    def request(self, profile, file, *, game_id="", consumer=None):
        return None

    def has_local_md5(self, md5):
        return self._path


@pytest.fixture
def update_service(temp_data_dir, qapp, qtbot):
    manager = ModManager()
    manager.set_game("game_a")
    service = ModUpdateService(manager, Config(), MagicMock())
    yield manager, service
    try:
        qtbot.waitUntil(service.shutdown_updates, timeout=10000)
    finally:
        if manager._ini_sync is not None:
            manager._ini_sync.stop()


def test_apply_update_replaces_and_emits(update_service, sample_archive, qtbot, temp_data_dir):
    mm, svc = update_service
    char = mm.add_character(name="示例角色丁", node_type="character")
    mod = mm.import_mod(source_path=str(sample_archive), node_id=char.id, gb_meta=_meta())

    svc.set_download_manager(_FakeDM(sample_archive))
    new_file = GbModFile(
        file_id=11,
        file_name="new.zip",
        description="",
        download_url="http://x",
        size_bytes=1,
        date_added_ts=2000,
        md5_checksum="newmd5",
        av_result="clean",
        version="v1.3",
    )
    svc._status[mod.mod_id] = UpdateInfo(
        local_mod_id=mod.mod_id,
        gb_mod_id=123,
        current_version="v1.0",
        current_file_ts=1000,
        latest_file=new_file,
        latest_version="v1.3",
        candidate_files=[new_file],
        changelog=[],
        page_url="http://p",
    )
    applied, failures = [], []
    svc.update_applied.connect(lambda mid, m: applied.append((mid, m)))
    svc.update_failed.connect(lambda mid, error: failures.append((mid, error)))
    svc.apply_update(mod.mod_id)
    qtbot.waitUntil(lambda: bool(applied or failures), timeout=10000)
    assert failures == []
    assert len(applied) == 1 and applied[0][0] == mod.mod_id
    persisted = mm.get_mod(mod.mod_id)
    assert persisted.info.version == "v1.3"
    assert persisted.info.custom_data["gamebanana"]["file_md5"] == "newmd5"
    assert mod.mod_id not in svc.updatable_ids()  # 更新后从可更新列表移除
    assert not list((temp_data_dir / "data").glob("*/.mod_backups"))
