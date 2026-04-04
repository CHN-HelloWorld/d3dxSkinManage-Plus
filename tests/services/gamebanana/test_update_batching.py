import json
from pathlib import Path

import httpx
import pytest

from src.services.gamebanana.cache import GbApiCache
from src.services.gamebanana.client import GameBananaClient
from src.services.gamebanana.models import GbInstallMetadata
from src.services.gamebanana.update_checker import ModUpdateChecker

_BASE = "https://gamebanana.com/apiv11"
_PROFILE = Path(__file__).parent / "fixtures" / "profile_sample.json"


@pytest.fixture
async def client(tmp_path):
    instance = GameBananaClient(
        request_interval_ms=0,
        cache=GbApiCache(persist_dir=tmp_path / "gb_api_cache"),
    )
    yield instance
    await instance.aclose()


def _profile(mod_id, timestamp):
    payload = json.loads(_PROFILE.read_text(encoding="utf-8"))
    payload.update(
        _idRow=mod_id,
        _tsDateUpdated=timestamp,
        _sProfileUrl=f"https://gamebanana.com/mods/{mod_id}",
    )
    return payload


def _file(md5="bbb"):
    payload = _profile(123, 2000)["_aFiles"][0]
    payload.update(_idRow=11, _tsDateAdded=2000, _sMd5Checksum=md5, _sVersion="v2")
    return payload


