import asyncio
import json
import time
from email.utils import formatdate
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.services.gamebanana import client as client_module
from src.services.gamebanana.cache import GbApiCache
from src.services.gamebanana.client import GameBananaClient
from src.services.gamebanana.models import GbInstallMetadata, GbModel
from src.services.gamebanana.update_checker import ModUpdateChecker

BASE = "https://gamebanana.com/apiv11"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
async def client(tmp_path):
    instance = GameBananaClient(
        request_interval_ms=0,
        cache=GbApiCache(persist_dir=tmp_path / "gb_api_cache"),
    )
    yield instance
    await instance.aclose()


def _file(*, timestamp=2000, md5="bbb", archived=False):
    return {
        "_idRow": 11,
        "_sFile": "skin.zip",
        "_nFilesize": 123,
        "_tsDateAdded": timestamp,
        "_sMd5Checksum": md5,
        "_sVersion": "v2",
        "_sDownloadUrl": "https://gamebanana.com/dl/11",
        "_sAvResult": "clean",
        "_bIsArchived": archived,
    }


def _meta(mod_id, *, timestamp=1000, md5="aaa", version="v1"):
    return GbInstallMetadata(
        mod_id=mod_id,
        file_id=10,
        file_ts=timestamp,
        file_md5=md5,
        page_url=f"https://gamebanana.com/mods/{mod_id}",
        version=version,
        submitter_id=1,
        submitter_name="Author",
        last_checked_iso="",
    )


def _observe(checker):
    events = {"found": {}, "up_to_date": [], "failed": {}, "progress": [], "finished": []}
    checker.mod_update_found.connect(lambda mid, info: events["found"].update({mid: info}))
    checker.mod_up_to_date.connect(events["up_to_date"].append)
    checker.mod_check_failed.connect(lambda mid, error: events["failed"].update({mid: error}))
    checker.progress.connect(lambda done, total: events["progress"].append((done, total)))
    checker.finished.connect(events["finished"].append)
    return events


async def test_multi_active_files_match_the_real_download_page(client, respx_mock):
    payload = json.loads((FIXTURES / "multi_files_sample.json").read_text(encoding="utf-8"))
    downloaded = json.loads((FIXTURES / "download_variants_sample.json").read_text(encoding="utf-8"))
    route = respx_mock.get(f"{BASE}/Mod/Multi").respond(200, json=payload)
    respx_mock.get(f"{BASE}/Mod/310003/DownloadPage").respond(200, json=downloaded)

    result = await client.bulk_update_files([310001, 310002, 310003])
    assert result[310003] == await client.get_download_page(GbModel.MOD, 310003)
    assert [file.file_id for file in result[310003]] == [430003, 430004]
    assert len(result[310001]) == 3
    assert len(result[310002]) == 1
    assert "_aFiles" in route.calls[0].request.url.params["_csvProperties"].split(",")


async def test_bulk_files_batch_unique_ids_cache_and_force_refresh(client, respx_mock):
    timestamp = [2000]
    batches = []

    def respond(request):
        ids = [int(value) for value in request.url.params["_csvRowIds"].split(",")]
        batches.append(ids)
        return httpx.Response(200, json=[{"_idRow": mid, "_aFiles": [_file(timestamp=timestamp[0])]} for mid in ids])

    respx_mock.get(f"{BASE}/Mod/Multi").mock(side_effect=respond)
    assert await client.bulk_update_files([]) == {}
    result = await client.bulk_update_files(list(range(1, 102)) + [1, 50, 0, -1, True])
    assert set(result) == set(range(1, 102))
    assert batches == [list(range(1, 51)), list(range(51, 101)), [101]]

    timestamp[0] = 3000
    assert (await client.bulk_update_files([1]))[1][0].date_added_ts == 2000
    assert len(batches) == 3
    assert (await client.bulk_update_files([1], force=True))[1][0].date_added_ts == 3000
    assert batches[-1] == [1]
    assert len(batches) == 4


