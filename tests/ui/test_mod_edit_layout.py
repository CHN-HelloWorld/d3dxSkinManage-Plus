from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QDialog, QScrollArea

from src.models.mod import Mod, ModInfo
from src.ui import theme
from src.ui.dialogs.mod_edit import ModEditDialog
from tests.ui.tag_helpers import reveal_widget


@pytest.fixture
def show_editor(qtbot, qapp, temp_data_dir, monkeypatch):
    previous_style = qapp.styleSheet()
    previous_theme = theme.current_theme()
    theme.set_theme("light")
    qapp.setStyleSheet(theme.render_qss("light"))
    dialogs = []

    def show(mod):
        dialog = ModEditDialog(mod)
        qtbot.addWidget(dialog)
        dialogs.append(dialog)
        monkeypatch.setattr(dialog, "screen", lambda: SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1440, 900)))
        dialog.show()
        qtbot.wait(80)
        return dialog

    yield show
    for dialog in dialogs:
        dialog.done(QDialog.DialogCode.Rejected)
    theme.set_theme(previous_theme)
    qapp.setStyleSheet(previous_style)


def _long_mod():
    return Mod(
        mod_id="1234567890abcdef1234567890abcdef12345678",
        name="月下礼服",
        path="D:\\Mods\\" + "角色外观与材质收藏\\" * 35 + "月下礼服_v1.2",
        info=ModInfo(author="Studio", description="较长的使用说明。\n" * 100),
    )


def _visible_in_scroll(dialog, widget):
    if not widget.isVisibleTo(dialog):
        return False
    ancestor = widget.parentWidget()
    while ancestor is not None and ancestor is not dialog:
        if isinstance(ancestor, QScrollArea):
            viewport = ancestor.viewport()
            if not viewport.rect().contains(QRect(widget.mapTo(viewport, QPoint()), widget.size())):
                return False
        ancestor = ancestor.parentWidget()
    return True


def test_long_metadata_does_not_increase_initial_window_height(show_editor):
    short_dialog = show_editor(Mod(mod_id="short", name="Short", path="D:\\Mods\\Short"))
    short_size = short_dialog.size()
    short_dialog.done(QDialog.DialogCode.Rejected)
    long_dialog = show_editor(_long_mod())

    assert long_dialog.size() == short_size
    assert _visible_in_scroll(long_dialog, long_dialog._name_input)
    assert _visible_in_scroll(long_dialog, long_dialog._desc_input)


def test_many_tags_keep_search_and_primary_fields_visible(show_editor, qtbot):
    names = [f"月光礼服高清材质与星空配色方案{number:02d}" for number in range(20)]
    dialog = show_editor(Mod(name="Many tags", info=ModInfo(tags=names)))
    picker = dialog.tag_picker

    assert _visible_in_scroll(dialog, picker.input)
    assert _visible_in_scroll(dialog, dialog._desc_input)
    assert dialog._form_scroll.verticalScrollBar().maximum() == 0
    assert picker.draft.names == names
    bar = picker._selected_scroll.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(bar.maximum())
    qtbot.wait(20)
    assert _visible_in_scroll(dialog, picker.input)
    assert picker.draft.names == names


def test_backspace_reveals_the_tag_before_removing_it(show_editor, qtbot):
    names = [f"月光礼服高清材质与星空配色方案{number:02d}" for number in range(20)]
    dialog = show_editor(Mod(name="Many tags", info=ModInfo(tags=names)))
    picker = dialog.tag_picker
    picker.input.setFocus()
    qtbot.keyClick(picker.input, Qt.Key.Key_Backspace)
    qtbot.wait(50)

    assert picker.draft.names == names
    chip = picker._flow.itemAt(picker._flow.count() - 1).widget()
    assert chip.isChecked()
    viewport = picker._selected_scroll.viewport()
    assert viewport.rect().contains(QRect(chip.mapTo(viewport, QPoint()), chip.size()))

    qtbot.keyClick(picker.input, Qt.Key.Key_Backspace)
    assert picker.draft.names == names[:-1]


def test_color_confirmation_keeps_candidate_actions_visible(show_editor, qtbot):
    dialog = show_editor(Mod(name="Colors", info=ModInfo(tags=["First", "Second"])))
    picker = dialog.tag_picker
    first, second = picker.draft.selected_ids
    reveal_widget(qtbot, picker._field)
    picker.open_editor(first)
    reveal_widget(qtbot, picker.editor_card.custom_button)
    qtbot.mouseClick(picker.editor_card.custom_button, Qt.MouseButton.LeftButton)
    color_dialog = picker.editor_card.color_dialog
    color_dialog.setCurrentColor(QColor("#123456"))
    color_dialog.accept()
    qtbot.wait(80)

    viewport = picker._candidate_scroll.viewport()
    button = picker._rows[second].edit_button
    reveal_widget(qtbot, button)
    assert viewport.rect().contains(QRect(button.mapTo(viewport, QPoint()), button.size()))
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert picker._active_tag_id == second


