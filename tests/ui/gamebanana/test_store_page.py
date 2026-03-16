from types import SimpleNamespace

import pytest

from src.core.game_config_store import GameConfigStore
from src.services.gamebanana.models import (
    GbAuthor,
    GbCategory,
    GbModel,
    GbModProfile,
    GbModRecord,
)
from src.ui.pages.gamebanana_store import GameBananaStorePage
from tests.helpers import reset_game_service


def _rec(rid):
    return GbModRecord(
        mod_id=rid,
        model=GbModel.MOD,
        name=f"Mod {rid}",
        submitter=GbAuthor(id=1, name="A", avatar_url="", profile_url=""),
        thumbnail_url="",
        page_url=f"https://gamebanana.com/mods/{rid}",
        view_count=10,
        like_count=1,
        download_count=0,
        comment_count=0,
        is_nsfw=False,
        date_updated_ts=0,
    )


@pytest.fixture
def store_page(qtbot, temp_data_dir, fake_gb_client, fake_image_loader, monkeypatch, native_qt_teardown):
    """隔离游戏配置，并显式提供已匹配游戏的名称缓存。"""
    reset_game_service()
    game_names = {
        "game_a": ("示例游戏甲", "Example Game A"),
        "game_b": ("示例游戏乙", "Example Game B"),
        "game_online": ("示例在线游戏", "Example Online Game"),
    }
    GameConfigStore().set_gamebanana_overrides({"example game a": 101, "example game b": 102})

    def _fake_get_game(self, gid):
        info = game_names.get(gid)
        if not info:
            return None
        return SimpleNamespace(id=gid, name=info[0], name_en=info[1])

    monkeypatch.setattr("src.core.game_service.GameService.get_game", _fake_get_game)
    fake_gb_client.categories.return_value = [
        GbCategory(id=5000, name="Skins", parent_id=None, item_count=100, icon_url=None),
    ]
    fake_gb_client.sub_categories.return_value = []
    fake_gb_client.mods_by_category.return_value = ([_rec(1), _rec(2)], False)
    # 未匹配游戏通过在线名称查询解析；默认无候选时判定为未收录。
    fake_gb_client.match_game.return_value = []
    page = GameBananaStorePage(
        client=fake_gb_client,
        image_loader=fake_image_loader,
        local_game_id="game_a",
    )
    qtbot.addWidget(page)
    page.show()
    yield page
    page.shutdown()
    page.close()
    reset_game_service()


def test_initial_load_renders_browse_view(qtbot, store_page):
    qtbot.wait_until(lambda: store_page.grid.count() >= 2, timeout=3000)
    assert store_page.stack.currentIndex() == GameBananaStorePage.VIEW_MAIN


def test_category_failure_stays_in_dropdown_and_retry_recovers(qtbot, store_page, monkeypatch):
    import asyncio

    import httpx

    from src.services.gamebanana.client import GameBananaClient

    qtbot.wait_until(lambda: store_page.grid.count() >= 2, timeout=3000)
    client = GameBananaClient(request_interval_ms=0)
    offline = [True]

    async def send(request, **kwargs):
        if offline[0]:
            raise httpx.ConnectError("network offline", request=request)
        return httpx.Response(
            200,
            request=request,
            json=[
                {
                    "_sName": "Characters",
                    "_sUrl": "https://gamebanana.com/mods/cats/5100",
                    "_nItemCount": 1,
                }
            ],
        )

    monkeypatch.setattr(client._client, "send", send)
    store_page._cat_service._client = client
    bar = store_page.breadcrumb
    bar._populate_menu(2)
    picker = bar._pickers[2]
    qtbot.wait_until(lambda: picker._stack.currentWidget() == picker._error, timeout=8000)
    assert not picker._retry.icon().isNull()
    assert store_page.grid.count() == 2
    offline[0] = False
    picker._retry.click()
    qtbot.wait_until(lambda: picker._stack.currentWidget() == picker._list, timeout=3000)
    assert picker._all[0].id == 5100
    store_page._on_children_error(5000, "old failure", store_page._request_gen - 1)
    assert picker._stack.currentWidget() == picker._list
    asyncio.run(client.aclose())


def test_set_game_unknown_switches_to_unknown_view(qtbot, store_page):
    # 等待在线名称匹配完成；无候选时才显示未收录状态。
    store_page.set_game("my_homemade_game")
    qtbot.wait_until(
        lambda: store_page.stack.currentIndex() == GameBananaStorePage.VIEW_UNKNOWN,
        timeout=3000,
    )
    assert "my_homemade_game" in store_page.unknown_view.message_label.text()


