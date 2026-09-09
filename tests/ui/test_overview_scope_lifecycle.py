from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from src.models.mod import Mod, ModStatus
from src.ui.pages.overview import OverviewPage
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


@pytest.fixture
def page(qtbot, temp_data_dir, request):
    result = OverviewPage()
    qtbot.addWidget(result)
    request.addfinalizer(result.close)
    result.resize(900, 600)
    result.show()
    qtbot.wait(20)
    return result


def _start_partial_view(page, monkeypatch):
    ticks = iter(range(100_000))
    monkeypatch.setattr("src.ui.pages.overview.perf_counter", lambda: next(ticks) * 0.02)
    mods = [Mod(mod_id=f"old-{i:03}", name=f"Old {i:03}") for i in range(100)]
    page.set_current_mods(mods, mods)
    assert page._vs_population_pending
    return dict(page._vs_card_cache)


@pytest.mark.parametrize("trigger", ["navigation", "search"])
def test_new_scope_stops_old_population_and_keeps_visible_cards(page, qtbot, monkeypatch, trigger):
    original = _start_partial_view(page, monkeypatch)
    page.set_mod_loader(lambda: None)
    if trigger == "navigation":
        page._navigate_to("next")
    else:
        page._search_input.setText("needle")
        page._execute_search_refresh(force_text="needle")

    assert not page._vs_population_timer.isActive()
    page._scroll_area.verticalScrollBar().setValue(page._vs_row_h * 4)
    page.resize(1100, 750)
    page._on_card_scale_changed(110)
    page._deferred_refresh()
    page._vs_relayout()
    qtbot.wait(80)

    assert page._vs_card_cache == original, "waiting must not bind more old-directory cards"
    assert all(not card.isHidden() for card in original.values())
    assert page.updatesEnabled() and page._scroll_content.updatesEnabled()
    assert not any(
        timer.isActive()
        for timer in (
            page._vs_population_timer,
            page._vs_scroll_timer,
            page._vs_pixmap_timer,
            page._resize_timer,
            page._scale_timer,
        )
    )


def test_loading_failure_resumes_old_directory_population(page, qtbot, monkeypatch):
    original = _start_partial_view(page, monkeypatch)
    page._navigate_to("next")

    page.loading_failed()

    qtbot.waitUntil(lambda: not page._vs_population_pending, timeout=2000)
    assert set(original.values()) <= set(page._vs_card_cache.values())
    assert len(page._vs_card_cache) > len(original)
    assert all(mid.startswith("old-") for mid in page._vs_card_cache)
    assert "正在检索" not in page._status_label.text()


def test_new_data_resumes_population_with_reused_cards(page, qtbot, monkeypatch):
    original = _start_partial_view(page, monkeypatch)
    page._navigate_to("next")
    mods = [Mod(mod_id=f"new-{i:03}", name=f"New {i:03}") for i in range(100)]

    page.set_current_mods(mods, mods)

    qtbot.waitUntil(lambda: not page._vs_population_pending, timeout=2000)
    assert set(original.values()) <= set(page._vs_card_cache.values())
    assert all(mid.startswith("new-") for mid in page._vs_card_cache)
    mid, card = next(iter(page._vs_card_cache.items()))
    qtbot.mouseClick(card, Qt.MouseButton.LeftButton)
    assert page._selected_ids == {mid}


def test_loading_retires_old_preview_requests_and_failure_resubmits(page, monkeypatch):
    mod = Mod(mod_id="preview", name="Preview", preview_images=["pending-preview.png"])
    page.set_current_mods([mod])
    card = page._vs_card_cache[mod.mod_id]
    page._vs_image_request_id = 42
    page._vs_image_requests[mod.mod_id] = (42, card, "pending-preview.png")
    page._vs_pending_prefetch.append(("outside", "outside.png"))
    submitted = []
    monkeypatch.setattr(page._pixmap_worker, "enqueue", lambda *args, **kwargs: submitted.append(args))

    page.begin_loading()
    page._on_bg_pixmap_loaded(mod.mod_id, "pending-preview.png", 42, None)
    page._vs_load_deferred_pixmaps()

    assert not page._vs_image_requests and not page._vs_pending_prefetch
    assert not card._preview_resolved
    assert submitted == []
    page.loading_failed()
    page._vs_load_deferred_pixmaps()
    assert len(submitted) == 1
    assert submitted[0][0] == mod.mod_id and submitted[0][4] > 42
    page._on_bg_pixmap_loaded(mod.mod_id, "pending-preview.png", 42, None)
    assert not card._preview_resolved


def test_repeated_loading_pauses_and_restores_lazy_table_cells(page, qtbot):
    page._set_view("list")
    page.set_current_mods([Mod(mod_id=f"old-{i:03}", name=f"Old {i:03}") for i in range(100)])
    table = page._table
    assert 99 in table._pending_rows

    page.begin_loading()
    page.begin_loading()
    table.verticalScrollBar().setValue(table.verticalScrollBar().maximum())
    qtbot.wait(20)

    assert table.item(99, 1) is None
    assert 99 in table._pending_rows
    page.loading_failed()
    assert table.item(99, 1).text() == "Old 099"


