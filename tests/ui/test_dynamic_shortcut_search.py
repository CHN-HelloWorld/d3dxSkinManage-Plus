import pytest
import shiboken6
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QPushButton

from src.core.shortcuts import ShortcutCommand
from src.ui.pages.settings import SettingsPage
from src.ui.shortcut_manager import ShortcutSettings

pytestmark = pytest.mark.usefixtures("native_qt_teardown")


@pytest.fixture
def settings_page(qtbot, temp_data_dir, monkeypatch):
    monkeypatch.setattr(SettingsPage, "_refresh_cache_usage", lambda self: None)
    shortcuts = ShortcutSettings()
    page = SettingsPage(shortcuts=shortcuts)
    qtbot.addWidget(page)
    page.resize(1250, 850)
    page.show()
    qtbot.waitUntil(page.isVisible)
    return page, shortcuts


def plugin_command():
    return ShortcutCommand(
        "test.dynamic.action",
        "plugin:test.dynamic",
        "Dynamic action",
        "Run dynamic action",
        "Ctrl+Alt+L",
        frozenset({"window"}),
    )


def drain_deferred_deletes(qapp):
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def test_active_host_search_remaps_to_live_shortcut_after_catalog_rebuild(settings_page, qtbot, qapp):
    page, shortcuts = settings_page
    page._settings_search.setText("F5")
    old_target = page._shortcuts_page.binding_buttons["view.refresh"]
    assert any(entry.target is old_target for entry in page._search_entries)

    shortcuts.register_plugin("test.dynamic", "Dynamic", [plugin_command()])
    drain_deferred_deletes(qapp)

    current_target = page._shortcuts_page.binding_buttons["view.refresh"]
    assert not shiboken6.isValid(old_target)
    assert current_target is not old_target
    assert any(entry.target is current_target for entry in page._search_entries)
    assert all(shiboken6.isValid(entry.target) for entry in page._search_entries)

    result = next(button for button in page._search_results.findChildren(QPushButton) if button.isVisible())
    qtbot.mouseClick(result, Qt.MouseButton.LeftButton)
    assert page._highlighted_setting is current_target
    page._open_search_result(2, "keymap", old_target)


def test_removed_plugin_search_result_disappears_and_stale_activation_is_safe(settings_page, qapp):
    page, shortcuts = settings_page
    command = plugin_command()
    shortcuts.register_plugin("test.dynamic", "Dynamic", [command])
    page._settings_search.setText("Dynamic action")
    old_target = page._shortcuts_page.binding_buttons[command.id]
    assert any(entry.target is old_target for entry in page._search_entries)

    shortcuts.unregister_plugin("test.dynamic")
    drain_deferred_deletes(qapp)

    assert not shiboken6.isValid(old_target)
    assert all(entry.target is not old_target for entry in page._search_entries)
    assert not any(button.isVisible() for button in page._search_results.findChildren(QPushButton))
    page._open_search_result(2, "keymap", old_target)
