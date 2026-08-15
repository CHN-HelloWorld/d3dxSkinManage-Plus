from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from src.models.mod import Mod, ModInfo
from src.ui.widgets.mod_card import ModCard
from tests.ui.tag_helpers import TAG_MOD_IDS
from tests.ui.tag_helpers import tag_page as tag_page


def test_card_scales_capsules_inside_unchanged_height(qtbot, temp_data_dir):
    from src.ui.widgets.tag_chip import TagChip

    mod = Mod(
        game_id="game", mod_id=TAG_MOD_IDS["one"], name="Example", info=ModInfo(tags=["first", "second", "third"])
    )
    card = ModCard(mod, card_width=160, card_height=200)
    qtbot.addWidget(card)
    card.show()
    qtbot.wait(10)
    assert card.height() == 200
    assert len(card._tag_row.findChildren(TagChip)) == 1
    assert card._tag_row._more_button.text() == "+2"
    card.resize_card_fast(240, 300)
    assert card.height() == 300
    assert len(card._tag_row.findChildren(TagChip)) == 2
    assert card._tag_row._more_button.text() == "+1"
    card.resize_card_fast(128, 160)
    assert card._tag_row.isHidden()
    assert card.height() == 160


def test_capsule_click_does_not_select_or_toggle_card(qtbot, temp_data_dir):
    from src.ui.widgets.tag_chip import TagChip

    card = ModCard(
        Mod(game_id="game", mod_id=TAG_MOD_IDS["one"], info=ModInfo(tags=["first"])), card_width=160, card_height=200
    )
    qtbot.addWidget(card)
    card.show()
    clicks, doubles, toggles, tags = [], [], [], []
    card.clicked.connect(clicks.append)
    card.double_clicked.connect(doubles.append)
    card.toggle_requested.connect(toggles.append)
    card.tag_clicked.connect(tags.append)
    chip = card._tag_row.findChild(TagChip)
    qtbot.mouseClick(chip, Qt.MouseButton.LeftButton)
    qtbot.mouseDClick(chip, Qt.MouseButton.LeftButton)
    assert tags
    assert not clicks and not doubles and not toggles


def test_preview_has_all_tags_and_no_tag_edit_controls(tag_page, qtbot):
    from src.ui.widgets.tag_chip import TagChip

    page, manager, tags = tag_page
    page._selected_mod = manager.get_mod(TAG_MOD_IDS["one"])
    page._show_mod_panel(page._selected_mod)
    chips = page._panel_tags.findChildren(TagChip)
    assert len(chips) == 2
    assert all(chip.height() == 24 for chip in chips)
    qtbot.mouseClick(chips[0], Qt.MouseButton.LeftButton)
    assert tags["高清材质"] in page._tag_filter.selected_ids
    page._hide_panel()
    assert page._panel_tags.isHidden()


def test_list_keeps_five_columns_with_name_and_clickable_second_line(tag_page, qtbot):
    from src.ui.widgets.tag_chip import TagChip

    page, _manager, _tags = tag_page
    page._set_view("list")
    assert page._table.columnCount() == 5
    row = page._display_idx_map[TAG_MOD_IDS["one"]]
    assert page._table.item(row, 1).text() == "First"
    cell = page._table.cellWidget(row, 1)
    assert cell is not None
    chips = cell.findChildren(TagChip)
    assert len(chips) == 2
    qtbot.mouseClick(chips[0], Qt.MouseButton.LeftButton)
    assert page._tag_filter.selected_ids


def test_overflow_shows_every_legacy_tag_without_triggering_card_action(qtbot, temp_data_dir):
    from src.ui.widgets.tag_chip import TagChip

    names = [f"旧标签-{i}" for i in range(24)] + ["超长旧标签" * 10]
    card = ModCard(Mod(game_id="game", mod_id="legacy", info=ModInfo(tags=names)), card_width=160, card_height=200)
    qtbot.addWidget(card)
    card.show()
    actions, tags = [], []
    card.double_clicked.connect(actions.append)
    card.clicked.connect(actions.append)
    card.tag_clicked.connect(tags.append)
    qtbot.mouseClick(card._tag_row._more_button, Qt.MouseButton.LeftButton)
    popup = card._tag_row._popup
    assert popup.isVisible()
    chips = popup.findChildren(TagChip)
    assert [chip.tag.name for chip in chips] == names
    qtbot.mouseClick(chips[-1], Qt.MouseButton.LeftButton)
    assert not popup.isVisible()
    assert len(tags) == 1 and not actions
    assert card.height() == 200


def test_table_name_click_keeps_selection_and_double_click_behavior(tag_page, qtbot):
    page, _manager, _tags = tag_page
    page._set_view("list")
    qtbot.wait(10)
    row = page._display_idx_map[TAG_MOD_IDS["one"]]
    cell = page._table.cellWidget(row, 1)
    opened = []
    page.mod_toggle_requested.connect(opened.append)
    qtbot.mouseClick(cell, Qt.MouseButton.LeftButton, pos=cell._label.geometry().center())
    assert page._selected_ids == {TAG_MOD_IDS["one"]}
    qtbot.mouseDClick(cell, Qt.MouseButton.LeftButton, pos=cell._label.geometry().center())
    assert opened == [TAG_MOD_IDS["one"]]


def test_table_tag_cell_is_removed_when_same_row_becomes_category(tag_page):
    page, _manager, tags = tag_page
    page._set_view("list")
    page.select_tag_filter(tags["高清材质"])
    assert page._table.cellWidget(0, 1) is not None
    page._type_filter.setCurrentIndex(page._type_filter.findData("character"))
    assert page._table.item(0, 1).text() == "Nested"
    assert page._table.cellWidget(0, 1) is None


@pytest.mark.parametrize("view", ["grid", "list"])
def test_subtree_result_selection_targets_the_visible_mod(tag_page, view):
    page, _manager, tags = tag_page
    page._set_view(view)
    page.select_tag_filter(tags["高清材质"])
    for mod_id in (TAG_MOD_IDS["one"], TAG_MOD_IDS["two"]):
        if view == "grid":
            page._on_mod_card_clicked(mod_id)
        else:
            page._table.selectRow(page._display_idx_map[mod_id])
        assert page._selected_mod.mod_id == mod_id
    edits, toggles = [], []
    page.mod_edit_requested.connect(edits.append)
    page.mod_toggle_requested.connect(toggles.append)
    page._on_panel_edit()
    page._on_panel_toggle()
    assert edits == [TAG_MOD_IDS["two"]] and toggles == [TAG_MOD_IDS["two"]]
    assert page._panel_name_label.text() == "Second"


def test_subtree_context_selection_updates_preview_target(tag_page):
    page, _manager, tags = tag_page
    page._on_mod_card_clicked(TAG_MOD_IDS["one"])
    page.select_tag_filter(tags["高清材质"])
    page._select_for_context_menu(TAG_MOD_IDS["two"])
    assert page._selected_mod.mod_id == TAG_MOD_IDS["two"]
    assert page._panel_name_label.text() == "Second"