@pytest.mark.parametrize("force", [False, True])
async def test_a_batch_of_only_missing_files_still_uses_bounded_download_fallback(client, respx_mock, qapp, force):
    multi = respx_mock.get(f"{BASE}/Mod/Multi").respond(200, json=[{"_idRow": 1, "_aFiles": None}])
    download = respx_mock.get(f"{BASE}/Mod/1/DownloadPage").respond(200, json={"_aFiles": [_file(md5="aaa")]})
    checker = ModUpdateChecker(client)
    events = _observe(checker)
    await checker._run_all([("mid1", _meta(1))], {}, force)
    assert events["finished"] == [{"found": 0, "up_to_date": 1, "failed": 0}]
    assert multi.call_count == download.call_count == 1


async def test_missing_files_remain_fallback_eligible_when_other_records_are_cached(client, respx_mock, qapp):
    batches = []

    def respond(request):
        ids = [int(value) for value in request.url.params["_csvRowIds"].split(",")]
        batches.append(ids)
        return httpx.Response(200, json=[{"_idRow": mid, "_aFiles": [] if mid == 1 else None} for mid in ids])

    respx_mock.get(f"{BASE}/Mod/Multi").mock(side_effect=respond)
    download = respx_mock.get(f"{BASE}/Mod/2/DownloadPage").respond(200, json={"_aFiles": [_file(md5="aaa")]})
    checker = ModUpdateChecker(client)
    events = _observe(checker)
    jobs = [("mid1", _meta(1)), ("mid2", _meta(2))]
    await checker._run_all(jobs, {}, False)
    await checker._run_all(jobs, {}, False)
    assert events["finished"] == [{"found": 0, "up_to_date": 2, "failed": 0}] * 2
    assert batches == [[1, 2], [2]]
    assert download.call_count == 2


@pytest.mark.parametrize("invalidate", ["mod", "game", "ttl"])
async def test_bulk_files_cache_obeys_existing_invalidation(client, respx_mock, monkeypatch, invalidate):
    route = respx_mock.get(f"{BASE}/Mod/Multi").mock(
        side_effect=[
            httpx.Response(200, json=[{"_idRow": 1, "_aFiles": [_file(timestamp=2000)]}]),
            httpx.Response(200, json=[{"_idRow": 1, "_aFiles": [_file(timestamp=3000)]}]),
        ]
    )
    assert (await client.bulk_update_files([1]))[1][0].date_added_ts == 2000
    if invalidate == "mod":
        client.invalidate_mod_cache(GbModel.MOD, 1)
    elif invalidate == "game":
        client.invalidate_game_caches(101)
    else:
        later = time.time() + 601
        monkeypatch.setattr("src.services.gamebanana.cache.time.time", lambda: later)
    assert (await client.bulk_update_files([1]))[1][0].date_added_ts == 3000
    assert route.call_count == 2


@pytest.mark.parametrize("force", [False, True])
async def test_file_batch_checks_all_files_but_fetches_only_needed_changelogs(client, respx_mock, qapp, force):
    def respond(request):
        if request.url.path.endswith("/Multi"):
            return httpx.Response(
                200,
                json=[
                    {
                        "_idRow": mid,
                        "_tsDateUpdated": 500,
                        "_aFiles": [_file(timestamp=1000 if mid <= 12 else 2000, md5="aaa" if mid == 13 else "bbb")],
                    }
                    for mid in range(1, 16)
                ],
            )
        assert request.url.path == "/apiv11/Mod/15/Updates"
        return httpx.Response(200, json=[{"_sTitle": "Updated", "_tsDateAdded": 2000}])

    route = respx_mock.get(url__regex=rf"{BASE}/Mod/.*").mock(side_effect=respond)
    checker = ModUpdateChecker(client)
    events = _observe(checker)
    await checker._run_all([(f"mid{mid}", _meta(mid)) for mid in range(1, 16)], {"mid14": {11}}, force)

    assert events["finished"] == [{"found": 1, "up_to_date": 14, "failed": 0}]
    assert set(events["found"]) == {"mid15"}
    assert events["found"]["mid15"].changelog[0].title == "Updated"
    assert events["progress"][-1] == (15, 15)
    assert route.call_count == 2  # 一个文件批次及对应的更新日志。


