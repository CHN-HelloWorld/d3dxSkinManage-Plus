from __future__ import annotations

import cProfile
import hashlib
import json
import os
import pstats
import time
from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QWidget

from src.models.mod import Mod, ModInfo
from src.ui import anim
from src.ui.pages.overview import OverviewPage
from src.ui.widgets.tag_chip import TagChip
from src.ui.widgets.tag_display import TagNameCell


def test_large_list_switch_and_column_resize(qtbot, temp_data_dir):
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(1200, 800)
    page.show()
    qtbot.wait(30)
    mods = [
        Mod(
            game_id="game",
            mod_id=f"list-{i:05}",
            name=f"合成列表 Mod {i:05}",
            info=ModInfo(author=f"Author {i % 11}", tags=["高清", "材质", "第三标签"]),
        )
        for i in range(5000)
    ]
    page._current_mods = mods
    page._subtree_mods = mods
    # 使用时间排序，排除无关的拼音转换开销。
    page._sort_key = os.environ.get("LIST_PERF_SORT", "time_desc")
    grid_preparation_ms = None
    if os.environ.get("LIST_PERF_WARM_GRID") == "1":
        start = time.perf_counter()
        page._refresh_cards()
        grid_preparation_ms = (time.perf_counter() - start) * 1000
        qtbot.wait(150)
    old_enabled = anim.is_enabled()
    anim.set_enabled(True)
    profile = cProfile.Profile()
    start = time.perf_counter()
    profile.enable()
    page._set_view("list")
    profile.disable()
    switch_ms = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    QApplication.processEvents()
    initial_events_ms = (time.perf_counter() - start) * 1000
    qtbot.wait(400)
    resizes = []
    for width in range(220, 340, 6):
        start = time.perf_counter()
        page._table.setColumnWidth(1, width)
        QApplication.processEvents()
        resizes.append((time.perf_counter() - start) * 1000)
    result = {
        "sort": page._sort_key,
        "grid_preparation_unprofiled_ms": grid_preparation_ms,
        "switch_profiled_ms": switch_ms,
        "initial_events_ms": initial_events_ms,
        "resize_max_ms": max(resizes),
        "resize_median_ms": sorted(resizes)[len(resizes) // 2],
        "rows": page._table.rowCount(),
        "qwidgets": len(page._table.findChildren(QWidget)),
        "source_sha256": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (Path("src/ui/pages/overview.py"), Path("src/ui/widgets/mod_list.py"))
        },
    }
    print(json.dumps(result, ensure_ascii=False))
    target = os.environ.get("LIST_PERF_OUTPUT")
    if target:
        output = Path(target)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        profile.dump_stats(str(output.with_suffix(".prof")))
        with output.with_suffix(".profile.txt").open("w", encoding="utf-8") as stream:
            pstats.Stats(profile, stream=stream).sort_stats("cumulative").print_stats(30)
    anim.set_enabled(old_enabled)
    assert page._table.rowCount() == 5000
    assert switch_ms < 200, "list switching must return within the maximum stall budget"
    assert max(resizes) < 50, "resizing must only lay out visible name cells"


def _list_page(qtbot, count=400):
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(1000, 650)
    page.show()
    page._current_mods = [
        Mod(game_id="game", mod_id=f"row-{i:04}", name=f"Name {i:04}", info=ModInfo(tags=["first", "second", "third"]))
        for i in range(count)
    ]
    page._subtree_mods = page._current_mods
    page._sort_key = "time_asc"
    page._set_view("list")
    qtbot.wait(10)
    return page


def test_scrolling_materializes_tail_and_bounds_live_tag_controls(qtbot, temp_data_dir):
    page = _list_page(qtbot)
    table = page._table
    for row in (200, 399, 0):
        table.scrollTo(table.model().index(row, 0))
        qtbot.wait(10)
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert table.item(row, 1).text() == f"Name {row:04}"
        cell = table.cellWidget(row, 1)
        assert cell is not None
        assert [chip.tag.name for chip in cell.findChildren(TagChip)] == ["first", "second"]
        assert len(table.findChildren(TagNameCell)) < 24


def test_offscreen_status_and_selection_survive_list_rebuild(qtbot, temp_data_dir):
    page = _list_page(qtbot)
    table = page._table
    page._select_all()
    assert len(page._selected_ids) == 400
    assert len(table.selectionModel().selectedRows()) == 400
    table.set_deploy_progress("row-0399", 0.45)
    assert table.item(399, 4).text() == "启用中 45%"
    page._refresh_table()
    assert len(page._selected_ids) == 400
    table.scrollTo(table.model().index(399, 0))
    qtbot.wait(10)
    assert table.item(399, 4).text() == "启用中 45%"
    assert table.cellWidget(399, 1)._name == "Name 0399"
    table.finish_deploy("row-0399", success=False, error="synthetic failure")
    assert table.item(399, 4).toolTip() == "synthetic failure"


def test_replaced_rows_never_render_old_lazy_content(qtbot, temp_data_dir):
    page = _list_page(qtbot)
    page._current_mods = [Mod(game_id="game", mod_id=f"new-{i}", name=f"Replacement {i}") for i in range(400)]
    page._subtree_mods = page._current_mods
    page._refresh_table()
    page._table.scrollTo(page._table.model().index(399, 0))
    qtbot.wait(10)
    assert page._table.item(399, 0).data(Qt.ItemDataRole.UserRole) == "new-399"
    assert page._table.cellWidget(399, 1)._name == "Replacement 399"
    assert page._table.item(399, 4).text() == "已禁用"


