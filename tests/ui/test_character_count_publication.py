from PySide6.QtGui import QColor, QPixmap

from src.ui.pages.overview import OverviewPage
from src.utils.i18n import t


def _page(qtbot, request):
    page = OverviewPage()
    qtbot.addWidget(page)
    request.addfinalizer(page.close)
    page.resize(900, 600)
    page.show()
    qtbot.wait(10)
    return page


def _forbid_rebuild(*args, **kwargs):
    raise AssertionError("count publication must preserve the current view and preview")


def test_deferred_character_update_refreshes_current_level_data(qtbot, temp_data_dir, request):
    page = _page(qtbot, request)
    original = [
        {"id": "parent", "name": "Parent", "enabled_count": 0},
        {"id": "child", "parent_id": "parent", "name": "Child", "enabled_count": 0},
    ]
    page._current_parent_id = "parent"
    page.set_characters(original)
    updated = [dict(row, enabled_count=2) for row in original]

    page.set_characters(updated, refresh=False)

    assert page._all_characters is updated
    assert page._children_map["parent"][0] is updated[1]
    assert page._characters is page._children_map["parent"]
    assert page._characters[0] is updated[1]
    assert page._display_items[0]["data"] is updated[1]


def test_count_publication_preserves_visible_card_and_preview(qtbot, temp_data_dir, request, monkeypatch):
    page = _page(qtbot, request)
    original = [
        {"id": f"cat-{i:03}", "name": f"Category {i:03}", "mod_count": 5, "enabled_count": 0} for i in range(60)
    ]
    page.set_characters(original)
    page._fresh_population = False
    page._vs_population_timer.stop()
    page._vs_update_visible()
    card_id, card = next(iter(page._vs_card_cache.items()))
    page._selected_ids = {card_id}
    page._selected_category = card._data
    card.set_selected(True)
    card.set_cut_state(True)
    preview = QPixmap(20, 20)
    preview.fill(QColor("red"))
    card.set_preview_pixmap(preview)
    preview_key = card._thumb.pixmap().cacheKey()
    cards = dict(page._vs_card_cache)
    display_items = page._display_items
    index_map = page._display_idx_map
    geometry = card.geometry()
    scroll = page._scroll_area.verticalScrollBar().value()
    for method in ("_refresh_current_level", "_show_selection_panel", "_hide_panel"):
        monkeypatch.setattr(page, method, _forbid_rebuild)
    monkeypatch.setattr(card, "set_category", _forbid_rebuild)
    monkeypatch.setattr(card, "set_preview_pixmap", _forbid_rebuild)
    updated = [dict(row, enabled_count=3) for row in original]
    by_id = {row["id"]: row for row in updated}

    page.set_characters(updated, refresh=False)

    assert page._display_items is display_items
    assert page._display_idx_map is index_map
    assert page._vs_card_cache == cards
    assert page._selected_ids == {card_id}
    assert page._selected_category is by_id[card_id]
    assert card._selected and card._cut_state
    assert card._cached_pixmap is preview
    assert card._thumb.pixmap().cacheKey() == preview_key
    assert card._preview_resolved and not card._preview_requested
    assert card.geometry() == geometry
    assert page._scroll_area.verticalScrollBar().value() == scroll
    for item in display_items:
        assert item["data"] is by_id[item["id"]]
    for item_id, visible_card in cards.items():
        assert visible_card._data is by_id[item_id]
        assert visible_card._info_label.text() == t("{enabled}/{total} 已启用", enabled=3, total=5)


def test_count_publication_updates_existing_and_deferred_list_rows(qtbot, temp_data_dir, request, monkeypatch):
    page = _page(qtbot, request)
    original = [
        {"id": f"cat-{i:03}", "name": f"Category {i:03}", "mod_count": 5, "enabled_count": 0} for i in range(60)
    ]
    page.set_characters(original)
    page._set_view("list")
    table = page._table
    table.selectRow(0)
    first_count = table.item(0, 4)
    pending_rows = set(table._pending_rows)
    assert 59 in pending_rows
    row_factory = table._row_factory
    monkeypatch.setattr(page, "_refresh_current_level", _forbid_rebuild)
    updated = [dict(row, enabled_count=3) for row in original]

    page.set_characters(updated, refresh=False)

    assert table.item(0, 4) is first_count
    assert first_count.text() == "3 / 5"
    assert table._pending_rows == pending_rows
    assert table._row_factory is row_factory
    assert [row.row() for row in table.selectionModel().selectedRows()] == [0]
    assert table.item(59, 4).text() == "3 / 5"