async def test_local_copies_share_requests_but_keep_baselines_and_ignored_versions(client, respx_mock, qapp):
    multi = respx_mock.get(f"{BASE}/Mod/Multi").respond(200, json=[{"_idRow": 1, "_aFiles": [_file()]}])
    updates = respx_mock.get(f"{BASE}/Mod/1/Updates").respond(
        200,
        json=[
            {"_sTitle": "Earlier", "_tsDateAdded": 900},
            {"_sTitle": "Latest", "_tsDateAdded": 2000},
        ],
    )
    checker = ModUpdateChecker(client)
    events = _observe(checker)
    await checker._run_all(
        [
            ("first", _meta(1, timestamp=500)),
            ("second", _meta(1)),
            ("ignored", _meta(1)),
            ("unknown", _meta(1, timestamp=0, md5="", version="v2")),
        ],
        {"ignored": {11}},
        True,
    )

    assert events["finished"] == [{"found": 2, "up_to_date": 2, "failed": 0}]
    first, second = events["found"]["first"], events["found"]["second"]
    assert first is not second
    assert (first.local_mod_id, first.current_file_ts) == ("first", 500)
    assert (second.local_mod_id, second.current_file_ts) == ("second", 1000)
    assert [entry.title for entry in first.changelog] == ["Latest", "Earlier"]
    assert [entry.title for entry in second.changelog] == ["Latest"]
    assert multi.calls[0].request.url.params["_csvRowIds"] == "1"
    assert multi.call_count == updates.call_count == 1


async def test_only_missing_or_malformed_rows_use_download_fallback(client, respx_mock, qapp):
    def respond(request):
        if request.url.path.endswith("/Multi"):
            return httpx.Response(
                200,
                json=[
                    {"_idRow": 1, "_aFiles": []},
                    {"_idRow": 2},
                    {"_idRow": 3, "_aFiles": [None]},
                    {"_idRow": 4, "_aFiles": [_file(archived=True)]},
                    {"_idRow": 5, "_aFiles": [_file(), {"_idRow": 12, "_tsDateAdded": "invalid"}]},
                    {"_idRow": 6, "_aFiles": [dict(_file(), _bIsArchived="false")]},
                    {"_idRow": 999, "_aFiles": []},
                ],
            )
        if request.url.path == "/apiv11/Mod/2/DownloadPage":
            return httpx.Response(200, json={"_aFiles": [_file()]})
        if request.url.path == "/apiv11/Mod/2/Updates":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    route = respx_mock.get(url__regex=rf"{BASE}/Mod/.*").mock(side_effect=respond)
    checker = ModUpdateChecker(client)
    events = _observe(checker)
    await checker._run_all([(f"mid{mid}", _meta(mid)) for mid in range(1, 7)], {}, True)
    assert events["finished"] == [{"found": 1, "up_to_date": 2, "failed": 3}]
    assert set(events["up_to_date"]) == {"mid1", "mid4"}
    assert set(events["failed"]) == {"mid3", "mid5", "mid6"}
    assert {call.request.url.path for call in route.calls if call.request.url.path.endswith("/DownloadPage")} == {
        "/apiv11/Mod/2/DownloadPage",
        "/apiv11/Mod/3/DownloadPage",
        "/apiv11/Mod/5/DownloadPage",
        "/apiv11/Mod/6/DownloadPage",
    }


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"_aRecords": "invalid"},
        [None, {"_idRow": 999, "_aFiles": []}],
    ],
)
async def test_unusable_batch_does_not_report_latest_or_expand_requests(client, respx_mock, qapp, payload):
    route = respx_mock.get(url__regex=rf"{BASE}/Mod/.*").respond(200, json=payload)
    checker = ModUpdateChecker(client)
    events = _observe(checker)
    await checker._run_all([(f"mid{mid}", _meta(mid)) for mid in range(1, 10)], {}, True)
    assert events["finished"] == [{"found": 0, "up_to_date": 0, "failed": 9}]
    assert events["progress"][-1] == (9, 9)
    assert route.call_count == 1


