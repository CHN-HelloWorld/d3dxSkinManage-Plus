from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QLabel, QMainWindow, QPushButton

from src.core.ini_keys import KeyBinding
from src.models.mod import Mod
from src.ui.pages.overview import OverviewPage
from src.ui.widgets.key_capture import KeyCaptureField
from tests.ui.test_theme_transition_surfaces import appearance as appearance


def test_enter_category_clears_preview_and_does_not_reselect_old_card(qtbot, temp_data_dir):
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.show()
    page.set_characters(
        [
            {"id": "parent", "name": "Parent", "parent_id": ""},
            {"id": "child", "name": "Child", "parent_id": "parent"},
        ]
    )
    page.category_navigated.connect(lambda _category_id: page.set_current_mods([]))
    qtbot.waitUntil(lambda: "parent" in page._vs_card_cache)
    card = page._vs_card_cache["parent"]

    qtbot.mouseClick(card, Qt.MouseButton.LeftButton)
    assert page._panel_mode == "category"
    assert page._panel_edit_btn.isEnabled()

    qtbot.mouseDClick(card, Qt.MouseButton.LeftButton)

    assert page._current_parent_id == "parent"
    assert page._selected_ids == set()
    assert page._selected_category is None
    assert page._panel_mode == "none"
    assert not page._panel_edit_btn.isVisible()
    assert not page._panel_preview.acceptDrops()

    qtbot.waitUntil(lambda: "child" in page._vs_card_cache)
    qtbot.mouseClick(page._vs_card_cache["child"], Qt.MouseButton.LeftButton)
    assert page._selected_ids == {"child"}
    assert page._panel_name_label.text() == "Child"
    assert page._panel_edit_btn.isEnabled()


def test_preview_panel_blank_click_keeps_mod_selected(qtbot, temp_data_dir):
    page = OverviewPage()
    qtbot.addWidget(page)
    mod = Mod(
        mod_id="selected-mod",
        game_id="game_a",
        name="Selected",
        folder_name="Selected",
        path="",
        imported_at="2026-10-05T10:00:00",
    )

    page.resize(1000, 700)
    page.show()
    page._selected_ids = {mod.mod_id}
    page._selected_mod = mod
    page._show_mod_panel(mod)
    qtbot.wait(10)

    qtbot.mouseClick(
        page._preview_panel,
        Qt.MouseButton.LeftButton,
        pos=QPoint(2, 2),
    )

    assert page._selected_ids == {mod.mod_id}
    assert page._selected_mod is mod


def test_key_panel_shows_section_header_without_tooltips(qtbot, temp_data_dir):
    page = OverviewPage()
    qtbot.addWidget(page)
    page._selected_mod = Mod(
        mod_id="key-mod",
        game_id="game_a",
        name="Key Mod",
        folder_name="Key Mod",
        path="",
        imported_at="2026-10-05T10:00:00",
    )
    page._mod_manager = type(
        "BindingManager",
        (),
        {"effective_ini_disk_path": lambda self, mod, rel: "C:/mods/outfit.ini"},
    )()
    binding = KeyBinding(
        ini_path="outfit.ini",
        section="KeySwapOutfit",
        key_line_no=1,
        key_value="h",
        back_line_no=2,
        back_value="j",
        rel="outfit.ini",
    )

    page._render_keys_panel([binding])

    action_label = page._panel_keys_container.findChild(QLabel, "iniBindingActionLabel")
    title = page._panel_keys_container.findChild(QLabel, "iniKeysTitle")
    file_badge = page._panel_keys_container.findChild(QLabel, "iniFileBadge")
    file_label = page._panel_keys_container.findChild(QLabel, "iniFileName")
    forward_label = page._panel_keys_container.findChild(QLabel, "iniForwardLabel")
    back_label = page._panel_keys_container.findChild(QLabel, "iniBackLabel")
    open_button = page._panel_keys_container.findChild(QPushButton, "iniOpenFileButton")
    key_field = page._panel_keys_container.findChild(KeyCaptureField)
    assert title is not None and title.font().pixelSize() == 13
    assert file_badge is not None and file_badge.font().pixelSize() == 10
    assert action_label is not None
    assert action_label.text() == "KeySwapOutfit"
    assert action_label.font().pixelSize() == 13
    assert action_label.toolTip() == ""
    assert file_label is not None
    assert file_label.font().pixelSize() == 12
    assert file_label.toolTip() == ""
    assert forward_label is not None and forward_label.font().pixelSize() == 12
    assert back_label is not None and back_label.font().pixelSize() == 12
    assert open_button is not None and open_button.font().pixelSize() == 12
    assert page._panel_path_label.font().pixelSize() == 12
    assert key_field is not None
    assert key_field._btn.toolTip() == ""
    assert key_field._edit.toolTip() == ""


