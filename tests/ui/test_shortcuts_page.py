import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QScrollArea


@pytest.fixture
def page(qtbot, temp_data_dir):
    from src.ui.pages.settings import SettingsPage

    settings = SettingsPage()
    qtbot.addWidget(settings)
    settings.resize(864, 645)
    settings.show()
    settings.activateWindow()
    settings._settings_nav_btns[2].click()
    QCoreApplication.processEvents()
    assert hasattr(settings, "_shortcuts_page"), "设置页应集成快捷键子页"
    return settings._shortcuts_page


def record(page, command_id, qtbot):
    qtbot.mouseClick(page.binding_buttons[command_id], Qt.MouseButton.LeftButton)
    QCoreApplication.processEvents()
    dialog = page._dialog
    dialog.activateWindow()
    dialog.capture.setFocus()
    QCoreApplication.processEvents()
    return dialog


def test_settings_has_only_real_commands_and_shared_footer(page):
    from src.core.shortcuts import COMMAND_BY_ID

    assert set(page.binding_buttons) == set(COMMAND_BY_ID)
    labels = {label.text() for label in page.findChildren(QLabel)}
    assert "通用" in labels and "Mod 与类别" in labels
    assert "打开设置" in labels and "搜索当前页面" in labels
    assert not labels & {"打开预设列表", "启用 / 禁用所选 Mod"}
    assert page.scroll.horizontalScrollBar().maximum() == 0
    assert len(page.window()._stack.currentWidget().findChildren(QScrollArea)) == 1
    footer = page.window()._settings_footer
    assert footer.isVisible()
    assert any(label.text() == "更改即时生效并自动保存" for label in footer.findChildren(QLabel))
    reset_buttons = [
        button for button in page.window().findChildren(QPushButton) if button.isVisible() and "恢复" in button.text()
    ]
    assert len(reset_buttons) == 1
    assert reset_buttons[0].text() == "恢复默认"
    assert reset_buttons[0].isEnabled()


def test_record_cancel_does_not_persist_and_returns_focus(page, qtbot):
    before = page.settings.bindings
    dialog = record(page, "edit.copy", qtbot)
    qtbot.keyClick(
        dialog.capture, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    )
    assert dialog.candidate == "Ctrl+Shift+K"
    assert dialog.save_button.isEnabled()
    assert page.settings.bindings == before
    qtbot.keyClick(dialog.capture, Qt.Key.Key_Escape)
    QCoreApplication.processEvents()
    assert page.settings.bindings == before
    assert page.binding_buttons["edit.copy"].hasFocus()


def test_conflict_replace_save_and_undo_are_atomic(page, qtbot):
    before = page.settings.bindings
    dialog = record(page, "view.refresh", qtbot)
    qtbot.keyClick(dialog.capture, Qt.Key.Key_I, Qt.KeyboardModifier.ControlModifier)
    assert dialog.save_button.text() == "替换并保存"
    assert "导入 Mod" in dialog.feedback.text()
    assert "未绑定" in dialog.feedback.text()
    assert page.settings.bindings == before
    qtbot.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
    assert page.settings.binding("view.refresh") == "Ctrl+I"
    assert page.settings.binding("mod.import") is None
    qtbot.mouseClick(page.toast.undo_button, Qt.MouseButton.LeftButton)
    assert page.settings.bindings == before


def test_single_reset_only_generates_a_candidate(page, qtbot):
    assert page.settings.save_binding("edit.copy", "Ctrl+Shift+C")
    dialog = record(page, "edit.copy", qtbot)
    qtbot.mouseClick(dialog.reset_button, Qt.MouseButton.LeftButton)
    assert dialog.candidate == "Ctrl+C"
    assert page.settings.binding("edit.copy") == "Ctrl+Shift+C"
    dialog.reject()
    assert page.settings.binding("edit.copy") == "Ctrl+Shift+C"


