from types import SimpleNamespace

import pytest

from src.models.mod import Mod
from src.ui.main_window import MainWindow


def test_preset_commit_publishes_only_affected_rows_without_discovery():
    calls = []
    updated = Mod(game_id="game", mod_id="changed", name="Changed")

    class Host:
        _on_preset_applied = MainWindow._on_preset_applied
        PAGE_GALLERY = MainWindow.PAGE_GALLERY
        _game_service = SimpleNamespace(current_game_id="game")
        _mod_manager = SimpleNamespace(get_mod=lambda mid: updated, get_characters_for_ui=lambda gid: [])
        _overview_page = SimpleNamespace(
            update_mods_in_place=lambda mods: calls.append(("rows", mods)),
            set_characters=lambda chars, **kw: calls.append(("counts", kw)),
        )
        _stack = SimpleNamespace(currentIndex=lambda: MainWindow.PAGE_OVERVIEW)
        _gallery_page = SimpleNamespace(refresh_from_index=lambda: calls.append(("gallery",)))

        def _refresh_preset_combo(self):
            return calls.append(("presets",))

        def _refresh_current(self, **kw):
            return calls.append(("discovery", kw))

    Host()._on_preset_applied(["changed"])
    assert ("rows", [updated]) in calls
    assert not any(c[0] in ("gallery", "discovery") for c in calls)


def test_batch_status_publication_preserves_selection_rows_and_scrolled_widgets(qtbot, temp_data_dir):
    from tests.ui.test_large_mod_list import _list_page

    page = _list_page(qtbot)
    table = page._table
    table.selectRow(3)
    selected = table.selectedItems()[0].row()
    untouched = table.cellWidget(2, 1)
    changed = Mod(game_id="game", mod_id="row-0003", name="Name 0003")
    changed.enable()
    tail = Mod(game_id="game", mod_id="row-0399", name="Name 0399")
    tail.enable()
    page.update_mods_in_place([changed, tail])
    assert page._current_mods[3] is changed
    assert page._subtree_mods[399] is tail
    assert page._display_items[3]["data"] is changed
    assert table.selectedItems()[0].row() == selected
    assert table.cellWidget(2, 1) is untouched
    assert table.item(3, 4).text() == "已启用"
    table.scrollToItem(table.item(399, 0))
    qtbot.wait(10)
    assert table.item(399, 4).text() == "已启用"


def test_old_game_probe_notification_cannot_replace_current_view():
    calls = []

    class Host:
        _refresh_after_source_reconcile = MainWindow._refresh_after_source_reconcile
        PAGE_OVERVIEW = MainWindow.PAGE_OVERVIEW
        PAGE_GALLERY = MainWindow.PAGE_GALLERY
        _game_service = SimpleNamespace(current_game_id="new")
        _stack = SimpleNamespace(currentIndex=lambda: MainWindow.PAGE_OVERVIEW)

        def _reload_overview_level(self, gid):
            return calls.append(gid)

    Host()._refresh_after_source_reconcile("old", 0)
    assert calls == []


@pytest.mark.parametrize("operation", ["preset", "disable"])
def test_game_switch_restores_owner_during_preparation(operation):
    class Host:
        _on_game_changed = MainWindow._on_game_changed
        _preset_apply_worker = object() if operation == "preset" else None
        _disable_worker = object() if operation == "disable" else None
        _preset_apply_game_id = "owner"
        _disable_game_id = "owner"
        _game_service = SimpleNamespace(current_game_id="new")
        _game_page = SimpleNamespace(reload=lambda: None)

    host = Host()
    host._on_game_changed("new")
    assert host._game_service.current_game_id == "owner"