@pytest.mark.parametrize("failure,max_requests", [(429, 1), (503, 2), (404, 1), ("timeout", 2)])
async def test_failed_batch_never_expands_into_per_mod_requests(client, respx_mock, qapp, failure, max_requests):
    if failure == "timeout":
        route = respx_mock.get(url__regex=rf"{BASE}/Mod/.*").mock(side_effect=httpx.ReadTimeout("timed out"))
    else:
        route = respx_mock.get(url__regex=rf"{BASE}/Mod/.*").respond(failure)
    checker = ModUpdateChecker(client)
    events = _observe(checker)
    await checker._run_all([(f"mid{mid}", _meta(mid)) for mid in range(1, 102)], {}, True)

    assert events["finished"] == [{"found": 0, "up_to_date": 0, "failed": 101}]
    assert events["progress"][-1] == (101, 101)
    assert route.call_count == max_requests
    assert all(call.request.url.path == "/apiv11/Mod/Multi" for call in route.calls)


async def test_later_batch_failure_preserves_checked_files_without_more_requests(client, respx_mock, qapp):
    def respond(request):
        ids = [int(value) for value in request.url.params["_csvRowIds"].split(",")]
        if ids[0] > 50:
            return httpx.Response(503)
        return httpx.Response(200, json=[{"_idRow": mid, "_aFiles": [_file(md5="aaa")]} for mid in ids])

    route = respx_mock.get(f"{BASE}/Mod/Multi").mock(side_effect=respond)
    checker = ModUpdateChecker(client)
    events = _observe(checker)
    await checker._run_all([(f"mid{mid}", _meta(mid)) for mid in range(1, 102)], {}, True)
    assert events["finished"] == [{"found": 0, "up_to_date": 50, "failed": 51}]
    assert events["progress"][-1] == (101, 101)
    assert route.call_count == 3


@pytest.mark.parametrize("date_header", [False, True])
async def test_retry_after_blocks_manual_and_automatic_checks_until_expiry(
    client,
    respx_mock,
    qapp,
    monkeypatch,
    date_header,
):
    now = [1000.0]
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: now[0]))
    retry_after = formatdate(1300, usegmt=True) if date_header else "300"
    route = respx_mock.get(f"{BASE}/Mod/Multi").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": retry_after}),
            httpx.Response(200, json=[{"_idRow": 1, "_aFiles": [_file(md5="aaa")]}]),
        ]
    )
    jobs = [("mid1", _meta(1))]
    checker = ModUpdateChecker(client)
    events = _observe(checker)
    await checker._run_all(jobs, {}, True)
    assert events["finished"][-1] == {"found": 0, "up_to_date": 0, "failed": 1}
    now[0] += 299
    for force in (False, True):
        another = ModUpdateChecker(client)
        later = _observe(another)
        await another._run_all(jobs, {}, force)
        assert later["finished"] == [{"found": 0, "up_to_date": 0, "failed": 1}]
    assert route.call_count == 1
    now[0] += 1
    await checker._run_all(jobs, {}, True)
    assert events["finished"][-1] == {"found": 0, "up_to_date": 1, "failed": 0}
    assert route.call_count == 2


async def test_detail_workers_stop_queued_requests_on_429_but_keep_file_results(client, respx_mock, qapp):
    entered = []
    second_entered = asyncio.Event()

    async def respond(request):
        if request.url.path.endswith("/Multi"):
            return httpx.Response(200, json=[{"_idRow": mid, "_aFiles": [_file()]} for mid in range(1, 21)])
        assert request.url.path.endswith("/Updates")
        entered.append(request.url.path)
        if len(entered) == 1:
            await asyncio.wait_for(second_entered.wait(), 1)
            return httpx.Response(429)
        second_entered.set()
        await asyncio.sleep(0.02)
        return httpx.Response(200, json=[])

    route = respx_mock.get(url__regex=rf"{BASE}/Mod/.*").mock(side_effect=respond)
    checker = ModUpdateChecker(client)
    events = _observe(checker)
    await checker._run_all([(f"mid{mid}", _meta(mid)) for mid in range(1, 21)], {}, True)
    assert events["finished"] == [{"found": 20, "up_to_date": 0, "failed": 0}]
    assert len(entered) == 2
    assert route.call_count == 3


