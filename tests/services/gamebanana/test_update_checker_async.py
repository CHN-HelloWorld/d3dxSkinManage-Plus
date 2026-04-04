from unittest.mock import AsyncMock

from src.services.gamebanana.client import GameBananaClient
from src.services.gamebanana.models import (
    GbAuthor,
    GbInstallMetadata,
    GbModel,
    GbModFile,
    GbModProfile,
)
from src.services.gamebanana.update_checker import ModUpdateChecker


def _meta(mod_id, file_ts=1000, file_md5="aaa"):
    return GbInstallMetadata(
        mod_id=mod_id,
        file_id=10,
        file_md5=file_md5,
        file_ts=file_ts,
        page_url=f"https://gamebanana.com/mods/{mod_id}",
        version="v1",
        submitter_id=1,
        submitter_name="A",
        last_checked_iso="",
    )


def _profile(mod_id, date_updated_ts):
    return GbModProfile(
        mod_id=mod_id,
        model=GbModel.MOD,
        name="m",
        submitter=GbAuthor(id=1, name="A", avatar_url="", profile_url=""),
        thumbnail_url="",
        page_url=f"https://gamebanana.com/mods/{mod_id}",
        view_count=0,
        like_count=0,
        download_count=0,
        comment_count=0,
        is_nsfw=False,
        date_updated_ts=date_updated_ts,
    )


def _file(ts, md5):
    return GbModFile(
        file_id=11,
        file_name="skin.zip",
        description="",
        download_url="http://x",
        size_bytes=1,
        date_added_ts=ts,
        md5_checksum=md5,
        av_result="clean",
    )


def test_checker_finds_update(qtbot):
    client = AsyncMock(spec=GameBananaClient)
    client.bulk_update_files.return_value = {123: [_file(2000, "bbb")]}
    client.get_updates.return_value = []
    checker = ModUpdateChecker(client)
    found = []
    checker.mod_update_found.connect(lambda mid, info: found.append((mid, info)))
    with qtbot.waitSignal(checker.finished, timeout=5000) as blocker:
        checker.start([("mid1", _meta(123))], {"mid1": set()}, force=True)
    assert blocker.args[0]["found"] == 1
    assert found and found[0][0] == "mid1"
    assert found[0][1].latest_file.md5_checksum == "bbb"


def test_checker_up_to_date_via_batch_files(qtbot):
    client = AsyncMock(spec=GameBananaClient)
    client.bulk_update_files.return_value = {123: [_file(1000, "aaa")]}
    checker = ModUpdateChecker(client)
    utd = []
    checker.mod_up_to_date.connect(lambda mid: utd.append(mid))
    with qtbot.waitSignal(checker.finished, timeout=5000):
        checker.start([("mid1", _meta(123, file_ts=1000))], {}, force=False)
    assert utd == ["mid1"]
    client.get_profile.assert_not_called()
    client.get_download_page.assert_not_called()
    client.get_updates.assert_not_called()


def test_checker_reports_failure(qtbot):
    client = AsyncMock(spec=GameBananaClient)
    client.bulk_update_files.side_effect = RuntimeError("network down")
    checker = ModUpdateChecker(client)
    failed = []
    checker.mod_check_failed.connect(lambda mid, err: failed.append((mid, err)))
    with qtbot.waitSignal(checker.finished, timeout=5000) as blocker:
        checker.start([("mid1", _meta(123))], {}, force=False)
    assert blocker.args[0]["failed"] == 1
    assert failed and failed[0][0] == "mid1"


def test_checker_uses_injected_submit(qtbot):
    """注入共享协程提交器后，更新检查应复用其事件循环。

    GameBananaClient 的连接池绑定事件循环，不能在另一个临时循环中重复使用。
    """
    import asyncio
    import threading

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        calls = []

        def submit(coro_factory):
            calls.append(coro_factory)

            async def _runner():
                return await coro_factory()

            return asyncio.run_coroutine_threadsafe(_runner(), loop)

        client = AsyncMock(spec=GameBananaClient)
        client.bulk_update_files.return_value = {123: [_file(2000, "bbb")]}
        client.get_updates.return_value = []
        checker = ModUpdateChecker(client, submit=submit)
        found = []
        checker.mod_update_found.connect(lambda mid, info: found.append((mid, info)))
        with qtbot.waitSignal(checker.finished, timeout=5000) as blocker:
            checker.start([("mid1", _meta(123))], {"mid1": set()}, force=True)
        assert blocker.args[0]["found"] == 1
        assert len(calls) == 1  # 走了注入的 submit
        assert checker._thread is None  # 没有回退到自起一次性 loop 的线程
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2)