def test_escape_cancels_capture_before_window_shortcut(qtbot):
    window = QMainWindow()
    field = KeyCaptureField("y")
    window.setCentralWidget(field)
    qtbot.addWidget(window)
    shortcut_calls: list[bool] = []
    shortcut = QShortcut(
        QKeySequence("Escape"),
        window,
        lambda: shortcut_calls.append(True),
    )
    window.show()

    qtbot.mouseClick(field._btn, Qt.MouseButton.LeftButton)
    assert field._capturing
    qtbot.keyClick(field, Qt.Key.Key_Escape)

    assert not field._capturing
    assert field.token() == "y"
    assert shortcut_calls == []
    assert shortcut.isEnabled()


def test_mod_preview_tabs_keep_title_actions_outside_scroll(qtbot, temp_data_dir):
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(1024, 680)
    mod = Mod(mod_id="preview", game_id="game_a", name="Preview", folder_name="Preview")
    page._selected_mod = mod
    page._selected_ids = {mod.mod_id}
    page._show_mod_panel(mod)
    page.show()
    assert hasattr(page, "_detail_tabs"), "The preview still has one long information column"
    assert [page._detail_tabs.tabText(i).split()[0] for i in range(3)] == ["信息", "按键", "变体"]
    assert not page._panel_info_scroll.isAncestorOf(page._panel_name_label)
    page._detail_tabs.setCurrentIndex(2)
    qtbot.wait(10)
    assert page._variant_panel.isVisible()
    assert page._panel_toggle_btn.isVisible()
    assert page._panel_splitter.widget(1).height() >= 240


def test_mod_variant_context_is_cleared_for_multi_category_and_navigation(qtbot, temp_data_dir):
    page = OverviewPage()
    qtbot.addWidget(page)
    mod = Mod(mod_id="preview", game_id="game_a", name="Preview")
    assert hasattr(page, "variant_context_changed"), "The variant controller has no selection boundary"
    contexts = []
    page.variant_context_changed.connect(contexts.append)
    for clear in (
        lambda: page._show_multi_select_panel(),
        lambda: page._show_category_panel({"id": "cat", "name": "Category"}),
        lambda: page._navigate_to("cat"),
    ):
        page._selected_mod = mod
        page._selected_ids = {mod.mod_id}
        page._show_mod_panel(mod)
        clear()
        assert contexts[-1] == ""
        assert not page._variant_panel.isVisible()