def test_identity_copies_keep_distinct_status_and_rebuild_resets_category_height(qtbot, temp_data_dir):
    page = _list_page(qtbot, count=2)
    enabled, disabled = page._current_mods
    enabled.enable()
    page.set_characters([{"id": "category", "name": "Category"}], refresh=False)
    page._refresh_table()
    table = page._table
    assert table.rowCount() == 3
    expected = [("category", "category", None), (enabled.mod_id, "mod", True), (disabled.mod_id, "mod", False)]
    for row, (item_id, kind, status) in enumerate(expected):
        identity = table.item(row, 0)
        assert identity.data(Qt.ItemDataRole.UserRole) == item_id
        assert identity.data(Qt.ItemDataRole.UserRole + 1) == kind
        assert identity.data(table.ROLE_ENABLED) is status
    assert table.rowHeight(0) == 32
    table.item(1, 0).setData(table.ROLE_ENABLED, False)
    assert table.item(2, 0).data(table.ROLE_ENABLED) is False
    table.setRowHidden(0, True)
    page.set_characters([], refresh=False)
    page._current_mods.append(Mod(game_id="game", mod_id="third", name="Third"))
    page._refresh_table()
    assert table.rowCount() == 3
    assert all(table.rowHeight(row) == 52 and not table.isRowHidden(row) for row in range(3))
    assert [table.item(row, 0).data(Qt.ItemDataRole.UserRole) for row in range(3)] == [
        enabled.mod_id,
        disabled.mod_id,
        "third",
    ]


def test_rebuilding_list_keeps_scrolled_row_and_immediate_tail_identity(qtbot, temp_data_dir):
    page = _list_page(qtbot)
    table = page._table
    table.scrollToItem(table.item(250, 0))
    qtbot.wait(10)
    previous = table.verticalScrollBar().value()
    page._refresh_table()
    assert table.verticalScrollBar().value() == previous
    assert table.item(399, 0).data(Qt.ItemDataRole.UserRole) == "row-0399"


def test_virtual_list_overflow_remains_interactive_after_scrolling(qtbot, temp_data_dir):
    page = _list_page(qtbot)
    table = page._table
    table.scrollTo(table.model().index(399, 0))
    qtbot.wait(10)
    cell = table.cellWidget(399, 1)
    clicked, toggles = [], []
    table.tag_clicked.connect(clicked.append)
    page.mod_toggle_requested.connect(toggles.append)
    qtbot.mouseClick(cell._tags._more_button, Qt.MouseButton.LeftButton)
    popup = cell._tags._popup
    assert popup.isVisible()
    chips = popup.findChildren(TagChip)
    assert [chip.tag.name for chip in chips] == ["first", "second", "third"]
    qtbot.mouseClick(chips[-1], Qt.MouseButton.LeftButton)
    assert clicked == ["local:third"]
    assert not toggles


def test_offscreen_name_and_update_badge_are_current_when_revealed(qtbot, temp_data_dir):
    page = _list_page(qtbot)
    mod = page._current_mods[-1]
    mod.name = "Renamed tail"
    page.update_mod_in_view(mod)
    page.set_updatable({mod.mod_id})
    page._table.scrollTo(page._table.model().index(399, 0))
    qtbot.wait(10)
    cell = page._table.cellWidget(399, 1)
    assert cell._name == "Renamed tail"
    from PySide6.QtWidgets import QLabel

    assert "可更新" in [label.text() for label in cell.findChildren(QLabel)]


def test_keyboard_navigation_reveals_the_right_tail_row(qtbot, temp_data_dir):
    page = _list_page(qtbot)
    table = page._table
    table.setFocus()
    table.setCurrentCell(398, 0)
    qtbot.keyClick(table, Qt.Key.Key_Down)
    qtbot.wait(10)
    assert page._selected_ids == {"row-0399"}
    assert table.cellWidget(399, 1)._name == "Name 0399"


def test_repeated_name_sort_reuses_transliteration_but_rename_resorts(qtbot, temp_data_dir):
    page = OverviewPage()
    qtbot.addWidget(page)
    original = page._lazy_pinyin
    conversions = []

    def convert(value, **kwargs):
        conversions.append(value)
        return original(value, **kwargs)

    page._lazy_pinyin = convert
    page._sort_key = "alpha_asc"
    mods = [Mod(game_id="game", mod_id="z", name="中"), Mod(game_id="game", mod_id="a", name="阿")]
    assert [mod.mod_id for mod in page._apply_sort([], mods)[1]] == ["a", "z"]
    assert [mod.mod_id for mod in page._apply_sort([], mods)[1]] == ["a", "z"]
    assert conversions == ["中", "阿"], "view switches must reuse the same sort keys"
    mods[1].name = "紫"
    assert [mod.mod_id for mod in page._apply_sort([], mods)[1]] == ["z", "a"]
    assert conversions == ["中", "阿", "紫"]
    # 转换器变化后不能复用此前转换语义生成的键。
    page._lazy_pinyin = None
    assert page._sort_text_key("中") == ("中",)
