from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QPushButton

from src.models.preset import Preset
from src.ui.widgets.preset_selector import PresetPopup


def make_popup(qtbot):
    popup = PresetPopup()
    qtbot.addWidget(popup)
    popup.resize(400, 390)
    presets = [Preset(id="default", name="默认", is_default=True)]
    presets += [
        Preset(id=key, name=name) for key, name in (("daily", "日常搭配"), ("photo", "拍照搭配"), ("other", "另一组合"))
    ]
    popup.set_presets(presets, "daily")
    popup.show()
    return popup, presets


def row(popup, index):
    return popup._list.itemWidget(popup._list.item(index))


def button(popup, name):
    control = popup.findChild(QPushButton, name)
    assert control is not None, f"Preset management needs a visible {name} action"
    return control


def test_visible_rename_action_edits_only_selected_preset(qtbot):
    popup, _ = make_popup(qtbot)
    changed = QSignalSpy(popup.rename_requested)
    qtbot.mouseClick(row(popup, 2), Qt.MouseButton.LeftButton)
    qtbot.mouseClick(button(popup, "presetRename"), Qt.MouseButton.LeftButton)
    edit = row(popup, 2)._name_edit
    assert edit.isVisible()
    edit.setText("新的拍照搭配")
    qtbot.keyClick(edit, Qt.Key.Key_Return)
    assert [changed.at(index) for index in range(changed.count())] == [["photo", "新的拍照搭配"]]


def test_visible_copy_duplicates_selection_without_applying_it(qtbot):
    popup, _ = make_popup(qtbot)
    copied = QSignalSpy(popup.duplicate_requested)
    applied = QSignalSpy(popup.apply_requested)
    qtbot.mouseClick(row(popup, 1), Qt.MouseButton.LeftButton)
    qtbot.mouseClick(row(popup, 3), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier)
    qtbot.mouseClick(button(popup, "presetDuplicate"), Qt.MouseButton.LeftButton)
    assert [copied.at(index) for index in range(copied.count())] == [[["daily", "other"]]]
    assert [applied.at(index) for index in range(applied.count())] == []


def test_management_refresh_keeps_selected_ids(qtbot):
    popup, presets = make_popup(qtbot)
    qtbot.mouseClick(row(popup, 2), Qt.MouseButton.LeftButton)
    qtbot.mouseClick(row(popup, 3), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier)
    presets[2].name = "新名称"
    popup.set_presets(presets, "daily")
    assert popup._selected_ids() == ["photo", "other"]


def test_delete_cannot_remove_unselected_or_default_preset(qtbot):
    popup, _ = make_popup(qtbot)
    deleted = QSignalSpy(popup.delete_requested)
    delete = button(popup, "presetDelete")
    popup._list.clearSelection()
    assert not delete.isEnabled()
    qtbot.mouseClick(delete, Qt.MouseButton.LeftButton)
    assert [deleted.at(index) for index in range(deleted.count())] == []
    qtbot.mouseClick(row(popup, 0), Qt.MouseButton.LeftButton)
    assert not delete.isEnabled()
    qtbot.mouseClick(row(popup, 2), Qt.MouseButton.LeftButton)
    assert delete.isEnabled()
    qtbot.mouseClick(delete, Qt.MouseButton.LeftButton)
    assert [deleted.at(index) for index in range(deleted.count())] == [[["photo"]]]


def test_shift_selection_and_clipboard_keep_windows_semantics(qtbot):
    popup, _ = make_popup(qtbot)
    applied = QSignalSpy(popup.apply_requested)
    copied = QSignalSpy(popup.duplicate_requested)
    qtbot.mouseClick(row(popup, 1), Qt.MouseButton.LeftButton)
    qtbot.mouseClick(row(popup, 3), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
    assert popup._selected_ids() == ["daily", "photo", "other"]
    popup._on_copy()
    popup._list.clearSelection()
    popup._on_paste()
    assert [copied.at(index) for index in range(copied.count())] == [[["daily", "photo", "other"]]]
    assert [applied.at(index) for index in range(applied.count())] == []


def test_row_application_remains_explicit_and_current_is_disabled(qtbot):
    popup, _ = make_popup(qtbot)
    applied = QSignalSpy(popup.apply_requested)
    assert not row(popup, 1)._apply_btn.isEnabled()
    qtbot.mouseClick(row(popup, 2), Qt.MouseButton.LeftButton)
    assert applied.count() == 0
    qtbot.mouseClick(row(popup, 2)._apply_btn, Qt.MouseButton.LeftButton)
    assert [applied.at(index) for index in range(applied.count())] == [["photo"]]
    assert not popup.isVisible()


def test_new_preset_rename_survives_showing_popup(qtbot):
    from src.ui.widgets.preset_selector import PresetSelector

    selector = PresetSelector()
    qtbot.addWidget(selector)
    selector.show()
    selector.set_presets([Preset(id="new", name="新建预设")], "", rename_id="new")
    edit = row(selector._popup, 0)._name_edit
    assert edit.isVisible()
    assert edit.hasFocus()
    assert edit.selectedText() == "新建预设"
    selector._popup.close()