def test_key_actions_and_forward_back_fields_share_a_row(qtbot, temp_data_dir):
    page = OverviewPage()
    qtbot.addWidget(page)
    mod = Mod(mod_id="key-mod", game_id="game_a", name="Keys")
    page._selected_mod = mod
    page._show_mod_panel(mod)
    page._mod_manager = type("Manager", (), {"effective_ini_disk_path": lambda *args: "C:/outfit.ini"})()
    page._render_keys_panel(
        [
            KeyBinding(
                ini_path="outfit.ini",
                section="KeyOutfit",
                key_line_no=1,
                key_value="h",
                back_line_no=2,
                back_value="j",
                rel="outfit.ini",
            )
        ]
    )
    page.resize(1024, 680)
    page._detail_tabs.setCurrentIndex(1)
    page.show()
    qtbot.wait(10)
    label = page._panel_keys_container.findChild(QLabel, "iniBindingActionLabel")
    fields = page._panel_keys_container.findChildren(KeyCaptureField)
    label_left = label.mapTo(page._panel_keys_container, QPoint(0, 0))
    field_left = fields[0].mapTo(page._panel_keys_container, QPoint(0, 0))
    assert label_left.x() + label.width() <= field_left.x()
    assert abs(label_left.y() + label.height() // 2 - field_left.y() - fields[0].height() // 2) <= 2


def test_detail_tabs_restore_each_mod_scroll_after_async_keys(qtbot, temp_data_dir):
    from threading import Event

    from tests.ui.test_mod_variant_panel import state, variant

    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(1024, 680)
    bindings = [
        KeyBinding(ini_path="outfit.ini", section=f"KeyOutfit{i}", key_line_no=i + 1, key_value="h", rel="outfit.ini")
        for i in range(30)
    ]
    entered, release = Event(), Event()
    delay = False

    def parse(mod):
        if delay:
            entered.set()
            assert release.wait(3)
        return list(bindings)

    page._mod_manager = SimpleNamespace(parse_mod_key_bindings=parse, effective_ini_disk_path=lambda *args: None)
    variants = [variant(str(i), f"Variant {i}") for i in range(25)]
    first = Mod(mod_id="first-mod", game_id="game_a", name="First")
    second = Mod(mod_id="second-mod", game_id="game_a", name="Second")
    first.info.description = "Description\n" * 40
    second.info.description = "Other description\n" * 40

    def select(mod, *, wait=True):
        page._selected_mod = mod
        page._show_mod_panel(mod)
        page.set_variant_state(mod.mod_id, state(mod_id=mod.mod_id, selected_variant_id="0"), variants)
        if wait:
            qtbot.waitUntil(lambda: page._keys_panel_mod_id == mod.mod_id)
            qtbot.wait(15)

    page.show()
    select(first)
    page._detail_tabs.setCurrentIndex(0)
    qtbot.waitUntil(lambda: page._panel_info_scroll.verticalScrollBar().maximum() >= 60)
    page._panel_info_scroll.verticalScrollBar().setValue(60)
    page._detail_tabs.setCurrentIndex(1)
    qtbot.waitUntil(lambda: page._panel_keys_scroll.verticalScrollBar().maximum() >= 120)
    page._panel_keys_scroll.verticalScrollBar().setValue(120)
    page._detail_tabs.setCurrentIndex(2)
    qtbot.waitUntil(lambda: page._variant_panel.scroll_area.verticalScrollBar().maximum() >= 300)
    page._variant_panel.scroll_area.verticalScrollBar().setValue(300)
    select(second)
    select(first)
    assert page._variant_panel.scroll_area.verticalScrollBar().value() == 300
    select(second)
    page._detail_tabs.setCurrentIndex(1)
    page._panel_keys_scroll.verticalScrollBar().setValue(40)
    delay = True
    try:
        select(first, wait=False)
        qtbot.waitUntil(entered.is_set)
        qtbot.wait(25)
    finally:
        release.set()
    qtbot.waitUntil(lambda: page._keys_panel_mod_id == first.mod_id)
    qtbot.wait(15)
    assert page._panel_keys_scroll.verticalScrollBar().value() == 120
    page._detail_tabs.setCurrentIndex(2)
    assert page._variant_panel.scroll_area.verticalScrollBar().value() == 300
    page._detail_tabs.setCurrentIndex(0)
    assert page._panel_info_scroll.verticalScrollBar().value() == 60


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_narrow_short_preview_keeps_variant_rows_and_all_actions_reachable(qtbot, temp_data_dir, appearance, mode):
    from tests.ui.test_mod_variant_panel import state, variant

    appearance(mode)
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(1024, 680)
    mod = Mod(mod_id="narrow", game_id="game_a", name="Native narrow preview")
    page._selected_mod = mod
    page._show_mod_panel(mod)
    page._detail_tabs.setCurrentIndex(2)
    page.set_variant_state(mod.mod_id, state(mod_id=mod.mod_id), [variant(str(i), f"Variant {i}") for i in range(27)])
    page.show()
    page._main_splitter.setSizes([600, 380])
    page._panel_splitter.setSizes([230, 340])
    qtbot.wait(20)
    buttons = [page._panel_toggle_btn, page._panel_edit_btn, page._panel_open_btn, page._panel_delete_btn]
    assert len({button.mapTo(page._preview_panel, QPoint(0, 0)).y() for button in buttons}) == 1
    assert page._variant_panel.scroll_area.viewport().height() >= 100
    assert page._variant_panel.scroll_area.verticalScrollBar().maximum() > 0