def test_set_game_known_switches_back_to_browse(qtbot, store_page):
    store_page.set_game("my_homemade_game")
    qtbot.wait_until(
        lambda: store_page.stack.currentIndex() == GameBananaStorePage.VIEW_UNKNOWN,
        timeout=3000,
    )
    store_page.set_game("game_b")
    assert store_page.stack.currentIndex() == GameBananaStorePage.VIEW_MAIN


def test_card_click_switches_to_detail_with_loading_state(qtbot, store_page, fake_gb_client):
    qtbot.wait_until(lambda: store_page.grid.count() >= 2, timeout=3000)
    fake_gb_client.get_profile.return_value = GbModProfile(
        mod_id=1,
        model=GbModel.MOD,
        name="x",
        submitter=GbAuthor(id=1, name="A", avatar_url="", profile_url=""),
        thumbnail_url="",
        page_url="",
        view_count=0,
        like_count=0,
        download_count=0,
        comment_count=0,
        is_nsfw=False,
        date_updated_ts=0,
    )
    store_page._on_card_clicked(_rec(1))
    # 详情浮层覆盖浏览视图，主视图索引保持不变。
    assert store_page.detail_view.isVisible()
    assert store_page.stack.currentIndex() == GameBananaStorePage.VIEW_MAIN
    assert store_page.detail_view.title_label.text() == "Mod 1"


def test_back_button_returns_to_browse(qtbot, store_page, fake_gb_client):
    fake_gb_client.get_profile.return_value = GbModProfile(
        mod_id=1,
        model=GbModel.MOD,
        name="x",
        submitter=GbAuthor(id=1, name="A", avatar_url="", profile_url=""),
        thumbnail_url="",
        page_url="",
        view_count=0,
        like_count=0,
        download_count=0,
        comment_count=0,
        is_nsfw=False,
        date_updated_ts=0,
    )
    store_page._on_card_clicked(_rec(1))
    store_page._on_back_to_browse()
    # 关闭动画后，返回操作同步隐藏详情浮层。
    assert not store_page.detail_view.isVisible()
    assert store_page.stack.currentIndex() == GameBananaStorePage.VIEW_MAIN


def test_search_triggers_search_api(qtbot, store_page, fake_gb_client):
    fake_gb_client.search.return_value = ([_rec(99)], False)
    store_page._on_search("sigma")
    qtbot.wait_until(lambda: fake_gb_client.search.called, timeout=3000)


def test_uncached_game_waits_for_match_with_loading_view(qtbot, store_page, fake_gb_client):
    """在线名称查询未完成时保留加载状态，避免提前报告游戏未收录。"""
    import asyncio
    import threading

    entered = threading.Event()
    release = threading.Event()

    async def pending_match(_query):
        entered.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return []

    fake_gb_client.match_game.side_effect = pending_match
    try:
        store_page.set_game("game_online")
        qtbot.wait_until(entered.is_set, timeout=3000)
        assert store_page.stack.currentIndex() == GameBananaStorePage.VIEW_MAIN
        assert store_page.browse_view.stack.currentWidget() is store_page.loading_state
        assert store_page.grid.count() == 0
        assert not store_page.unknown_view.isVisible()
    finally:
        release.set()


def test_online_match_error_is_retryable_and_success_is_cached(qtbot, store_page, fake_gb_client):
    """名称查询网络失败后，刷新可重新匹配并保存当前名称的编号。"""
    import httpx

    fake_gb_client.match_game.side_effect = [
        httpx.ConnectError("network offline"),
        [{"_idRow": 201, "_sName": "Example Online Game"}],
    ]
    store_page.set_game("game_online")

    qtbot.wait_until(lambda: store_page.error_bar.isVisible(), timeout=3000)
    assert store_page.stack.currentIndex() == GameBananaStorePage.VIEW_MAIN
    assert store_page.browse_view.stack.currentWidget() is not store_page.loading_state
    assert not store_page.unknown_view.isVisible()
    assert "example online game" not in GameConfigStore().get_gamebanana_overrides()

    store_page.filter_bar.refresh_btn.click()

    qtbot.wait_until(lambda: store_page._gb_game_id == 201 and store_page.grid.count() == 2, timeout=3000)
    assert store_page.stack.currentIndex() == GameBananaStorePage.VIEW_MAIN
    assert fake_gb_client.match_game.await_args_list[0].args == ("Example Online Game",)
    assert fake_gb_client.match_game.await_count == 2
    assert GameConfigStore().get_gamebanana_overrides()["example online game"] == 201
    assert not store_page.error_bar.isVisible()


