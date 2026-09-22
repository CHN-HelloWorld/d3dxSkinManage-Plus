import asyncio
import zipfile

import pytest

from src.services.gamebanana.models import GbInstallMetadata
from tests.helpers import registered_games as registered_games


@pytest.fixture
def sample_archive(tmp_path):
    p = tmp_path / "m.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("ModFolder/a.ini", "[X]\nk=v\n")
    return p


def _meta(mod_id=123, file_ts=1000, last_checked=""):
    return GbInstallMetadata(
        mod_id=mod_id,
        file_id=10,
        file_md5="aaa",
        file_ts=file_ts,
        page_url=f"https://gamebanana.com/mods/{mod_id}",
        version="v1",
        submitter_id=1,
        submitter_name="A",
        last_checked_iso=last_checked,
    )


def _make_service():
    from unittest.mock import MagicMock

    from src.core.config import Config
    from src.core.mod_manager import ModManager
    from src.core.mod_update_service import ModUpdateService

    mm = ModManager()
    mm.set_game("game_a")
    cfg = Config()
    svc = ModUpdateService(mm, cfg, MagicMock())  # client 不真用（不调 check）
    return mm, cfg, svc


def test_collect_jobs_respects_throttle(temp_data_dir, sample_archive):
    from datetime import datetime, timedelta

    mm, cfg, svc = _make_service()
    char = mm.add_character(name="角色", node_type="character")
    recent = (datetime.now() - timedelta(hours=1)).isoformat()
    mod = mm.import_mod(source_path=str(sample_archive), node_id=char.id, gb_meta=_meta(last_checked=recent))
    # 非 force：1h < 24h 节流间隔 → 跳过
    jobs, _ = svc._collect_jobs(force=False)
    assert jobs == []
    # force：忽略节流
    jobs, _ = svc._collect_jobs(force=True)
    assert len(jobs) == 1 and jobs[0][0] == mod.mod_id


def test_status_persist_and_load(temp_data_dir, sample_archive):
    from src.services.gamebanana.models import GbModFile
    from src.services.gamebanana.update_checker import UpdateInfo

    mm, cfg, svc = _make_service()
    char = mm.add_character(name="角色", node_type="character")
    mod = mm.import_mod(source_path=str(sample_archive), node_id=char.id, gb_meta=_meta())
    f = GbModFile(
        file_id=11,
        file_name="s.zip",
        description="",
        download_url="http://x",
        size_bytes=1,
        date_added_ts=2000,
        md5_checksum="bbb",
        av_result="clean",
    )
    info = UpdateInfo(
        local_mod_id=mod.mod_id,
        gb_mod_id=123,
        current_version="v1",
        current_file_ts=1000,
        latest_file=f,
        latest_version="v2",
        candidate_files=[f],
        changelog=[],
        page_url="http://p",
    )
    svc._status[mod.mod_id] = info
    svc._save_status()
    # 新建一个 service，load → 角标应恢复
    from unittest.mock import MagicMock

    from src.core.mod_update_service import ModUpdateService

    svc2 = ModUpdateService(mm, cfg, MagicMock())
    svc2.load_status()
    assert mod.mod_id in svc2.updatable_ids()


def test_ignore_mod_persists(temp_data_dir, sample_archive):
    mm, cfg, svc = _make_service()
    char = mm.add_character(name="角色", node_type="character")
    mod = mm.import_mod(source_path=str(sample_archive), node_id=char.id, gb_meta=_meta())
    svc.ignore_mod(mod.mod_id)
    persisted = mm.get_mod(mod.mod_id)
    assert persisted.info.custom_data["gamebanana"]["ignored"] is True
    # 被忽略的 mod 不进入 job
    jobs, _ = svc._collect_jobs(force=True)
    assert jobs == []