def _meta(mod_id, file_ts=1000):
    return GbInstallMetadata(
        mod_id=mod_id,
        file_id=10,
        file_md5="aaa",
        file_ts=file_ts,
        page_url=f"https://gamebanana.com/mods/{mod_id}",
        version="v1",
        submitter_id=1,
        submitter_name="A",
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


async def test_batch_files_reduce_requests_and_preserve_file_checks(client, respx_mock, qapp):
    def respond(request):
        if request.url.path.endswith("/Multi"):
            return httpx.Response(
                200,
                json=[
                    {
                        "_idRow": mid,
                        "_tsDateUpdated": 1000 if mid <= 12 else 2000,
                        "_aFiles": [
                            dict(_file("aaa" if mid == 13 else "bbb"), _tsDateAdded=1000 if mid <= 12 else 2000)
                        ],
                    }
                    for mid in range(1, 16)
                ],
            )
        mid = int(request.url.path.split("/")[-2])
        if request.url.path.endswith("/ProfilePage"):
            return httpx.Response(200, json=_profile(mid, 1000 if mid <= 12 else 2000))
        if request.url.path.endswith("/DownloadPage"):
            return httpx.Response(200, json={"_aFiles": [_file("aaa" if mid == 13 else "bbb")]})
        assert request.url.path.endswith("/Updates")
        return httpx.Response(200, json=[])

    route = respx_mock.get(url__regex=rf"{_BASE}/Mod/.*").mock(side_effect=respond)
    checker = ModUpdateChecker(client)
    events = _observe(checker)
    await checker._run_all([(f"mid{mid}", _meta(mid)) for mid in range(1, 16)], {"mid14": {11}}, False)

    assert events["finished"] == [{"found": 1, "up_to_date": 14, "failed": 0}]
    assert set(events["found"]) == {"mid15"}
    assert events["found"]["mid15"].page_url == "https://gamebanana.com/mods/15"
    assert events["progress"] == [(done, 15) for done in range(16)]
    assert route.call_count == 2  # 1 次批量文件 + 1 次真正有更新的日志。
    assert all(not call.request.url.path.endswith("/ProfilePage") for call in route.calls)


async def test_batch_missing_files_and_unknown_baselines_still_check_files(client, respx_mock, qapp):
    def respond(request):
        if request.url.path.endswith("/Multi"):
            return httpx.Response(
                200,
                json=[
                    {"_idRow": 1, "_tsDateUpdated": 0, "_aFiles": [_file()]},
                    {"_idRow": 3, "_tsDateUpdated": 0, "_aFiles": [_file()]},
                    {"_idRow": 4, "_tsDateUpdated": 1000, "_aFiles": [_file()]},
                ],
            )
        mid = int(request.url.path.split("/")[-2])
        if request.url.path.endswith("/ProfilePage"):
            return httpx.Response(200, json=_profile(mid, 0))
        if request.url.path.endswith("/DownloadPage"):
            return httpx.Response(200, json={"_aFiles": [_file()]})
        assert request.url.path.endswith("/Updates")
        return httpx.Response(200, json=[])

    route = respx_mock.get(url__regex=rf"{_BASE}/Mod/.*").mock(side_effect=respond)
    checker = ModUpdateChecker(client)
    events = _observe(checker)
    jobs = [(f"mid{mid}", _meta(mid, file_ts=0 if mid in (1, 4) else 1000)) for mid in range(1, 5)]
    await checker._run_all(jobs, {}, False)
    assert events["finished"] == [{"found": 4, "up_to_date": 0, "failed": 0}]
    profiles = {call.request.url.path for call in route.calls if call.request.url.path.endswith("/ProfilePage")}
    assert profiles == set()
    downloads = {call.request.url.path for call in route.calls if call.request.url.path.endswith("/DownloadPage")}
    assert downloads == {"/apiv11/Mod/2/DownloadPage"}


async def test_batch_failure_reports_individual_failures_without_fanout(client, respx_mock, qapp):
    def respond(request):
        if request.url.path.endswith("/Multi"):
            return httpx.Response(404)
        mid = int(request.url.path.split("/")[-2])
        if request.url.path.endswith("/ProfilePage"):
            if mid == 3:
                return httpx.Response(404)
            return httpx.Response(200, json=_profile(mid, 1000 if mid == 1 else 2000))
        if request.url.path.endswith("/DownloadPage"):
            return httpx.Response(200, json={"_aFiles": [_file()]})
        assert request.url.path.endswith("/Updates")
        return httpx.Response(200, json=[])

    route = respx_mock.get(url__regex=rf"{_BASE}/Mod/.*").mock(side_effect=respond)
    checker = ModUpdateChecker(client)
    events = _observe(checker)
    await checker._run_all([(f"mid{mid}", _meta(mid)) for mid in (1, 2, 3)], {}, False)
    assert events["finished"] == [{"found": 0, "up_to_date": 0, "failed": 3}]
    assert events["up_to_date"] == []
    assert events["found"] == {}
    assert set(events["failed"]) == {"mid1", "mid2", "mid3"}
    assert route.call_count == 1


async def test_batch_429_blocks_automatic_and_manual_checks_without_fallback(client, respx_mock, qapp):
    def respond(request):
        if request.url.path.endswith(("/Multi", "/ProfilePage")):
            return httpx.Response(429)
        if request.url.path.endswith("/DownloadPage"):
            return httpx.Response(200, json={"_aFiles": [_file()]})
        assert request.url.path.endswith("/Updates")
        return httpx.Response(200, json=[])

    route = respx_mock.get(url__regex=rf"{_BASE}/Mod/.*").mock(side_effect=respond)
    checker = ModUpdateChecker(client)
    events = _observe(checker)
    jobs = [("mid1", _meta(1)), ("mid2", _meta(2))]
    await checker._run_all(jobs, {}, False)
    assert events["finished"] == [{"found": 0, "up_to_date": 0, "failed": 2}]
    assert set(events["failed"]) == {"mid1", "mid2"}
    assert events["progress"][-1] == (2, 2)
    assert route.call_count == 1

    # 客户端共享静默状态，换一个 checker 也不能绕过。
    another = ModUpdateChecker(client)
    later_events = _observe(another)
    await another._run_all(jobs, {}, False)
    assert later_events["finished"] == [{"found": 0, "up_to_date": 0, "failed": 2}]
    assert route.call_count == 1

    await checker._run_all(jobs, {}, True)
    assert events["finished"][-1] == {"found": 0, "up_to_date": 0, "failed": 2}
    assert route.call_count == 1
