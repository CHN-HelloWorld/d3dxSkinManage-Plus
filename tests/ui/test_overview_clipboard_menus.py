import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QMenu

from src.models.mod import Mod
from src.ui.pages.overview import OverviewPage
from tests.qt_dialogs import interact


@pytest.mark.parametrize("surface", ["category", "mod", "table_category", "table_mod", "empty", "table_empty"])
def test_context_menus_move_selected_items(qtbot, temp_data_dir, monkeypatch, surface):
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.show()
    page.set_characters([{"id": "cat", "name": "Category", "parent_id": ""}])
    page.set_current_mods([Mod(mod_id="mod", name="Mod", folder_name="Mod")])
    qtbot.waitUntil(lambda: "cat" in page._vs_card_cache and "mod" in page._vs_card_cache)
    calls = []
    monkeypatch.setattr(page, "_cut_selected", lambda: calls.append("cut"))
    monkeypatch.setattr(page, "_paste_items", lambda: calls.append("paste"))
    page._clipboard_cat_ids = ["elsewhere"]
    page._clipboard_source_parent = "source"

    def exec_menu(menu):
        actions = {action.text(): action for action in menu.actions()}
        paste = actions["粘贴"]
        assert paste.isEnabled() and not paste.icon().isNull()
        paste.trigger()
        if surface not in ("empty", "table_empty"):
            cut = actions["剪切"]
            assert cut.isEnabled() and not cut.icon().isNull()
            cut.trigger()
        return None

    with interact(QMenu, exec_menu):
        if surface == "category":
            page._vs_card_cache["cat"]._show_context_menu(QPoint(10, 10))
        elif surface == "mod":
            page._show_mod_card_context_menu(page._vs_card_cache["mod"], "mod", QPoint(10, 10))
        elif surface.startswith("table"):
            page._set_view("list")
            if surface == "table_empty":
                pos = QPoint(page._table.viewport().width() - 2, page._table.viewport().height() - 2)
            else:
                wanted = "cat" if surface == "table_category" else "mod"
                row = next(i for i, item in enumerate(page._display_items) if item["id"] == wanted)
                pos = page._table.visualItemRect(page._table.item(row, 0)).center()
            page._show_table_context_menu(pos)
        else:
            page._show_empty_area_context_menu(QPoint(10, 10))
    assert calls == (["paste"] if surface in ("empty", "table_empty") else ["paste", "cut"])


def test_unselected_preview_has_noninteractive_empty_outline(qtbot, temp_data_dir):
    page = OverviewPage()
    qtbot.addWidget(page)
    page._hide_panel()
    label = page._panel_preview._image_label
    assert "dashed" in label.styleSheet()
    assert "transparent" in label.styleSheet()
    assert label.text() == "选择项目查看预览图"
    assert not page._panel_preview.acceptDrops()
    page.retheme()
    assert "dashed" in label.styleSheet()
    assert "transparent" in label.styleSheet()
    assert not page._panel_preview.acceptDrops()


def test_menu_cut_then_paste_moves_selection_to_current_directory(qtbot, temp_data_dir):
    page = OverviewPage()
    qtbot.addWidget(page)
    page._display_items = [{"id": "cat", "type": "category"}, {"id": "mod", "type": "mod"}]
    page._selected_ids = {"cat", "mod"}
    page._current_parent_id = "source"
    menu = QMenu(page)
    page._add_item_clipboard_actions(menu)
    cut, paste = menu.actions()
    assert not paste.isEnabled()
    cut.trigger()
    assert page._clipboard_cat_ids == ["cat"]
    assert page._clipboard_mod_ids == ["mod"]
    same_dir = QMenu(page)
    page._add_item_clipboard_actions(same_dir)
    assert not same_dir.actions()[1].isEnabled()
    page._current_parent_id = "destination"
    destination = QMenu(page)
    page._add_item_clipboard_actions(destination, include_cut=False)
    moves = []
    page.items_move_requested.connect(lambda cats, mods, target: moves.append((list(cats), list(mods), target)))
    destination.actions()[0].trigger()
    assert moves == [(["cat"], ["mod"], "destination")]
    assert not page._clipboard_cat_ids and not page._clipboard_mod_ids
