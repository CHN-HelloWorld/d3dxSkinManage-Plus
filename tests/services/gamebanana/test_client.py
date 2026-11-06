import json
from pathlib import Path

import httpx
import pytest

from src.services.gamebanana.client import GameBananaClient
from src.services.gamebanana.models import GbModel

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
async def client(tmp_path):
    # 每个用例使用独立的持久化缓存目录，避免历史缓存绕过当前网络测试路由。
    from src.services.gamebanana.cache import GbApiCache

    c = GameBananaClient(
        max_concurrency=4, request_interval_ms=0, cache=GbApiCache(persist_dir=tmp_path / "gb_api_cache")
    )
    yield c
    await c.aclose()


async def test_categories_happy(client, respx_mock):
    # 分类来自游戏详情响应中的根分类字段。
    respx_mock.get(url__regex=r".*/Game/101/ProfilePage.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "_idRow": 101,
                "_aModRootCategories": [
                    {"_idRow": 5000, "_sName": "Skins", "_nItemCount": 1234},
                    {"_idRow": 5001, "_sName": "Effects", "_nItemCount": 56},
                ],
            },
        )
    )
    cats = await client.categories(101)
    assert len(cats) == 2
    assert all(c.id > 0 for c in cats)


async def test_mods_by_game_filters_mod_index_and_preserves_sort(client, respx_mock):
    route = respx_mock.get("https://gamebanana.com/apiv11/Mod/Index").mock(return_value=httpx.Response(200, json=[]))
    records, has_more = await client.mods_by_game(101, page=2, sort="latest")
    params = route.calls.last.request.url.params
    assert params["_aFilters[Generic_Game]"] == "101"
    assert params["_nPage"] == "2"
    assert "_aFilters[Generic_Category]" not in params
    assert "_sSort" in params
    assert records == []
    assert not has_more


async def test_requests_identify_current_application_version(client, respx_mock):
    from src.utils.constants import APP_VERSION

    route = respx_mock.get("https://gamebanana.com/apiv11/ModCategory/5000/SubCategories").mock(
        return_value=httpx.Response(200, json=[])
    )

    await client.sub_categories(5000)

    assert route.calls.last.request.headers["User-Agent"] == f"d3dxSkinManagePlus-GB/{APP_VERSION}"


async def test_sub_categories_returns_children(client, respx_mock):
    # 官方端点 /apiv11/ModCategory/{id}/SubCategories 返回的是数组，
    # 字段是 _sName / _sUrl / _sIconUrl / _nItemCount —— **没有** _idRow，
    # 叶子 id 要从 _sUrl 末段（.../cats/{id}）解析。
    respx_mock.get(url__regex=r".*/ModCategory/5000/SubCategories.*").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "_sName": "Characters",
                    "_sUrl": "https://gamebanana.com/mods/cats/5100",
                    "_sIconUrl": "https://images.gamebanana.com/img/ico/x.png",
                    "_nItemCount": 1234,
                },
                {
                    "_sName": "Weapons",
                    "_sUrl": "https://gamebanana.com/mods/cats/5101",
                    "_sIconUrl": None,
                    "_nItemCount": 88,
                },
            ],
        )
    )
    subs = await client.sub_categories(5000)
    assert len(subs) == 2
    # 字母序排序：Characters < Weapons
    assert subs[0].id == 5100 and subs[0].name == "Characters"
    assert subs[0].parent_id == 5000
    assert subs[0].item_count == 1234  # 分类数量应来自接口响应。
    assert subs[1].id == 5101 and subs[1].name == "Weapons"
    assert subs[1].item_count == 88


async def test_sub_categories_propagates_http_failure(client, respx_mock):
    respx_mock.get(url__regex=r".*/ModCategory/5000/SubCategories.*").mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        await client.sub_categories(5000)


async def test_failed_categories_are_not_cached_and_retry_recovers(client, respx_mock):
    from src.services.gamebanana.category_service import GbCategoryService

    route = respx_mock.get(url__regex=r".*/ModCategory/5000/SubCategories.*")
    route.mock(side_effect=httpx.ConnectError("offline"))
    service = GbCategoryService(client)
    with pytest.raises(httpx.ConnectError):
        await service.children(5000)
    route.mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "_sName": "Characters",
                    "_sUrl": "https://gamebanana.com/mods/cats/5100",
                }
            ],
        )
    )
    assert (await service.children(5000))[0].id == 5100