def test_file_details_expand_without_resizing_and_copy_original_values(show_editor, qtbot, qapp):
    mod = _long_mod()
    dialog = show_editor(mod)
    initial_size = dialog.size()
    clipboard = qapp.clipboard()
    previous_clipboard = clipboard.text()
    try:
        assert not dialog._path_label.isVisible()
        qtbot.mouseClick(dialog._info_toggle, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(dialog._path_label.isVisible)
        assert dialog.size() == initial_size
        assert dialog._path_label.toPlainText() == mod.path
        assert dialog._path_label.verticalScrollBar().maximum() > 0

        dialog._form_scroll.ensureWidgetVisible(dialog._copy_path_button)
        qtbot.mouseClick(dialog._copy_path_button, Qt.MouseButton.LeftButton)
        assert clipboard.text() == mod.path
        # 下一次复制替换 OLE 对象前，先让原生剪贴板监听器完成读取。
        qtbot.wait(80)
        dialog._form_scroll.ensureWidgetVisible(dialog._copy_id_button)
        qtbot.mouseClick(dialog._copy_id_button, Qt.MouseButton.LeftButton)
        assert clipboard.text() == mod.mod_id

        dialog.reject()
        assert dialog.discard_dialog is None
        assert not dialog.isVisible()
    finally:
        clipboard.setText(previous_clipboard)


def test_width_changes_do_not_override_user_height(show_editor, qtbot):
    dialog = show_editor(_long_mod())
    dialog.resize(480, 500)
    qtbot.wait(90)
    assert dialog.height() == 500
    assert dialog._form_scroll.horizontalScrollBar().maximum() == 0
    dialog._form_scroll.ensureWidgetVisible(dialog.tag_picker.input)
    assert _visible_in_scroll(dialog, dialog.tag_picker.input)
    position = dialog._save_button.mapTo(dialog, QPoint())
    assert position.y() + dialog._save_button.height() < dialog.height()

    dialog.resize(820, 500)
    qtbot.wait(90)
    assert dialog.height() == 500
    assert dialog._form_scroll.horizontalScrollBar().maximum() == 0


def test_reopening_editor_restores_tag_keyboard_and_popup(show_editor, qtbot):
    dialog = show_editor(Mod(name="Reopen", info=ModInfo(tags=["Original"])))
    dialog.done(QDialog.DialogCode.Rejected)
    dialog.show()
    picker = dialog.tag_picker
    picker.input.setFocus()
    picker.input.setText("Added after reopening")
    qtbot.keyClick(picker.input, Qt.Key.Key_Return)
    assert picker.draft.names == ["Original", "Added after reopening"]
    assert picker.popup.isVisible()
    qtbot.keyClick(picker.input, Qt.Key.Key_Escape)
    assert not picker.popup.isVisible()
    assert dialog.isVisible()


def test_tag_editor_uses_an_overlay_without_resizing_the_form(show_editor, qtbot):
    dialog = show_editor(Mod(name="Inline", info=ModInfo(tags=["First", "Second"])))
    size = dialog.size()
    picker = dialog.tag_picker
    reveal_widget(qtbot, picker._field)
    qtbot.mouseClick(picker._flow.itemAt(0).widget(), Qt.MouseButton.LeftButton)
    qtbot.wait(80)

    assert picker.popup.parentWidget() is dialog
    assert not dialog._form_content.isAncestorOf(picker.popup)
    assert picker.popup.isAncestorOf(picker.editor_card)
    assert dialog.rect().contains(picker.popup.geometry())
    assert picker.popup.width() <= picker.width()
    reveal_widget(qtbot, picker.name_input)
    reveal_widget(qtbot, picker.hex_input)
    assert dialog.size() == size
    assert dialog._form_scroll.horizontalScrollBar().maximum() == 0


def test_floating_editor_closes_when_another_form_field_is_selected(show_editor, qtbot):
    dialog = show_editor(Mod(name="Inline narrow", info=ModInfo(tags=["First", "Second"])))
    dialog.resize(480, 500)
    picker = dialog.tag_picker
    picker.open_editor(picker.draft.selected_ids[0])
    qtbot.wait(80)
    dialog._form_scroll.verticalScrollBar().setValue(0)
    dialog._author_input.setFocus()
    qtbot.mouseClick(dialog._author_input, Qt.MouseButton.LeftButton)
    qtbot.wait(80)

    assert not picker.popup.isVisible()
    assert dialog.height() == 500
    assert dialog._form_scroll.horizontalScrollBar().maximum() == 0


@pytest.mark.parametrize("tag_count", [2, 20])
def test_long_tag_editor_controls_remain_accessible_inside_the_overlay(show_editor, qtbot, tag_count):
    names = [f"LongTagNameForReview{number:02d}" for number in range(tag_count)]
    dialog = show_editor(Mod(name="Long tag editor", info=ModInfo(tags=names)))
    picker = dialog.tag_picker
    reveal_widget(qtbot, picker._field)
    picker.open_editor(picker.draft.selected_ids[0])
    picker.name_input.setText("Kept during reparent")
    qtbot.wait(100)

    for scroll in (dialog._side_scroll, picker._selected_scroll, picker._candidate_scroll, picker._editor_scroll):
        assert scroll.horizontalScrollBar().maximum() == 0
    reveal_widget(qtbot, picker.name_input)
    reveal_widget(qtbot, picker.hex_input)
    reveal_widget(qtbot, picker.editor_card.custom_button)
    button = picker._rows[picker.draft.selected_ids[-1]].remove_button
    reveal_widget(qtbot, button)


def test_add_tag_icon_commits_pending_text_without_saving_the_dialog(show_editor, qtbot):
    dialog = show_editor(Mod(name="Add tag", info=ModInfo(tags=["First"])))
    picker = dialog.tag_picker
    picker.input.setText("Second")
    qtbot.mouseClick(picker._add_button, Qt.MouseButton.LeftButton)
    assert picker.draft.names == ["First", "Second"]
    assert picker.input.text() == ""
    assert dialog.get_result() is None
    assert dialog.isVisible()
