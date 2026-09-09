from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from src.models.mod import Mod, ModInfo
from src.ui.pages.overview import OverviewPage
from src.ui.smooth_scroll import SmoothScroller


@pytest.fixture
def page(qtbot, temp_data_dir, request):
    result = OverviewPage()
    qtbot.addWidget(result)
    request.addfinalizer(result.close)
    result.resize(900, 600)
    result.show()
    result.set_current_game("game_a")
    result.set_characters([{"id": name, "name": name} for name in ("A", "B", "C")])
    qtbot.wait(20)
    return result


def mods(parent, count=240):
    return [
        Mod(mod_id=f"{parent}-{i:03}", node_id=parent, name=f"Item {i:03}", info=ModInfo(tags=["Shared", "Second"]))
        for i in range(count)
    ]


def enter(page, parent, count=240):
    page._navigate_to(parent)
    page.set_current_mods(mods(parent, count))
    QApplication.processEvents()


def bar(page):
    return (page._table if page._view_mode == "list" else page._scroll_area).verticalScrollBar()


@pytest.mark.parametrize("mode", ["grid", "list"])
@pytest.mark.parametrize("a_position", [0, 700])
def test_directories_restore_separate_values_before_first_binding(page, qtbot, monkeypatch, mode, a_position):
    page._set_view(mode)
    enter(page, "A")
    bar(page).setValue(a_position if mode == "grid" else a_position // 50)
    expected_a = bar(page).value()
    enter(page, "B")
    assert bar(page).value() == 0, "first visit must not inherit A's position"
    bar(page).setValue(2100 if mode == "grid" else 70)
    expected_b = bar(page).value()
    assert expected_b > 0
    bound_at = []
    if mode == "grid":
        create = page._vs_create_card

        def record(item, **kwargs):
            bound_at.append((bar(page).value(), item["id"]))
            return create(item, **kwargs)

        monkeypatch.setattr(page, "_vs_create_card", record)
    else:
        populate = page._table._populate_row

        def record(row):
            if row in page._table._pending_rows and page._table._row_factory is not None:
                bound_at.append((bar(page).value(), row))
            return populate(row)

        monkeypatch.setattr(page._table, "_populate_row", record)
    enter(page, "A")
    assert bound_at and all(value == expected_a for value, _ in bound_at)
    assert bar(page).value() == expected_a
    if mode == "grid":
        assert bound_at[0][1] == f"A-{expected_a // page._vs_row_h * page._vs_cols:03}"
    else:
        assert bound_at[0][1] == max(0, page._table.rowAt(0) - 1)
    enter(page, "B")
    assert bar(page).value() == expected_b
    qtbot.wait(40)
    assert bar(page).value() == expected_b


@pytest.mark.parametrize("mode", ["grid", "list"])
def test_same_directory_refresh_keeps_live_value_and_clamps(page, mode):
    page._set_view(mode)
    enter(page, "A")
    bar(page).setValue(1700 if mode == "grid" else 60)
    expected = bar(page).value()
    page.begin_loading()
    page.set_current_mods(mods("A"))
    assert bar(page).value() == expected
    page.set_current_mods(mods("A", 8))
    assert bar(page).value() == min(expected, bar(page).maximum())


def test_fast_navigation_only_saves_the_presented_directory(page):
    enter(page, "B")
    bar(page).setValue(1800)
    enter(page, "A")
    bar(page).setValue(900)
    page._navigate_to("B")
    page._navigate_to("C")
    bar(page).setValue(1100)  # 读取 C 期间仍显示 A。
    page.set_current_mods(mods("C"))
    assert bar(page).value() == 0
    enter(page, "B")
    assert bar(page).value() == 1800, "uncommitted B must not acquire A's value"
    enter(page, "A")
    assert bar(page).value() == 1100


def test_return_to_source_while_loading_uses_latest_actual_value(page):
    enter(page, "A")
    bar(page).setValue(900)
    page._navigate_to("B")
    bar(page).setValue(1300)
    page._navigate_to("A")
    bar(page).setValue(1700)
    page.set_current_mods(mods("A"))
    assert bar(page).value() == 1700


def test_read_failure_restores_presented_identity_conditions_and_mode(page):
    enter(page, "A")
    page._search_input.setText("Item")
    page._execute_search_refresh(force_text="Item")
    tag_id = next(iter(page._tag_filter.catalog))
    page._tag_filter.toggle(tag_id)
    bar(page).setValue(900)
    old_cards = dict(page._vs_card_cache)
    page.set_mod_loader(lambda: None)
    page._navigate_to("B")
    page.begin_loading()
    page._search_input.setText("uncommitted")
    page._set_view("list")
    page._navigate_to("C")
    page.loading_failed()
    assert page._current_parent_id == "A"
    assert page.get_current_path() == [("A", "A")]
    assert page._search_input.text() == "Item"
    assert page._tag_filter.selected_ids == [tag_id]
    assert page._view_mode == "grid" and page._scroll_area.isVisible()
    assert bar(page).value() == 900
    assert set(old_cards.values()) <= set(page._vs_card_cache.values())
    assert not page._mods_loading


@pytest.mark.parametrize("condition", ["search", "clear-search", "type", "age", "tag", "tag-mode", "sort"])
def test_changed_conditions_start_at_top(page, condition):
    enter(page, "A")
    if condition == "clear-search":
        page._search_input.setText("Item")
        page._execute_search_refresh(force_text="Item")
    if condition == "age":
        page._type_filter.setCurrentIndex(page._type_filter.findData("mod"))
    if condition == "tag-mode":
        page._tag_filter.toggle(next(iter(page._tag_filter.catalog)))
    bar(page).setValue(1300)
    assert bar(page).value() > 0
    if condition == "search":
        page._search_input.setText("Item")
        page._execute_search_refresh(force_text="Item")
    elif condition == "clear-search":
        page._search_input.clear()
    elif condition == "type":
        page._type_filter.setCurrentIndex(page._type_filter.findData("mod"))
    elif condition == "age":
        page._age_filter.setCurrentIndex(1)
    elif condition == "tag":
        page._tag_filter.toggle(next(iter(page._tag_filter.catalog)))
    elif condition == "tag-mode":
        page._tag_filter.set_match_mode("any")
    else:
        page._sort_combo.setCurrentIndex(page._sort_combo.findData("alpha_desc"))
    assert bar(page).value() == 0


def test_equivalent_search_keeps_actual_position(page):
    enter(page, "A")
    page._search_input.setText("Item")
    page._execute_search_refresh(force_text="Item")
    bar(page).setValue(1200)
    page._search_input.setText("  ITEM  ")
    page._execute_search_refresh(force_text="  ITEM  ")
    assert bar(page).value() == 1200


@pytest.mark.parametrize("fail", [False, True])
def test_loading_mode_change_never_samples_dormant_control(page, fail):
    enter(page, "A")
    bar(page).setValue(900)
    page._set_view("list")
    assert bar(page).value() == 0
    bar(page).setValue(40)
    page._set_view("grid")
    assert bar(page).value() == 900
    page._navigate_to("B")
    page._set_view("list")
    assert page._scroll_area.isVisible() and not page._table.isVisible()
    if fail:
        page.loading_failed()
        assert page._view_mode == "grid" and bar(page).value() == 900
    else:
        page.set_current_mods(mods("B"))
        assert page._view_mode == "list" and bar(page).value() == 0
        assert not page._card_size_slider.isVisible()
    enter(page, "A")
    page._set_view("list")
    assert bar(page).value() == 40


def test_root_synchronous_commit_is_not_repeated_by_data_only_apply(page):
    roots = [{"id": f"root-{i:03}", "name": f"Root {i:03}"} for i in range(180)]
    page.set_characters(roots)
    bar(page).setValue(1500)
    enter(page, "root-000")
    bar(page).setValue(2700)
    page._navigate_to("")
    page.set_current_mods([], [])
    assert bar(page).value() == 1500
    bar(page).setValue(1900)
    page.set_current_mods([], mods("root-000"), refresh_view=False)
    assert bar(page).value() == 1900


@pytest.mark.parametrize("mode", ["grid", "list"])
def test_empty_result_consumes_position_before_next_nonempty_view(page, mode):
    page._set_view(mode)
    enter(page, "A")
    bar(page).setValue(1400 if mode == "grid" else 40)
    enter(page, "B")
    enter(page, "A", count=0)
    assert bar(page).value() == 0
    page.set_current_mods(mods("A"))
    assert bar(page).value() == 0


@pytest.mark.parametrize("game_id", ["game_b", ""])
def test_game_boundary_discards_pending_and_all_position_memory(page, game_id):
    enter(page, "A")
    bar(page).setValue(1500)
    page._navigate_to("B")
    page.set_current_game(game_id)
    page.set_characters([{"id": "A", "name": "A"}], force_root=True)
    page.set_current_mods([], [], refresh_view=False)
    page.loading_failed()  # 过期任务不能恢复先前来源。
    assert page._current_parent_id == "" and bar(page).value() == 0
    enter(page, "A")
    assert bar(page).value() == 0


@pytest.mark.parametrize("deleted", ["A", "B"])
def test_deleted_source_or_target_cannot_return_via_failure(page, deleted):
    enter(page, "A")
    bar(page).setValue(1200)
    page._navigate_to("B")
    page.set_characters([{"id": name, "name": name} for name in ("A", "B", "C") if name != deleted])
    page.loading_failed()
    assert page._current_parent_id != deleted
    page.set_characters([{"id": name, "name": name} for name in ("A", "B", "C")])
    enter(page, deleted)
    assert bar(page).value() == 0


def test_incremental_node_update_keeps_other_directories_positions(page):
    enter(page, "A")
    bar(page).setValue(1500)
    enter(page, "B")
    page.apply_node_delta([{"id": "B", "name": "Renamed"}])
    enter(page, "A")
    assert bar(page).value() == 1500


def test_list_navigation_does_not_reveal_hidden_top_rows(page, qtbot, monkeypatch):
    page._set_view("list")
    enter(page, "A")
    bar(page).setValue(65)
    enter(page, "B")
    monkeypatch.setattr("src.ui.pages.overview.anim.is_enabled", lambda: True)
    enter(page, "A")
    assert bar(page).value() == 65
    assert all(not page._table.isRowHidden(row) for row in range(24))
    qtbot.wait(360)
    assert bar(page).value() == 65


def test_same_value_navigation_cancels_old_smooth_target_without_fake_notifications(page, qtbot):
    enter(page, "A")
    scroller = page._scroll_area.findChild(SmoothScroller)
    scroller._target = 900
    scroller._timer.start()
    changed = []
    bar(page).valueChanged.connect(changed.append)
    enter(page, "B")
    assert bar(page).value() == 0
    assert not scroller._timer.isActive()
    scroller._tick()
    qtbot.wait(20)
    assert bar(page).value() == 0 and changed == []
    bar(page).setValue(100)
    assert changed == [100]


def test_main_window_syncs_authoritative_game_before_tree_publish(page):
    from src.ui.main_window import MainWindow

    enter(page, "A")
    bar(page).setValue(900)
    window = SimpleNamespace(
        _overview_page=page,
        _game_service=SimpleNamespace(has_active_game=lambda: True),
        _mod_manager=SimpleNamespace(get_characters_for_ui=lambda gid: [{"id": "A", "name": "A"}]),
        _overview_refresh=SimpleNamespace(schedule=lambda: None, prefetch=lambda *_: None),
    )
    MainWindow._load_characters(window, "game_b", force_root=True)
    assert page._current_game_id == "game_b"
    enter(page, "A")
    assert bar(page).value() == 0
    window._game_service.has_active_game = lambda: False
    MainWindow._load_characters(window, "")
    assert page._current_game_id == "" and page._current_parent_id == ""


def controlled_refresh(page, request, *, cached=False):
    """使用真实控制器和可确定结果的内存目录读取器。"""
    from src.ui.overview_refresh import OverviewRefreshController

    manager = SimpleNamespace(
        library_probe_stamp=(1, 0),
        library_probe_busy=lambda: False,
        _index=SimpleNamespace(iter_mods=lambda game, ids: (mod for mod in mods(ids[0], 100))),
    )
    controller = OverviewRefreshController(
        manager,
        current=lambda: (page._current_game_id, page._current_parent_id, page._search_input.text()),
        apply=page.set_current_mods,
        on_loading=lambda _: page.begin_loading(),
        on_error=page.loading_failed,
        recursive=lambda: False,
        cache_levels=4 if cached else 0,
        parent=page,
    )
    request.addfinalizer(controller.stop)
    page.category_navigated.connect(lambda _: controller.schedule())
    controller.BUDGET_MS = 0
    page.set_mod_loader(controller.schedule)
    return controller, manager


@pytest.mark.parametrize("cached", [False, True])
def test_controller_cold_and_synchronous_cached_apply_restore_before_binding(page, qtbot, monkeypatch, request, cached):
    controller, _ = controlled_refresh(page, request, cached=cached)
    page._navigate_to("A")
    qtbot.waitUntil(lambda: not controller.active)
    bar(page).setValue(700)
    page._navigate_to("B")
    qtbot.waitUntil(lambda: not controller.active)
    bar(page).setValue(2100)
    first = []
    create = page._vs_create_card

    def record(item, **kwargs):
        first.append((item["id"], bar(page).value()))
        return create(item, **kwargs)

    monkeypatch.setattr(page, "_vs_create_card", record)
    page._navigate_to("A")
    if cached:
        assert not controller.active and first
    qtbot.waitUntil(lambda: not controller.active)
    assert first and all(value == 700 for _, value in first)
    assert first[0][0].startswith("A-")


def test_controller_superseded_read_does_not_commit_or_change_final_position(page, qtbot, request):
    controller, _ = controlled_refresh(page, request)
    page._navigate_to("B")
    qtbot.waitUntil(lambda: not controller.active)
    bar(page).setValue(1800)
    page._navigate_to("A")
    qtbot.waitUntil(lambda: not controller.active)
    bar(page).setValue(900)
    page._navigate_to("B")
    controller._timer.stop()
    controller._slice()  # B 已解码一行，但仍显示 A。
    page._navigate_to("C")
    qtbot.waitUntil(lambda: not controller.active)
    assert page._current_parent_id == "C" and bar(page).value() == 0
    assert all(entry["id"].startswith("C-") for entry in page._display_items)
    page._navigate_to("B")
    qtbot.waitUntil(lambda: not controller.active)
    assert bar(page).value() == 1800


def test_controller_read_error_preserves_source_and_cancels_target(page, qtbot, request):
    controller, manager = controlled_refresh(page, request)
    page._navigate_to("A")
    qtbot.waitUntil(lambda: not controller.active)
    bar(page).setValue(900)
    old_items = page._display_items

    def failed_read(*_args):
        raise OSError("controlled read failure before apply")

    manager._index.iter_mods = failed_read
    page._navigate_to("B")
    qtbot.waitUntil(lambda: not controller.active)
    assert page._current_parent_id == "A" and bar(page).value() == 900
    assert page._display_items is old_items and not page._mods_loading


def test_closing_retires_pending_position_and_smooth_work(page):
    enter(page, "A")
    page._navigate_to("B")
    scroller = page._scroll_area.findChild(SmoothScroller)
    scroller._target = 1000
    scroller._timer.start()
    page.close()
    assert not scroller._timer.isActive()
    assert page._pending_view is None
