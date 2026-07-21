from types import SimpleNamespace

import pytest

from src.ui.overview_refresh import OverviewRefreshController
from src.ui.pages.overview import OverviewPage
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def test_switch_game_root_is_not_rebuilt_after_search_rows_arrive(qtbot, mm, tmp_path, monkeypatch):
    from src.ui.main_window import MainWindow

    root = mm.add_character("Root")
    _import_mod(mm, root.id, tmp_path, "Mod")
    page = OverviewPage()
    qtbot.addWidget(page)
    window = SimpleNamespace(
        _overview_page=page,
        _mod_manager=mm,
        _game_service=mm._game_service,
        _library_probe=SimpleNamespace(yield_to_local_events=lambda: None),
        _defer_source_reconcile=lambda: None,
    )
    refresh = OverviewRefreshController(
        mm,
        current=lambda: (mm.current_game_id, page._current_parent_id),
        apply=lambda direct, subtree: MainWindow._apply_overview_mods(window, direct, subtree),
        parent=page,
    )
    window._overview_refresh = refresh
    rendered = []
    original = page._refresh_current_level

    def render():
        rendered.append(True)
        original()

    monkeypatch.setattr(page, "_refresh_current_level", render)
    MainWindow._load_characters(window, mm.current_game_id, force_root=True)
    qtbot.waitUntil(lambda: not refresh.active)
    assert len(page._subtree_mods) == 1
    assert len(rendered) == 1


@pytest.mark.parametrize("view", ["grid", "list"])
def test_database_root_categories_visible_and_navigable(qtbot, mm, tmp_path, view):
    root = mm.add_character("Root")
    nested = mm.add_character("Child", parent_id=root.id)
    mod = _import_mod(mm, nested.id, tmp_path, "Nested")
    nodes = mm.get_characters_for_ui()
    assert next(row for row in nodes if row["id"] == root.id)["parent_id"] is None

    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(960, 700)
    page.show()
    page._set_view(view)
    refresh = OverviewRefreshController(
        mm, current=lambda: (mm.current_game_id, page._current_parent_id), apply=page.set_current_mods, parent=page
    )
    page.category_navigated.connect(lambda _: refresh.schedule())
    page.set_characters(nodes, force_root=True)
    refresh.schedule()
    qtbot.waitUntil(lambda: not refresh.active)
    assert [item["id"] for item in page._display_items] == [root.id]
    page._navigate_to(root.id)
    qtbot.waitUntil(lambda: not refresh.active)
    assert [item["id"] for item in page._display_items] == [nested.id]
    page._navigate_to(nested.id)
    qtbot.waitUntil(lambda: not refresh.active)
    assert [item["id"] for item in page._display_items] == [mod.mod_id]
    page._navigate_to(root.id)
    qtbot.waitUntil(lambda: not refresh.active)
    page._navigate_back()
    qtbot.waitUntil(lambda: not refresh.active)
    assert [row["id"] for row in page._characters] == [root.id]
    refresh.stop()
    page.close()