@pytest.mark.parametrize("retry_succeeds", [False, True])
async def test_store_inflight_429_stops_update_requests_even_when_the_store_retry_succeeds(
    client,
    respx_mock,
    qapp,
    retry_succeeds,
):
    store_entered = asyncio.Event()
    allow_429 = asyncio.Event()
    store_attempts = 0
    saw_429 = False
    late_update_requests = []

    async def respond(request):
        nonlocal store_attempts, saw_429
        path = request.url.path
        if path.endswith("/Multi"):
            allow_429.set()
            return httpx.Response(200, json=[{"_idRow": mid, "_aFiles": [_file()]} for mid in range(1, 6)])
        if path == "/apiv11/Mod/1/Updates":
            store_attempts += 1
            if store_attempts == 1:
                store_entered.set()
                await allow_429.wait()
            if store_attempts == 1 or not retry_succeeds:
                saw_429 = True
                return httpx.Response(429, headers={"Retry-After": "300"})
            return httpx.Response(200, json=[])
        if saw_429:
            late_update_requests.append(path)
        return httpx.Response(200, json=[])

    route = respx_mock.get(url__regex=rf"{BASE}/Mod/.*").mock(side_effect=respond)
    store_task = asyncio.create_task(client.get_updates(GbModel.MOD, 1))
    try:
        await asyncio.wait_for(store_entered.wait(), 1)
        checker = ModUpdateChecker(client)
        events = _observe(checker)
        jobs = [(f"mid{mid}", _meta(mid)) for mid in range(1, 6)]
        await checker._run_all(jobs, {}, True)
        assert events["finished"] == [{"found": 5, "up_to_date": 0, "failed": 0}]
        assert late_update_requests == []
        previous_requests = route.call_count
        await checker._run_all(jobs, {}, True)
        assert events["finished"][-1] == {"found": 0, "up_to_date": 0, "failed": 5}
        assert route.call_count == previous_requests
    finally:
        allow_429.set()
        await asyncio.gather(store_task, return_exceptions=True)


@pytest.mark.parametrize("failure_type", [httpx.ReadTimeout, httpx.ConnectError])
async def test_empty_network_error_stops_pending_fallback_requests(client, respx_mock, qapp, failure_type):
    def respond(request):
        if request.url.path.endswith("/Multi"):
            return httpx.Response(200, json=[{"_idRow": 1, "_aFiles": []}])
        raise failure_type("")

    route = respx_mock.get(url__regex=rf"{BASE}/Mod/.*").mock(side_effect=respond)
    checker = ModUpdateChecker(client)
    events = _observe(checker)
    await checker._run_all([(f"mid{mid}", _meta(mid)) for mid in range(1, 7)], {}, True)
    assert events["finished"] == [{"found": 0, "up_to_date": 1, "failed": 5}]
    assert route.call_count <= 5  # 一个批次，外加最多两个工作线程各两次尝试。
    assert all(events["failed"].values()), "a failed check must include a usable error message"


async def test_request_interval_spaces_concurrent_request_starts(client, respx_mock):
    client._interval = 0.04
    starts = []

    def respond(request):
        starts.append(time.monotonic())
        return httpx.Response(200, json={"_aFiles": [_file()]})

    respx_mock.get(url__regex=rf"{BASE}/Mod/\d+/DownloadPage").mock(side_effect=respond)
    results = await asyncio.gather(*(client.get_download_page(GbModel.MOD, mid) for mid in range(1, 5)))
    assert all(result[0].file_id == 11 for result in results)
    assert len(starts) == 4
    assert min(later - earlier for earlier, later in zip(starts, starts[1:])) >= 0.03
