from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap

from src.models.mod import Mod, ModInfo
from src.ui.main_window import MainWindow
from src.ui.pages.overview import OverviewPage


def _page(qtbot, mode="list"):
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(1000, 650)
    page._sort_key = "alpha_asc"
    page.set_characters(
        [
            {"id": "parent", "name": "Parent"},
            {"id": "child", "name": "Child", "parent_id": "parent"},
            {"id": "other", "name": "Other"},
        ],
        refresh=False,
    )
    page._current_parent_id = "parent"
    page._view_mode = mode
    mods = [Mod(mod_id=f"m{i:03}", name=f"Mod {i:03}", path=f"old/{i}", node_id="parent") for i in range(60)]
    page.set_current_mods(mods, mods)
    page.show()
    qtbot.wait(30)
    return page, mods


@pytest.mark.parametrize("mode", ["list", "grid"])
def test_reconcile_rename_updates_path_without_rebuilding_unrelated_ui(qtbot, temp_data_dir, monkeypatch, mode):
    page, mods = _page(qtbot, mode)
    page._selected_ids = {mods[1].mod_id}
    page._show_selection_panel()
    qtbot.wait(30)
    unchanged = page._table.item(3, 0) if mode == "list" else page._vs_card_cache[mods[0].mod_id]
    monkeypatch.setattr(page, "_refresh_current_level", lambda: pytest.fail("full refresh"))
    monkeypatch.setattr(page, "_show_mod_panel", lambda mod: pytest.fail("preview reopened"))
    moved = replace(mods[1], path="renamed/1")
    moved.enable()
    assert page.apply_reconcile_delta([moved], ())
    assert page._selected_mod is moved
    assert page._selected_ids == {moved.mod_id}
    assert page._panel_path_label.text().endswith("renamed/1")
    assert page._mod_by_id(moved.mod_id) is moved
    if mode == "list":
        assert page._table.item(3, 0) is unchanged
        row = page._display_idx_map[moved.mod_id]
        assert page._table.item(row, 4).text() == "已启用"
    else:
        assert page._vs_card_cache[mods[0].mod_id] is unchanged
        assert page._vs_card_cache[moved.mod_id]._mod is moved


def test_reconcile_delete_preserves_surviving_selection_scroll_and_deferred_rows(qtbot, temp_data_dir, monkeypatch):
    page, mods = _page(qtbot)
    page._selected_ids = {mods[30].mod_id}
    page._show_selection_panel()
    table = page._table
    table.scrollToItem(table.item(30, 0))
    qtbot.wait(10)
    scroll = table.verticalScrollBar().value()
    kept = table.item(page._display_idx_map[mods[30].mod_id], 0)
    monkeypatch.setattr(page, "_refresh_table", lambda *args: pytest.fail("full table rebuild"))
    assert page.apply_reconcile_delta([], (mods[0].mod_id, mods[2].mod_id))
    assert page._selected_ids == {mods[30].mod_id}
    assert page._selected_mod is mods[30]
    row = page._display_idx_map[mods[30].mod_id]
    assert table.item(row, 0) is kept
    assert table.verticalScrollBar().value() == scroll
    last = page._display_idx_map[mods[-1].mod_id]
    assert table.item(last, 1).text() == "Mod 059"
    assert table._mod_rows[mods[-1].mod_id] == last
    assert all(m.mod_id not in {"m000", "m002"} for m in page._current_mods)


@pytest.mark.parametrize("mode", ["grid", "list"])
def test_recovered_container_restores_selection_and_toggle(qtbot, temp_data_dir, mode):
    page, mods = _page(qtbot, mode)
    mod = mods[0]
    page._selected_ids = {mod.mod_id}
    page._show_selection_panel()
    for path in ("renamed/body.zip", mod.path):
        page.set_pending_missing_mods({mod.mod_id})
        assert not page._panel_toggle_btn.isEnabled()
        recovered = replace(mod, path=path)
        page.apply_reconcile_delta([recovered], ())
        assert mod.mod_id not in page._pending_missing_mods
        assert page._panel_toggle_btn.isEnabled()
        assert page._selected_ids == {mod.mod_id}
        if mode == "grid":
            assert page._vs_card_cache[mod.mod_id].isEnabled()
        else:
            row = page._display_idx_map[mod.mod_id]
            assert page._table.item(row, 0).flags() & Qt.ItemFlag.ItemIsEnabled