def test_proxy_change_retries_current_unresolved_game(qtbot, store_page, fake_gb_client):
    """代理更新完成后重新匹配当前游戏，并持久化在线结果。"""
    import asyncio
    import threading

    proxy_started = threading.Event()
    proxy_release = threading.Event()
    proxy_updated = threading.Event()

    async def match_game(_query):
        if fake_gb_client.match_game.await_count == 1:
            return []
        assert proxy_updated.is_set(), "名称查询必须等待代理更新完成"
        return [{"_idRow": 201, "_sName": "Example Online Game"}]

    async def update_proxy(proxy):
        proxy_started.set()
        while not proxy_release.is_set():
            await asyncio.sleep(0.01)
        fake_gb_client.proxy = proxy
        proxy_updated.set()

    fake_gb_client.match_game.side_effect = match_game
    fake_gb_client.update_proxy.side_effect = update_proxy
    fake_gb_client.proxy = None
    store_page.set_game("game_online")
    qtbot.wait_until(lambda: store_page.stack.currentIndex() == GameBananaStorePage.VIEW_UNKNOWN, timeout=3000)

    try:
        store_page.apply_proxy("http://127.0.0.1:8123")
        qtbot.wait_until(proxy_started.is_set, timeout=3000)
        assert fake_gb_client.match_game.await_count == 1
        proxy_release.set()

        qtbot.wait_until(lambda: store_page._gb_game_id == 201 and store_page.grid.count() == 2, timeout=3000)
        fake_gb_client.update_proxy.assert_awaited_once_with("http://127.0.0.1:8123")
        assert fake_gb_client.match_game.await_count == 2
        assert GameConfigStore().get_gamebanana_overrides()["example online game"] == 201
    finally:
        proxy_release.set()


def test_empty_online_match_switches_to_unknown_view(qtbot, store_page, fake_gb_client):
    """名称查询成功但没有候选时显示未收录提示。"""
    fake_gb_client.match_game.return_value = []

    store_page.set_game("game_online")

    qtbot.wait_until(lambda: fake_gb_client.match_game.await_count == 1, timeout=3000)
    qtbot.wait_until(lambda: store_page.stack.currentIndex() == GameBananaStorePage.VIEW_UNKNOWN, timeout=3000)
    assert "Example Online Game" in store_page.unknown_view.message_label.text()
    assert "example online game" not in GameConfigStore().get_gamebanana_overrides()


def test_stale_online_match_failure_does_not_replace_current_game(qtbot, store_page):
    """游戏切换后，旧请求的失败结果不得覆盖新游戏的列表。"""
    store_page.set_game("game_online")
    previous_generation = store_page._request_gen
    store_page.set_game("game_b")
    qtbot.wait_until(lambda: store_page._gb_game_id == 102 and store_page.grid.count() == 2, timeout=3000)

    store_page._on_game_match_failed("game_online", previous_generation, "network unavailable")

    assert store_page._gb_game_id == 102
    assert store_page.stack.currentIndex() == GameBananaStorePage.VIEW_MAIN
    assert store_page.grid.count() == 2
    assert not store_page.error_bar.isVisible()


def test_reset_then_refresh_does_not_query_the_previous_game(qtbot, store_page, fake_gb_client):
    """清空游戏后，刷新不得重新发起已删除游戏的名称查询。"""
    store_page.set_game("game_online")
    qtbot.wait_until(lambda: store_page.stack.currentIndex() == GameBananaStorePage.VIEW_UNKNOWN, timeout=3000)
    fake_gb_client.match_game.reset_mock()

    store_page.reset_to_empty()
    store_page.refresh()

    qtbot.wait(50)
    fake_gb_client.match_game.assert_not_awaited()
    assert store_page._gb_game_id is None
    assert store_page.stack.currentIndex() == GameBananaStorePage.VIEW_MAIN
    assert store_page.browse_view.stack.currentWidget() is store_page.browse_view.empty_state


def test_reset_while_hidden_discards_the_pending_game(qtbot, store_page, fake_gb_client):
    """隐藏期间清空游戏后，再次显示商店不得恢复旧查询。"""
    store_page.hide()
    store_page.set_game("game_online")
    store_page.reset_to_empty()

    store_page.show()

    qtbot.wait(50)
    fake_gb_client.match_game.assert_not_awaited()
    assert store_page._gb_game_id is None
    assert store_page.stack.currentIndex() == GameBananaStorePage.VIEW_MAIN
    assert store_page.browse_view.stack.currentWidget() is store_page.browse_view.empty_state
