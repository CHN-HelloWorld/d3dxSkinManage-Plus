from __future__ import annotations

from src.models.mod import ModInfo
from tests.helpers import make_mod
from tests.ui.tag_helpers import TAG_MOD_IDS, displayed_mod_ids
from tests.ui.tag_helpers import tag_page as tag_page


def test_text_search_includes_tag_substrings_in_current_subtree(tag_page):
    page, _manager, _tags = tag_page
    page._search_input.setText("高清")
    page._execute_search_refresh(force_text="高清")
    assert set(displayed_mod_ids(page)) == {TAG_MOD_IDS["one"], TAG_MOD_IDS["two"], TAG_MOD_IDS["similar"]}
    page._set_view("list")
    assert set(displayed_mod_ids(page)) == {TAG_MOD_IDS["one"], TAG_MOD_IDS["two"], TAG_MOD_IDS["similar"]}


def test_exact_tag_filter_searches_subtree_without_matching_similar_names(tag_page):
    page, _manager, tags = tag_page
    page.select_tag_filter(tags["高清材质"])
    assert set(displayed_mod_ids(page)) == {TAG_MOD_IDS["one"], TAG_MOD_IDS["two"]}
    assert all(entry["type"] == "mod" for entry in page._display_items)
    page._set_view("list")
    assert set(displayed_mod_ids(page)) == {TAG_MOD_IDS["one"], TAG_MOD_IDS["two"]}


def test_all_any_and_text_rating_intersections(tag_page):
    page, _manager, tags = tag_page
    page.select_tag_filter(tags["高清材质"])
    page.select_tag_filter(tags["服装"])
    assert displayed_mod_ids(page) == [TAG_MOD_IDS["one"]]
    page._tag_filter.set_match_mode("any")
    assert set(displayed_mod_ids(page)) == {TAG_MOD_IDS["one"], TAG_MOD_IDS["two"]}
    page._type_filter.setCurrentIndex(page._type_filter.findData("mod"))
    page._age_filter.setCurrentIndex(page._age_filter.findData("X"))
    assert displayed_mod_ids(page) == [TAG_MOD_IDS["two"]]
    page._search_input.setText("Second")
    page._execute_search_refresh(force_text="Second")
    assert displayed_mod_ids(page) == [TAG_MOD_IDS["two"]]
    page._search_input.setText("First")
    page._execute_search_refresh(force_text="First")
    assert displayed_mod_ids(page) == []


def test_filter_counts_are_saved_game_wide_despite_active_filters(tag_page):
    page, _manager, tags = tag_page
    page.refresh_tags()
    page._search_input.setText("First")
    page._execute_search_refresh(force_text="First")
    page.select_tag_filter(tags["服装"])
    hd = page._tag_filter.catalog[tags["高清材质"]]
    assert hd.usage_count == 3
    assert tags["高清材质"] in {tag.id for tag in page._tag_filter.available_tags()}


def test_clearing_tags_preserves_text_mode_and_sort(tag_page):
    page, _manager, tags = tag_page
    page._search_input.setText("First")
    page._execute_search_refresh(force_text="First")
    page.select_tag_filter(tags["高清材质"])
    page._tag_filter.set_match_mode("any")
    sort = page._sort_key
    page._tag_filter.clear()
    assert page._search_input.text() == "First"
    assert page._tag_filter.match_mode == "any"
    assert page._sort_key == sort
    assert page._tag_filter.selected_ids == []
    assert page._tag_filter.selection_row.isHidden()


def test_navigation_and_game_switch_clear_only_tag_selection(tag_page):
    page, manager, tags = tag_page
    page.select_tag_filter(tags["高清材质"])
    page._tag_filter.set_match_mode("any")
    page._navigate_to(manager.get_mod(TAG_MOD_IDS["two"]).node_id)
    assert page._tag_filter.selected_ids == []
    page.select_tag_filter(tags["高清材质"])
    manager.set_game("game-b")
    page.set_current_game("game-b")
    assert page._tag_filter.selected_ids == []
    assert page._tag_filter.match_mode == "any"
    assert page._tag_filter.catalog == {}


def test_category_types_disable_tag_filter_without_inheriting_mod_tags(tag_page):
    page, _manager, tags = tag_page
    page.select_tag_filter(tags["高清材质"])
    page._type_filter.setCurrentIndex(page._type_filter.findData("character"))
    assert not page._tag_filter.button.isEnabled()
    assert "Mod" in page._tag_filter.button.toolTip()
    assert displayed_mod_ids(page) == []
    assert [entry["id"] for entry in page._display_items] == [_manager.get_mod(TAG_MOD_IDS["two"]).node_id]


