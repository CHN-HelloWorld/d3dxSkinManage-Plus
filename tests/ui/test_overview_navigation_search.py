"""验证总览导航结束旧搜索，并在加载失败时恢复已呈现的查询与位置。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.models.mod import Mod
from src.ui.overview_refresh import OverviewRefreshController
from src.ui.pages.overview import OverviewPage


@pytest.fixture
def library(qtbot, temp_data_dir, request):
    """使用真实页面和分批加载器，以内存目录记录隔离文件读取及查询范围。"""
    page = OverviewPage()
    qtbot.addWidget(page)
    request.addfinalizer(page.close)
    page.resize(900, 600)
    page.show()
    page.set_current_game("game_a")
    page.set_characters(
        [
            {"id": "source", "name": "上级目录"},
            {"id": "target", "name": "查找目录", "parent_id": "source"},
            {"id": "child", "name": "下一层", "parent_id": "target"},
            {"id": "outside", "name": "其他目录"},
        ]
    )
    page.set_current_mods([])
    all_mods = [Mod(mod_id=f"source-{i:03}", node_id="source", name=f"查找条目 {i:03}") for i in range(80)] + [
        Mod(mod_id="direct", node_id="target", name="普通资源"),
        Mod(mod_id="nested", node_id="child", name="更深资源"),
        Mod(mod_id="outside-mod", node_id="outside", name="外部资源"),
    ]
    subtrees = {
        "source": ["source", "target", "child"],
        "target": ["target", "child"],
        "child": ["child"],
        "outside": ["outside"],
    }
    requests, reads = [], []

    def current_scope():
        """让加载器读取页面当前的游戏、目录和搜索文字。"""
        return page._current_game_id, page._current_parent_id, page._search_input.text()

    def iter_mods(game_id, node_ids):
        """记录真实加载器请求的目录范围，并提供可关闭的记录迭代器。"""
        reads.append((game_id, None if node_ids is None else tuple(node_ids)))
        return (mod for mod in all_mods if node_ids is None or mod.node_id in node_ids)

    manager = SimpleNamespace(
        library_view_stamp=(1, 0),
        library_view_busy=lambda: False,
        get_subtree_ids=lambda parent, _game: subtrees[parent],
        _index=SimpleNamespace(iter_mods=iter_mods),
    )
    controller = OverviewRefreshController(
        manager,
        current=current_scope,
        apply=page.set_current_mods,
        on_loading=lambda _game: page.begin_loading(),
        on_error=page.loading_failed,
        recursive=lambda: bool(page._search_input.text().strip()),
        parent=page,
    )
    request.addfinalizer(controller.stop)

    def request_mods(*_args):
        """记录页面交给宿主的刷新次数，同时执行真实目录加载。"""
        requests.append(current_scope())
        controller.schedule()

    page.set_mod_loader(request_mods)
    page.category_navigated.connect(request_mods)
    return SimpleNamespace(page=page, controller=controller, manager=manager, requests=requests, reads=reads)


def _search(library, qtbot):
    """等待防抖查询提交，使后续导航离开一个真实呈现的搜索结果。"""
    page = library.page
    page._search_input.setText("查找")
    qtbot.waitUntil(
        lambda: (
            not page._search_timer.isActive()
            and not library.controller.active
            and page._presented_view.search_text == "查找"
        )
    )
    assert "target" in page._display_idx_map
    assert "direct" not in page._display_idx_map


def _open_category(page, category_id):
    """通过当前布局的双击信号进入搜索命中的类别。"""
    if page._view_mode == "grid":
        page._vs_card_cache[category_id].double_clicked.emit(category_id)
    else:
        page._table.cellDoubleClicked.emit(page._display_idx_map[category_id], 1)


@pytest.mark.parametrize("mode", ["grid", "list"])
@pytest.mark.parametrize("pending_search", [False, True])
def test_opening_search_result_loads_complete_directory_once(library, qtbot, mode, pending_search):
    """导航既清除已执行查询，也取消尚在防抖中的文字，且不加载更深层或外部内容。"""
    page = library.page
    page._set_view(mode)
    _search(library, qtbot)
    if pending_search:
        page._search_input.setText("尚未检索")
        assert page._search_timer.isActive()
    library.requests.clear()
    library.reads.clear()

    _open_category(page, "target")
    qtbot.waitUntil(lambda: not library.controller.active)

    assert set(page._display_idx_map) == {"child", "direct"}
    assert library.requests == [("game_a", "target", "")]
    assert library.reads == [("game_a", ("target",))]
    assert page._search_input.text() == page._pending_search_text == ""
    assert not page._filter_empty.isVisible()
    assert not page._search_timer.isActive()
    qtbot.wait(page._search_timer.interval() + 20)
    assert library.requests == [("game_a", "target", "")]


@pytest.mark.parametrize("mode", ["grid", "list"])
def test_navigation_cancels_search_started_during_pending_load(library, qtbot, mode):
    """连续导航必须取消加载期间新输入的防抖，防止目标目录再次收到刷新请求。"""
    page = library.page
    page._set_view(mode)
    _search(library, qtbot)
    page._navigate_to("source")
    assert library.controller.active and page._mods_loading
    page._search_input.setText("尚未检索")
    assert page._search_timer.isActive()
    library.requests.clear()

    page._navigate_to("target")

    assert not page._search_timer.isActive()
    qtbot.waitUntil(lambda: not library.controller.active)
    qtbot.wait(page._search_timer.interval() + 20)
    assert set(page._display_idx_map) == {"child", "direct"}
    assert library.requests == [("game_a", "target", "")]


@pytest.mark.parametrize("mode", ["grid", "list"])
@pytest.mark.parametrize("destination, expected", [("source", {"target"}), ("", {"source", "outside"})])
def test_breadcrumb_navigation_clears_search(library, qtbot, mode, destination, expected):
    page = library.page
    page._set_view(mode)
    page._navigate_to("target")
    qtbot.waitUntil(lambda: not library.controller.active)
    page._search_input.setText("普通")
    qtbot.waitUntil(lambda: not page._search_timer.isActive() and not library.controller.active)
    assert set(page._display_idx_map) == {"direct"}
    library.requests.clear()

    page._breadcrumb.navigate.emit(destination)
    qtbot.waitUntil(lambda: not library.controller.active)

    categories = {item["id"] for item in page._display_items if item["type"] == "category"}
    assert categories == expected
    assert page._search_input.text() == page._pending_search_text == ""
    assert library.requests == [("game_a", destination, "")]


@pytest.mark.parametrize("mode", ["grid", "list"])
def test_failed_navigation_restores_presented_search_and_position(library, qtbot, monkeypatch, mode):
    """失败时恢复已呈现查询，舍弃离开前尚未执行的输入并保留原滚动位置。"""
    page = library.page
    page._set_view(mode)
    page._navigate_to("source")
    qtbot.waitUntil(lambda: not library.controller.active)
    _search(library, qtbot)
    scrollbar = (page._table if mode == "list" else page._scroll_area).verticalScrollBar()
    scrollbar.setValue(20 if mode == "list" else 700)
    old_position = scrollbar.value()
    old_items = page._display_items
    assert old_position > 0

    def failed_read(*_args):
        """模拟目标目录在替换视图前读取失败。"""
        raise OSError("controlled directory read failure")

    monkeypatch.setattr(library.manager._index, "iter_mods", failed_read)
    page._search_input.setText("尚未检索")
    library.requests.clear()

    page._navigate_to("target")
    qtbot.waitUntil(lambda: not library.controller.active)

    assert page._current_parent_id == "source"
    assert page.get_current_path() == [("source", "上级目录")]
    assert page._search_input.text() == page._pending_search_text == "查找"
    assert page._display_items is old_items
    assert scrollbar.value() == old_position
    assert page._view_mode == mode
    assert not page._mods_loading and not page._search_timer.isActive()
    assert library.requests == [("game_a", "target", "")]


@pytest.mark.parametrize("mode", ["grid", "list"])
def test_refreshing_current_directory_keeps_search(library, qtbot, mode):
    page = library.page
    page._set_view(mode)
    _search(library, qtbot)
    old_ids = set(page._display_idx_map)
    library.requests.clear()

    page._request_mods()
    qtbot.waitUntil(lambda: not library.controller.active)

    assert set(page._display_idx_map) == old_ids
    assert page._search_input.text() == page._pending_search_text == "查找"
    assert library.requests == [("game_a", "", "查找")]