def test_game_root_can_replace_a_pending_directory(page):
    page.set_current_mods([Mod(mod_id="old", name="Old")])
    page._navigate_to("pending")

    page.set_characters([{"id": "new-root", "name": "New root"}], force_root=True)
    page.set_current_mods([], [], refresh_view=False)

    assert [item["id"] for item in page._display_items] == ["new-root"]
    assert "new-root" in page._vs_card_cache


@pytest.mark.parametrize("still_missing", [False, True])
def test_reused_card_enabled_state_belongs_to_new_mod(page, qtbot, still_missing):
    page.set_current_mods([Mod(mod_id="old", name="Old")])
    card = page._vs_card_cache["old"]
    page.set_pending_missing_mods(["old"])
    if still_missing:
        page.set_pending_missing_mods(["new"])

    page.set_current_mods([Mod(mod_id="new", name="New")])

    assert page._vs_card_cache["new"] is card
    assert card.isEnabled() is not still_missing
    if not still_missing:
        qtbot.mouseClick(card, Qt.MouseButton.LeftButton)
        assert page._selected_ids == {"new"}


def test_reused_category_card_does_not_inherit_disabled_state(page):
    page.set_characters([{"id": "old", "name": "Old"}])
    card = page._vs_card_cache["old"]
    card.setEnabled(False)

    page.set_characters([{"id": "new", "name": "New"}])

    assert page._vs_card_cache["new"] is card
    assert card.isEnabled()


@pytest.fixture
def enabled_library(mm, tmp_path):
    parent = mm.add_character(name="Parent")
    child = mm.add_character(name="Child", parent_id=parent.id)
    sibling = mm.add_character(name="Sibling")
    direct = _import_mod(mm, parent.id, tmp_path, "direct")
    nested = _import_mod(mm, child.id, tmp_path, "nested")
    outside = _import_mod(mm, sibling.id, tmp_path, "outside")
    _import_mod(mm, parent.id, tmp_path, "disabled")
    mm.set_game("game_b")
    foreign_node = mm.add_character(name="Foreign")
    foreign = _import_mod(mm, foreign_node.id, tmp_path, "foreign")
    mm.set_game("game_a")
    db = mm._index._db()
    db.executemany(
        "INSERT OR REPLACE INTO deployment_records(mod_id,state,actual_path) VALUES(?,'active','')",
        [(mod.mod_id,) for mod in (direct, nested, outside, foreign)],
    )
    db.commit()
    return mm, parent.id, child.id, direct.mod_id, nested.mod_id, outside.mod_id, foreign.mod_id


def test_enabled_id_query_uses_game_subtree_and_current_committed_state(enabled_library, monkeypatch):
    manager, parent, child, direct, nested, outside, foreign = enabled_library
    monkeypatch.setattr(manager._index, "_row_to_mod", lambda *_args: pytest.fail("ID queries must not hydrate Mods"))

    assert set(manager.get_enabled_mod_ids()) == {direct, nested, outside}
    assert set(manager.get_enabled_mod_ids(parent)) == {direct, nested}
    assert manager.get_enabled_mod_ids(child) == [nested]
    assert manager.get_enabled_mod_ids(game_id="") == []
    assert manager.get_enabled_mod_ids(parent, game_id="game_b") == []
    manager._index._db().execute("UPDATE deployment_records SET state='disabled' WHERE mod_id=?", (nested,))
    manager._index._db().commit()
    assert manager.get_enabled_mod_ids(parent) == [direct]
    manager.set_game("game_b")
    assert manager.get_enabled_mod_ids() == [foreign]
    assert set(manager.get_enabled_mod_ids(game_id="game_a")) == {direct, outside}


@pytest.mark.parametrize("at_root", [True, False])
def test_disable_all_queries_unloaded_descendants(page, enabled_library, monkeypatch, at_root):
    manager, parent, _child, direct, nested, outside, _foreign = enabled_library
    page._current_parent_id = "" if at_root else parent
    page.set_current_mods([], [])
    page.set_enabled_mod_ids_loader(manager.get_enabled_mod_ids)
    emitted = []
    page.batch_disable_requested.connect(emitted.append)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *_args: pytest.fail("enabled descendants must be found"))

    page._on_batch_disable()

    assert len(emitted) == 1
    assert set(emitted[0]) == ({direct, nested, outside} if at_root else {direct, nested})
    assert page._subtree_mods == [], "bulk actions must not force navigation data hydration"


def test_standalone_disable_all_keeps_existing_subtree_behavior(page, monkeypatch):
    enabled = Mod(mod_id="nested", name="Nested", status=ModStatus.ENABLED)
    page.set_current_mods([], [enabled, Mod(mod_id="disabled")])
    emitted = []
    page.batch_disable_requested.connect(emitted.append)
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)

    page._on_batch_disable()

    assert emitted == [["nested"]]


def test_failed_enabled_id_query_never_falls_back_to_partial_view(page, monkeypatch):
    page.set_current_mods([Mod(mod_id="partial", status=ModStatus.ENABLED)])

    def fail(_parent):
        raise RuntimeError("index unavailable")

    page.set_enabled_mod_ids_loader(fail)
    emitted, warnings = [], []
    page.batch_disable_requested.connect(emitted.append)
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args: warnings.append(_args))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)

    page._on_batch_disable()

    assert emitted == []
    assert len(warnings) == 1