def test_shared_rename_refreshes_selected_identity_and_subtree_data(tag_page):
    page, manager, tags = tag_page
    page.select_tag_filter(tags["高清材质"])
    draft = manager.get_tag_service().begin_edit(manager.get_mod(TAG_MOD_IDS["one"]))
    draft.rename(tags["高清材质"], "新材质")
    result = manager.get_tag_service().commit_edit(draft)
    for mod in result.mods:
        page.update_mod_in_view(mod)
    page.refresh_tags()
    assert page._tag_filter.selected_ids == [tags["高清材质"]]
    assert page._tag_filter.catalog[tags["高清材质"]].name == "新材质"
    assert set(displayed_mod_ids(page)) == {TAG_MOD_IDS["one"], TAG_MOD_IDS["two"]}
    page._search_input.setText("新材质")
    page._execute_search_refresh(force_text="新材质")
    page._set_view("list")
    assert set(displayed_mod_ids(page)) == {TAG_MOD_IDS["one"], TAG_MOD_IDS["two"]}


def test_global_delete_prunes_selected_condition(tag_page):
    page, manager, tags = tag_page
    page.select_tag_filter(tags["高清材质"])
    draft = manager.get_tag_service().begin_edit(manager.get_mod(TAG_MOD_IDS["one"]))
    draft.remove_candidate(tags["高清材质"])
    result = manager.get_tag_service().commit_edit(draft)
    for mod in result.mods:
        page.update_mod_in_view(mod)
    page.refresh_tags()
    assert page._tag_filter.selected_ids == []
    assert tags["高清材质"] not in page._tag_filter.catalog


def test_new_import_updates_game_wide_filter_catalog(tag_page):
    page, manager, _tags = tag_page
    imported = make_mod(
        manager._index,
        "Imported",
        mod_id=TAG_MOD_IDS["imported"],
        node_id=page._current_parent_id,
        info=ModInfo(tags=["新标签"]),
    )
    assert manager._index.add_mod(imported)
    current = [
        manager.get_mod(TAG_MOD_IDS["one"]),
        manager.get_mod(TAG_MOD_IDS["similar"]),
        manager.get_mod(TAG_MOD_IDS["imported"]),
    ]
    page.set_current_mods(current, current + [manager.get_mod(TAG_MOD_IDS["two"])])
    assert "新标签" in {tag.name for tag in page._tag_filter.catalog.values()}


def test_text_normalization_is_shared_by_card_and_list(tag_page):
    page, _manager, _tags = tag_page
    page._search_input.setText("ＦＩＲＳＴ")
    page._execute_search_refresh(force_text="ＦＩＲＳＴ")
    assert displayed_mod_ids(page) == [TAG_MOD_IDS["one"]]
    page._set_view("list")
    assert displayed_mod_ids(page) == [TAG_MOD_IDS["one"]]


def test_popup_can_select_multiple_tags_and_escape_restores_button(tag_page, qtbot):
    from PySide6.QtCore import Qt

    from src.ui.widgets.tag_chip import TagChip

    page, _manager, tags = tag_page
    control = page._tag_filter
    qtbot.mouseClick(control.button, Qt.MouseButton.LeftButton)
    assert control._popup.isVisible()
    for name in ("高清材质", "服装"):
        chip = next(chip for chip in control._chips.findChildren(TagChip) if chip.tag.id == tags[name])
        qtbot.mouseClick(chip, Qt.MouseButton.LeftButton)
        assert control._popup.isVisible()
    assert displayed_mod_ids(page) == [TAG_MOD_IDS["one"]]
    control.clear()
    qtbot.keyClick(control._query, Qt.Key.Key_Escape)
    assert not control._popup.isVisible()
    assert not control.button.isChecked()


def test_empty_state_clear_actions_keep_dimensions_separate(tag_page):
    page, _manager, tags = tag_page
    page._search_input.setText("nothing-matches")
    page._execute_search_refresh(force_text="nothing-matches")
    page.select_tag_filter(tags["服装"])
    for mode in ("grid", "list"):
        page._set_view(mode)
        assert not page._filter_empty.isHidden()
        assert "服装" in page._filter_empty_description.text()
    page._empty_clear_tags.click()
    assert page._search_input.text() == "nothing-matches"
    assert page._tag_filter.selected_ids == []
    page._clear_all_conditions()
    assert page._filter_empty.isHidden()
    assert page._search_input.text() == ""
    assert set(displayed_mod_ids(page)) == {TAG_MOD_IDS["one"], TAG_MOD_IDS["similar"]}


def test_grid_filter_removes_hidden_selection_and_old_panel(tag_page):
    page, _manager, tags = tag_page
    page._on_mod_card_clicked(TAG_MOD_IDS["similar"])
    assert page._selected_mod.mod_id == TAG_MOD_IDS["similar"]
    page.select_tag_filter(tags["服装"])
    assert page._selected_ids == set()
    assert page._selected_mod is None
    assert page._panel_mode == "none"
    assert page._panel_edit_btn.isHidden()
    page._on_mod_card_clicked(TAG_MOD_IDS["one"])
    page._search_input.setText("nothing-matches")
    page._execute_search_refresh(force_text="nothing-matches")
    assert page._selected_ids == set()
    assert page._selected_mod is None
    assert "已选择" not in page._status_label.text()
    assert page._status_label.text() == "匹配 0 项"