def test_row_restore_prepares_default_without_saving(page, qtbot):
    assert page.settings.save_binding("edit.copy", "Ctrl+Shift+C")
    restore = page._rows["edit.copy"].restore_button
    assert restore.isEnabled()
    qtbot.mouseClick(restore, Qt.MouseButton.LeftButton)
    assert page._dialog.candidate == "Ctrl+C"
    assert page.settings.binding("edit.copy") == "Ctrl+Shift+C"
    page._dialog.reject()
    assert page.settings.binding("edit.copy") == "Ctrl+Shift+C"


def test_invalid_same_and_suspended_recording_cannot_save(page, qtbot):
    dialog = record(page, "edit.copy", qtbot)
    qtbot.keyClick(dialog.capture, Qt.Key.Key_A)
    assert not dialog.save_button.isEnabled()
    assert "Ctrl" in dialog.feedback.text()
    qtbot.keyClick(dialog.capture, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert not dialog.save_button.isEnabled()
    assert "与当前绑定相同" in dialog.feedback.text()
    qtbot.keyClick(dialog.capture, Qt.Key.Key_F6)
    assert dialog.save_button.isEnabled()
    QApplication.sendEvent(dialog, QEvent(QEvent.Type.WindowDeactivate))
    assert not dialog.save_button.isEnabled()
    assert "失焦" in dialog.feedback.text()
    QApplication.sendEvent(dialog, QEvent(QEvent.Type.WindowActivate))
    assert dialog.save_button.isEnabled()
    dialog.reject()


def test_failed_save_stays_in_dialog_and_does_not_change_row(page, qtbot, monkeypatch):
    from src.core import config as config_module

    def denied(*_args):
        raise PermissionError("read-only")

    before = page.settings.bindings
    monkeypatch.setattr(config_module, "write_json_atomic", denied)
    dialog = record(page, "view.refresh", qtbot)
    qtbot.keyClick(dialog.capture, Qt.Key.Key_I, Qt.KeyboardModifier.ControlModifier)
    qtbot.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
    assert dialog.isVisible()
    assert page.settings.bindings == before
    assert "保存失败" in dialog.feedback.text()
    dialog.reject()


def test_recording_buttons_fit_and_tab_enter_work_at_minimum_size(page, qtbot):
    dialog = record(page, "mod.import", qtbot)
    qtbot.keyClick(dialog.capture, Qt.Key.Key_F6)
    for button in dialog.card.findChildren(QPushButton):
        assert button.width() >= button.minimumSizeHint().width()
        assert dialog.card.rect().contains(button.mapTo(dialog.card, button.rect().bottomRight()))
    qtbot.keyClick(dialog.capture, Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is dialog.again_button
    dialog.save_button.setFocus()
    qtbot.keyClick(dialog.save_button, Qt.Key.Key_Return)
    assert page.settings.binding("mod.import") == "F6"


def test_system_interactions_can_be_read_and_expanded(page, qtbot):
    assert page.system_toggle.height() >= 45
    qtbot.mouseClick(page.system_toggle, Qt.MouseButton.LeftButton)
    assert page.system_toggle.isChecked()
    assert any(label.isVisible() and "取消录制、取消剪切" in label.text() for label in page.findChildren(QLabel))


def test_composition_remains_protected_until_input_method_finishes(page, qtbot):
    dialog = record(page, "edit.copy", qtbot)
    QApplication.sendEvent(dialog.capture, QInputMethodEvent("拼", []))
    qtbot.keyClick(dialog.capture, Qt.Key.Key_F6)
    assert dialog.candidate is None
    assert not dialog.save_button.isEnabled()
    QApplication.sendEvent(dialog.capture, QInputMethodEvent("", []))
    qtbot.keyClick(dialog.capture, Qt.Key.Key_F6)
    assert dialog.candidate == "F6"
    dialog.reject()


def test_system_toggle_accepts_enter_with_keyboard_focus(page, qtbot):
    page.system_toggle.setFocus()
    qtbot.keyClick(page.system_toggle, Qt.Key.Key_Return)
    assert page.system_toggle.isChecked()
