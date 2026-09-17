from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QLineEdit, QStackedWidget, QVBoxLayout, QWidget


def activate(widget):
    widget.window().activateWindow()
    widget.setFocus()
    QCoreApplication.processEvents()


def test_overview_shortcuts_use_selection_and_do_not_toggle_while_typing(qtbot, temp_data_dir):
    from src.ui.pages.overview import OverviewPage
    from src.ui.shortcut_manager import ShortcutDispatcher, ShortcutSettings

    page = OverviewPage()
    qtbot.addWidget(page)
    settings = ShortcutSettings(page)
    dispatcher = ShortcutDispatcher(settings, page)
    page.install_shortcuts(dispatcher)
    page._display_items = [{"id": "mod-a", "type": "mod", "data": SimpleNamespace(mod_id="mod-a")}]
    page._selected_ids = {"mod-a"}
    page.show()
    activate(page._scroll_area)
    toggles, edits, exports = [], [], []
    page.mod_toggle_requested.connect(toggles.append)
    page.mod_edit_requested.connect(edits.append)
    page.items_export_requested.connect(lambda cats, mods: exports.append((cats, mods)))
    qtbot.keyClick(page._scroll_area, Qt.Key.Key_Space)
    qtbot.keyClick(page._scroll_area, Qt.Key.Key_F2)
    qtbot.keyClick(
        page._scroll_area, Qt.Key.Key_E, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    )
    assert toggles == ["mod-a"]
    assert edits == ["mod-a"]
    assert exports == [([], ["mod-a"])]
    page._search_input.setText("hello")
    activate(page._search_input)
    qtbot.keyClick(page._search_input, Qt.Key.Key_Space)
    assert page._search_input.text() == "hello "
    assert toggles == ["mod-a"]
    page._search_timer.stop()


def test_multi_selection_does_not_turn_into_an_implicit_batch_toggle(qtbot, temp_data_dir):
    from src.ui.pages.overview import OverviewPage
    from src.ui.shortcut_manager import ShortcutDispatcher, ShortcutSettings

    page = OverviewPage()
    qtbot.addWidget(page)
    dispatcher = ShortcutDispatcher(ShortcutSettings(page), page)
    page.install_shortcuts(dispatcher)
    page._display_items = [{"id": mid, "type": "mod", "data": SimpleNamespace(mod_id=mid)} for mid in ("a", "b")]
    page._selected_ids = {"a", "b"}
    page.show()
    activate(page._scroll_area)
    toggles = []
    page.mod_toggle_requested.connect(toggles.append)
    qtbot.keyClick(page._scroll_area, Qt.Key.Key_Space)
    assert toggles == []


def test_window_search_navigation_and_settings_search_follow_active_page(qtbot, temp_data_dir):
    from src.ui.shortcut_manager import ShortcutDispatcher, ShortcutSettings
    from src.ui.window_shortcuts import install_window_shortcuts

    window = QWidget()
    qtbot.addWidget(window)
    layout = QVBoxLayout(window)
    window._stack = QStackedWidget()
    layout.addWidget(window._stack)
    for index, name in enumerate(("GAME", "OVERVIEW", "GALLERY", "STORE", "SETTINGS", "PLUGINS")):
        setattr(window, "PAGE_" + name, index)
        page = QWidget()
        window._stack.addWidget(page)
        setattr(window, "_" + ("gb_store" if name == "STORE" else name.lower()) + "_page", page)
    for page, attribute in (
        (window._game_page, "_search"),
        (window._overview_page, "_search_input"),
        (window._settings_page, "_settings_search"),
    ):
        box = QLineEdit(page)
        box.setText("existing text")
        setattr(page, attribute, box)
    window._select_nav_page = window._stack.setCurrentIndex
    window._on_launch_migoto = lambda: None
    window._on_launch_game = lambda: None
    window._overview_page._current_game_id = ""
    window._overview_page._on_add_character = lambda: None
    window._overview_page._navigate_back = lambda: None
    settings = ShortcutSettings(window)
    dispatcher = ShortcutDispatcher(settings, window)
    install_window_shortcuts(window, dispatcher)
    window.show()
    activate(window._game_page._search)
    qtbot.keyClick(window._game_page._search, Qt.Key.Key_2, Qt.KeyboardModifier.ControlModifier)
    assert window._stack.currentWidget() is window._overview_page
    activate(window._overview_page._search_input)
    qtbot.keyClick(window._overview_page._search_input, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert window._overview_page._search_input.selectedText() == "existing text"
    qtbot.keyClick(window._overview_page._search_input, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
    assert window._stack.currentWidget() is window._settings_page
    assert window._settings_page._settings_search.hasFocus()
    assert window._settings_page._settings_search.selectedText() == "existing text"
