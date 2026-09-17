import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from src.core.config import Config
from src.ui.pages.settings import SettingsPage
from src.utils.i18n import t


@pytest.mark.parametrize("action", ["cancel", "escape", "close", "enter", "confirm"])
@pytest.mark.parametrize("section", [0, 2], ids=["general", "shortcuts"])
def test_reset_only_changes_preferences_after_confirmation(qtbot, temp_data_dir, action, section):
    config = Config()
    config.set("mods.delete_method", "recycle_bin")
    page = SettingsPage()
    qtbot.addWidget(page)
    page.show()
    page._settings_nav_btns[section].click()
    assert page._shortcut_settings.save_binding("edit.copy", "Ctrl+Shift+C")
    before = config._config_path.read_bytes()
    changed = QSignalSpy(page.settings_changed)
    background_changed = QSignalSpy(page.background_changed)
    shown = []

    def respond():
        dialog = QApplication.activeModalWidget()
        if not isinstance(dialog, QMessageBox):
            if dialog is not None:
                dialog.close()
            return
        shown.append(
            (
                config._config_path.read_bytes(),
                changed.count(),
                background_changed.count(),
                dialog.defaultButton() is dialog.button(QMessageBox.StandardButton.Cancel),
            )
        )
        if action == "confirm":
            qtbot.mouseClick(dialog.button(QMessageBox.StandardButton.Yes), Qt.MouseButton.LeftButton)
        elif action == "cancel":
            qtbot.mouseClick(dialog.button(QMessageBox.StandardButton.Cancel), Qt.MouseButton.LeftButton)
        elif action == "escape":
            qtbot.keyClick(dialog, Qt.Key.Key_Escape)
        elif action == "enter":
            qtbot.keyClick(dialog, Qt.Key.Key_Return)
        else:
            dialog.close()

    timer = QTimer(page)
    timer.setSingleShot(True)
    timer.timeout.connect(respond)
    timer.start(0)
    try:
        button = next(
            button for button in page._settings_footer.findChildren(QPushButton) if button.text() == t("恢复默认")
        )
        assert button.isVisible()
        button.click()
    finally:
        timer.stop()

    assert shown, "Restore defaults must ask before changing any preferences"
    assert shown == [(before, 0, 0, True)]
    config.reload()
    if action == "confirm":
        assert config.get("mods.delete_method") == "permanent"
        assert page._delete_method_combo.currentData() == "permanent"
        assert page._shortcut_settings.binding("edit.copy") == "Ctrl+C"
        assert page._shortcut_settings.custom_count == 0
        assert page._shortcuts_page._rows["edit.copy"].custom.isHidden()
        assert changed.count() > 0
        assert background_changed.count() == 1
    else:
        assert config._config_path.read_bytes() == before
        assert config.get("mods.delete_method") == "recycle_bin"
        assert page._delete_method_combo.currentData() == "recycle_bin"
        assert page._shortcut_settings.binding("edit.copy") == "Ctrl+Shift+C"
        assert changed.count() == background_changed.count() == 0