@pytest.mark.parametrize("payload", [None, {}, {"_sErrorCode": "FAILED"}])
async def test_sub_categories_rejects_malformed_response(client, respx_mock, payload):
    respx_mock.get(url__regex=r".*/ModCategory/5000/SubCategories.*").mock(
        return_value=httpx.Response(200, content=json.dumps(payload))
    )
    with pytest.raises(ValueError):
        await client.sub_categories(5000)


async def test_sub_categories_accepts_successful_empty_list(client, respx_mock):
    respx_mock.get(url__regex=r".*/ModCategory/5000/SubCategories.*").mock(return_value=httpx.Response(200, json=[]))
    assert await client.sub_categories(5000) == []


async def test_sub_categories_skips_records_without_url_id(client, respx_mock):
    respx_mock.get(url__regex=r".*/ModCategory/5000/SubCategories.*").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"_sName": "GoodEntry", "_sUrl": "https://gamebanana.com/mods/cats/777", "_nItemCount": 5},
                {"_sName": "BadEntry", "_sUrl": "not-a-url"},
                {"_sName": "AlsoBad"},  # 缺 _sUrl
            ],
        )
    )
    subs = await client.sub_categories(5000)
    assert len(subs) == 1
    assert subs[0].id == 777


async def test_get_profile_happy(client, respx_mock):
    respx_mock.get(url__regex=r".*/Mod/123456/ProfilePage.*").mock(
        return_value=httpx.Response(200, json=load("profile_sample.json"))
    )
    p = await client.get_profile(GbModel.MOD, 123456)
    assert p.mod_id == 123456
    assert len(p.files) >= 1


async def test_search_returns_records_and_has_more(client, respx_mock):
    respx_mock.get(url__regex=r".*/Util/Search/Results.*").mock(
        return_value=httpx.Response(200, json=load("search_sample.json"))
    )
    records, has_more = await client.search("test", game_id=101, page=1)
    assert isinstance(records, list)
    assert isinstance(has_more, bool)


async def test_429_retries_with_backoff(client, respx_mock, monkeypatch):
    async def _no_sleep(*a, **kw):
        return None

    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    route = respx_mock.get(url__regex=r".*ProfilePage.*").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json=load("profile_sample.json")),
        ]
    )
    p = await client.get_profile(GbModel.MOD, 123456)
    assert p.mod_id == 123456
    assert route.call_count == 2


@pytest.mark.parametrize("header", ["Deprecation", "Sunset", "X-Deprecated"])
async def test_name_match_preserves_results_and_reports_deprecation(client, respx_mock, caplog, header):
    records = [{"_idRow": 101, "_sName": "Example Game"}]
    route = respx_mock.get("https://gamebanana.com/apiv11/Util/Game/NameMatch", params={"_sName": "Example Game"}).mock(
        return_value=httpx.Response(200, json=records, headers={header: "2026-12-31"})
    )

    assert await client.match_game("Example Game") == records
    assert route.call_count == 1
    assert header.lower() in caplog.text.lower()
    assert "2026-12-31" in caplog.text


async def test_concurrency_capped(respx_mock):
    import asyncio

    c = GameBananaClient(max_concurrency=2, request_interval_ms=0)
    try:
        inflight = {"n": 0, "max": 0}

        async def side(request):
            inflight["n"] += 1
            inflight["max"] = max(inflight["max"], inflight["n"])
            await asyncio.sleep(0.05)
            inflight["n"] -= 1
            return httpx.Response(200, json={"_aRecords": [], "_aMetadata": {"_bIsComplete": True}})

        respx_mock.get(url__regex=r".*Subfeed.*").mock(side_effect=side)
        await asyncio.gather(*[c.subfeed(101, page=p) for p in range(1, 6)])
        assert inflight["max"] <= 2
    finally:
        await c.aclose()