def test_tagless_delete_does_not_reload_catalog_or_reenable_pending_card(qtbot, temp_data_dir, monkeypatch):
    page, mods = _page(qtbot, "grid")
    card = page._vs_card_cache[mods[0].mod_id]
    enabled = []
    original = card.setEnabled

    def set_enabled(value):
        enabled.append(value)
        original(value)

    monkeypatch.setattr(card, "setEnabled", set_enabled)
    monkeypatch.setattr(page, "refresh_tags", lambda **kwargs: pytest.fail("tagless removal cannot reload the catalog"))
    page.set_pending_missing_mods({mods[0].mod_id})
    page.set_pending_missing_mods({mods[0].mod_id})
    assert enabled == [False]
    assert page.apply_reconcile_delta([], {mods[0].mod_id})
    assert True not in enabled
    assert mods[0].mod_id not in page._display_idx_map


def test_tagged_delete_still_refreshes_usage_catalog(qtbot, temp_data_dir, monkeypatch):
    page, mods = _page(qtbot)
    mods[0].info.tags = ["Shared"]
    refreshed = []
    monkeypatch.setattr(page, "refresh_tags", lambda **kwargs: refreshed.append(kwargs))
    assert page.apply_reconcile_delta([], {mods[0].mod_id})
    assert refreshed == [{"refresh_view": False}]


def test_reconcile_relocation_updates_current_and_searched_subtrees(qtbot, temp_data_dir):
    page, mods = _page(qtbot)
    page._selected_ids = {mods[0].mod_id}
    page._show_selection_panel()
    child = replace(mods[0], node_id="child", path="child/moved")
    outside = replace(mods[1], node_id="other", path="other/moved")
    incoming = Mod(mod_id="incoming", name="Incoming", node_id="parent")
    assert page.apply_reconcile_delta([child, outside, incoming], ())
    assert "m000" not in {m.mod_id for m in page._current_mods}
    assert "m000" in {m.mod_id for m in page._subtree_mods}
    assert "m001" not in {m.mod_id for m in page._subtree_mods}
    assert "incoming" in page._display_idx_map
    assert not page._selected_ids and page._selected_mod is None
    page._search_input.setText("Mod 000")
    page._refresh_table("Mod 000")
    assert [entry["id"] for entry in page._display_items] == ["m000"]
    assert page._table.item(0, 0).data(Qt.ItemDataRole.UserRole) == "m000"


def test_source_rename_retargets_selected_preview_and_pending_reorder(qtbot, temp_data_dir, tmp_path):
    page, mods = _page(qtbot, "grid")
    old_root, new_root = tmp_path / "old", tmp_path / "new"
    old_root.mkdir()
    paths = [str(old_root / f"{i}.png") for i in range(2)]
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.red)
    for path in paths:
        assert pixmap.save(path)
    original = replace(mods[0], path=str(old_root), preview_images=paths)
    page.update_mod_in_view(original)
    page._selected_ids = {original.mod_id}
    page._show_selection_panel()
    page._panel_preview.set_images_deferred(paths, initial_index=1, cached_path=paths[1], cached_pixmap=pixmap)
    page._preview_override_paths[original.mod_id] = paths[1]
    page._pending_reorder_mod_id, page._pending_reorder_path = original.mod_id, paths[1]
    card = page._vs_card_cache[original.mod_id]
    card.show_preview_by_path(paths[1], pixmap)
    old_root.rename(new_root)
    new_paths = [str(new_root / f"{i}.png") for i in range(2)]
    moved = replace(original, path=str(new_root), preview_images=new_paths)
    assert page.apply_reconcile_delta([moved], ())
    assert page._panel_preview.current_image_path() == new_paths[1]
    assert page._panel_preview._cached_pixmap is not None
    assert page._preview_override_paths[original.mod_id] == new_paths[1]
    assert page._pending_reorder_path == new_paths[1]
    assert card.effective_preview_path() == new_paths[1]
    assert card._cached_pixmap is not None


def test_status_sort_moves_only_affected_row_and_retargets_shift_selection(qtbot, temp_data_dir):
    page, mods = _page(qtbot)
    page._sort_key = "status_desc"
    page._refresh_table()
    kept = page._table.item(3, 0)
    page._selected_ids = {mods[3].mod_id}
    page._last_clicked_index = 3
    page._show_selection_panel()
    enabled = replace(mods[20])
    enabled.enable()
    assert page.apply_reconcile_delta([enabled], ())
    assert page._display_items[0]["id"] == enabled.mod_id
    assert page._table.item(4, 0) is kept
    assert page._last_clicked_index == 4
    assert page._selected_ids == {mods[3].mod_id}