@pytest.mark.parametrize("http_status", [200, 429])
async def test_batch_result_writes_last_checked_only_on_success(
    temp_data_dir,
    sample_archive,
    tmp_path,
    respx_mock,
    qapp,
    http_status,
):
    import httpx

    from src.core.mod_update_service import ModUpdateService
    from src.services.gamebanana.cache import GbApiCache
    from src.services.gamebanana.client import GameBananaClient
    from tests.helpers import register_games, reset_game_service

    register_games("game_a")
    client = GameBananaClient(
        request_interval_ms=0,
        cache=GbApiCache(persist_dir=tmp_path / "gb_api_cache"),
    )
    try:
        mm, cfg, _ = _make_service()
        submitted = []

        def submit(factory):
            task = asyncio.create_task(factory())
            submitted.append(task)
            return task

        svc = ModUpdateService(mm, cfg, client, submit=submit)
        char = mm.add_character(name="角色", node_type="character")
        previous = "2020-01-01T00:00:00"
        mod = mm.import_mod(
            source_path=str(sample_archive),
            node_id=char.id,
            gb_meta=_meta(last_checked=previous),
        )
        route = respx_mock.get("https://gamebanana.com/apiv11/Mod/Multi").mock(
            return_value=httpx.Response(http_status, json=[{"_idRow": 123, "_aFiles": []}]),
        )
        results = []
        svc.check_finished.connect(results.append)
        assert svc.check_updates(force=False)
        await submitted[0]
        persisted = mm.get_mod(mod.mod_id).info.custom_data["gamebanana"]["last_checked_iso"]
        if http_status == 200:
            assert results == [{"found": 0, "up_to_date": 1, "failed": 0}]
            assert persisted != previous
            assert svc.failures() == {}
        else:
            assert results == [{"found": 0, "up_to_date": 0, "failed": 1}]
            assert persisted == previous
            assert "429" in svc.failures()[mod.mod_id]
        assert route.call_count == 1
    finally:
        await client.aclose()
        reset_game_service()


# url 回退：丢了结构化 gb_meta 但 info.url 指向 GameBanana 的 Mod 仍可检查


def test_collect_jobs_falls_back_to_info_url(temp_data_dir, sample_archive):
    """缺少结构化来源信息时，仍应根据 GameBanana URL 解析资源编号并检查更新。"""
    mm, cfg, svc = _make_service()
    char = mm.add_character(name="角色", node_type="character")
    mod = mm.import_mod(source_path=str(sample_archive), node_id=char.id)  # 无 gb_meta
    assert (mod.info.custom_data or {}).get("gamebanana") in (None, {})
    mod.info.url = "https://gamebanana.com/mods/678677"
    mm.update_mod(mod)
    jobs, _ = svc._collect_jobs(force=True)
    assert len(jobs) == 1
    mid, meta = jobs[0]
    assert mid == mod.mod_id
    assert meta.mod_id == 678677
    assert meta.file_ts == 0 and meta.file_md5 == ""  # 基线未知
    assert meta.page_url == "https://gamebanana.com/mods/678677"


def test_collect_jobs_ignores_non_gamebanana_url(temp_data_dir, sample_archive):
    mm, cfg, svc = _make_service()
    char = mm.add_character(name="角色", node_type="character")
    mod = mm.import_mod(source_path=str(sample_archive), node_id=char.id)
    mod.info.url = "https://example.com/whatever/123"
    mm.update_mod(mod)
    assert svc._collect_jobs(force=True)[0] == []


def test_collect_jobs_skips_mod_without_any_gb_source(temp_data_dir, sample_archive):
    mm, cfg, svc = _make_service()
    char = mm.add_character(name="角色", node_type="character")
    mm.import_mod(source_path=str(sample_archive), node_id=char.id)  # 无 gb_meta、无 url
    assert svc._collect_jobs(force=True)[0] == []


def test_load_status_prunes_orphan_entries(temp_data_dir, sample_archive):
    from unittest.mock import MagicMock

    from src.core.mod_update_service import ModUpdateService
    from src.services.gamebanana.models import GbModFile
    from src.services.gamebanana.update_checker import UpdateInfo

    mm, cfg, svc = _make_service()
    char = mm.add_character(name="角色", node_type="character")
    mod = mm.import_mod(source_path=str(sample_archive), node_id=char.id, gb_meta=_meta())
    f = GbModFile(
        file_id=11,
        file_name="s.zip",
        description="",
        download_url="http://x",
        size_bytes=1,
        date_added_ts=2000,
        md5_checksum="bbb",
        av_result="clean",
    )

    def _info(mid):
        return UpdateInfo(
            local_mod_id=mid,
            gb_mod_id=123,
            current_version="v1",
            current_file_ts=1000,
            latest_file=f,
            latest_version="v2",
            candidate_files=[f],
            changelog=[],
            page_url="http://p",
        )

    svc._status[mod.mod_id] = _info(mod.mod_id)
    svc._status["DEADBEEFDEADBEEF"] = _info("DEADBEEFDEADBEEF")  # 孤儿：对应 Mod 不存在
    svc._save_status()
    svc2 = ModUpdateService(mm, cfg, MagicMock())
    svc2.load_status()
    assert mod.mod_id in svc2.updatable_ids()
    assert "DEADBEEFDEADBEEF" not in svc2.updatable_ids()
