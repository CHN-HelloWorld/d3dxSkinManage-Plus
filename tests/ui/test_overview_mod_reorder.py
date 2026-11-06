"""验证 Mod 卡片及列表拖动共用持久化顺序，并保留类别分组。"""

from dataclasses import replace

import pytest

from src.models.mod import Mod, ModStatus
from src.ui.pages.overview import OverviewPage


@pytest.fixture
def page(qtbot, temp_data_dir):
    """建立类别和 Mod 混合视图，使用稳定的中性标识。"""
    result = OverviewPage()
    qtbot.addWidget(result)
    result.resize(1000, 650)
    result.set_current_game("game.a")
    result.set_characters([{"id": "category-a", "name": "Category"}])
    result.set_current_mods(
        [Mod(mod_id=f"mod-{suffix}", name=name) for suffix, name in (("a", "Alpha"), ("b", "Beta"), ("c", "Charlie"))]
    )
    result._sort_combo.setCurrentIndex(result._sort_combo.findData("custom"))
    yield result
    result.close()


def _ids(page):
    """返回实际展示的类别及 Mod 标识。"""
    return [item["id"] for item in page._display_items]


@pytest.mark.parametrize("view_mode", ["grid", "list"])
def test_mod_reorder_saves_and_survives_view_changes_and_restart(page, qtbot, view_mode):
    page._set_view(view_mode)
    assert page._can_reorder_mods()
    assert page._move_mod_before("mod-c", "mod-a")
    expected = ["category-a", "mod-c", "mod-a", "mod-b"]
    assert _ids(page) == expected
    page._set_view("grid" if view_mode == "list" else "list")
    assert _ids(page) == expected
    restored = OverviewPage()
    qtbot.addWidget(restored)
    restored.set_current_game("game.a")
    restored.set_characters(page._all_characters)
    restored.set_current_mods(page._current_mods)
    assert _ids(restored) == expected


@pytest.mark.parametrize("view_mode", ["grid", "list"])
def test_mod_delta_deletion_addition_rename_and_enable_keep_survivor_order(page, view_mode):
    page._set_view(view_mode)
    assert page._move_mod_before("mod-c", "mod-a")
    changed = replace(page._current_mods[2], name="Renamed", status=ModStatus.ENABLED)
    added = Mod(mod_id="mod-new", name="A new Mod")
    assert page.apply_reconcile_delta([changed, added], ("mod-a",))
    assert _ids(page) == ["category-a", "mod-c", "mod-b", "mod-new"]


@pytest.mark.parametrize("blocked_by", ["search", "type", "loading", "builtin"])
def test_mod_reorder_rejects_filtered_loading_and_builtin_views(page, blocked_by):
    if blocked_by == "search":
        page._search_input.setText("Alpha")
    elif blocked_by == "type":
        page._type_filter.setCurrentIndex(page._type_filter.findData("mod"))
    elif blocked_by == "loading":
        page.begin_loading()
    else:
        page._sort_combo.setCurrentIndex(page._sort_combo.findData("alpha_asc"))
    before = page._category_order.path.read_bytes()
    assert not page._can_reorder_mods()
    assert not page._move_mod_before("mod-c", "mod-a")
    assert page._category_order.path.read_bytes() == before


def test_mod_and_category_cannot_be_dragged_across_groups(page):
    assert not page._move_mod_before("mod-c", "category-a")
    assert not page._move_category_before("category-a", "mod-a")