@pytest.mark.parametrize("known", [True, False])
def test_main_reconcile_uses_delta_only_with_complete_committed_ids(known):
    published, full = [], []
    mod = Mod(mod_id="changed", node_id="parent")

    class Host:
        _refresh_after_source_reconcile = MainWindow._refresh_after_source_reconcile
        PAGE_OVERVIEW = MainWindow.PAGE_OVERVIEW
        PAGE_GALLERY = MainWindow.PAGE_GALLERY
        _game_service = SimpleNamespace(current_game_id="game")
        _stack = SimpleNamespace(currentIndex=lambda: MainWindow.PAGE_OVERVIEW)
        _library_probe = SimpleNamespace(changed_ids=("changed",) if known else None, removed_ids=() if known else None)
        _overview_page = SimpleNamespace(
            apply_reconcile_delta=lambda mods, removed: published.append((mods, removed)) or True,
            set_characters=lambda *args, **kw: None,
        )
        _mod_manager = SimpleNamespace(get_mod=lambda mid: mod, get_characters_for_ui=lambda gid: [])

        def _reload_overview_level(self, gid):
            return full.append(gid)

    Host()._refresh_after_source_reconcile("game", 0)
    assert bool(published) is known
    assert bool(full) is not known


def test_main_reconcile_purged_ids_publish_removals_without_decoding_deleted_mods():
    published, decoded, full = [], [], []

    class Host:
        _refresh_after_source_reconcile = MainWindow._refresh_after_source_reconcile
        PAGE_OVERVIEW = MainWindow.PAGE_OVERVIEW
        PAGE_GALLERY = MainWindow.PAGE_GALLERY
        _game_service = SimpleNamespace(current_game_id="game")
        _stack = SimpleNamespace(currentIndex=lambda: MainWindow.PAGE_OVERVIEW)
        _library_probe = SimpleNamespace(changed_ids=("deleted",), removed_ids=("deleted",))
        _overview_page = SimpleNamespace(
            _selected_mod=None,
            apply_reconcile_delta=lambda mods, removed: published.append((mods, removed)) or True,
            set_characters=lambda *args, **kw: None,
        )
        _mod_manager = SimpleNamespace(get_mod=lambda mid: decoded.append(mid), get_characters_for_ui=lambda gid: [])

        def _reload_overview_level(self, gid):
            return full.append(gid)

        def _refresh_preset_combo(self):
            return None

        def _refresh_store_installed_state(self):
            return None

    Host()._refresh_after_source_reconcile("game", 1)
    assert published == [([], ("deleted",))]
    assert decoded == [] and full == []


def test_source_observation_notice_is_provisional_and_clears(qtbot, temp_data_dir):
    page, mods = _page(qtbot)
    mods[0].enable()
    row = page._display_idx_map[mods[0].mod_id]
    identity = page._table.item(row, 0)
    page.set_source_observations(
        (SimpleNamespace(mod_id=mods[0].mod_id, path=mods[0].path, state="missing", destination=""),)
    )
    assert page._source_reconcile_notice.isVisible()
    assert "正在核对" in page._source_reconcile_notice.text()
    assert page._table.item(row, 0) is identity
    assert page._mod_by_id(mods[0].mod_id) is mods[0] and mods[0].is_enabled
    page.set_source_observations(())
    assert page._source_reconcile_notice.isHidden()
    page.set_source_observations((SimpleNamespace(mod_id=mods[0].mod_id),))
    page.set_current_game("another-game")
    assert page._source_reconcile_notice.isHidden()


def test_delta_deleting_last_tagged_mod_updates_usage_without_clearing_filter(qtbot, temp_data_dir):
    from src.core.mod_index import ModIndex
    from src.core.tag_service import TagService
    from tests.helpers import register_games

    register_games("tag-delta")
    page, mods = _page(qtbot)
    index = ModIndex("tag-delta")
    try:
        index._db().execute("INSERT INTO nodes(id,game_id,directory_name) VALUES('parent','tag-delta','parent')")
        index._db().commit()
        tagged = replace(
            mods[0],
            game_id="tag-delta",
            folder_name="box",
            path=str(index.paths.roots["tag-delta"] / "parent/box/body.7z"),
            info=ModInfo(tags=["Only"]),
        )
        assert index.add_mod(tagged)
        service = TagService(index)
        page._mod_manager = SimpleNamespace(get_tag_service=lambda: service)
        page.set_current_mods([tagged, *mods[1:]])
        tag = next(iter(page._tag_filter.catalog.values()))
        page._tag_filter.toggle(tag.id)
        page._search_timer.stop()
        page._refresh_table()
        assert page._tag_filter.catalog[tag.id].usage_count == 1
        assert index.delete_mod(tagged.mod_id)
        assert page.apply_reconcile_delta([], (tagged.mod_id,))
        assert page._tag_filter.catalog[tag.id].usage_count == 0
        assert page._tag_filter.selected_ids == [tag.id]
        assert page._tag_filter.available_tags() == []
    finally:
        index.close()
