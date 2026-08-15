from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QHelpEvent, QInputMethodEvent
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton, QScrollArea, QToolTip, QVBoxLayout, QWidget

from src.core.mod_index import ModIndex
from src.core.tag_service import TagService
from src.models.mod import ModInfo
from src.models.tag import TagDefinition
from tests.helpers import make_mod, register_games, reset_game_service


@pytest.fixture
def tag_data(temp_data_dir):
    register_games("game-a")
    index = ModIndex("game-a")
    for mod_id, tags in (("one", ["Shared"]), ("two", ["Shared", "Other"])):
        assert index.add_mod(make_mod(index, mod_id, mod_id=mod_id, info=ModInfo(tags=tags)))
    yield index, TagService(index), index.get_mod("one")
    index.close()
    reset_game_service()


def make_picker(qtbot, draft):
    from src.ui.widgets.tag_picker import TagPicker

    host = QWidget()
    host.resize(520, 600)
    layout = QVBoxLayout(host)
    picker = TagPicker(draft, host)
    layout.addWidget(picker)
    layout.addStretch()
    boundary = QPushButton("保存", host)
    layout.addWidget(boundary)
    picker.set_popup_boundary(boundary)
    qtbot.addWidget(host)
    host.show()
    qtbot.waitUntil(host.isActiveWindow)
    picker.input.setFocus()
    return host, picker


def test_enter_adds_only_to_draft_and_pending_text_blocks_save(qtbot, tag_data):
    index, service, mod = tag_data
    host, picker = make_picker(qtbot, service.begin_edit(mod))
    picker.input.setText("new tag")
    assert not picker.validate()
    qtbot.keyClick(picker.input, Qt.Key.Key_Return)
    assert picker.validate()
    assert picker.draft.names == ["Shared", "new tag"]
    assert picker.input.text() == ""
    assert picker.popup.isVisible()
    assert index.get_mod("one").info.tags == ["Shared"]
    assert mod.info.tags == ["Shared"]


def test_context_paste_is_atomic_and_handles_all_separators(qtbot, tag_data):
    index, service, mod = tag_data
    host, picker = make_picker(qtbot, service.begin_edit(mod))
    QApplication.clipboard().setText("Valid,\n" + "x" * 25)
    # 下一次操作替换剪贴板前，先让原生监听器渲染 OLE 数据，避免连续同步写入阻塞。
    qtbot.wait(80)
    menu = picker.input.createStandardContextMenu()
    try:
        paste = next(action for action in menu.actions() if action.objectName() == "tag-paste")
        paste.trigger()
    finally:
        menu.deleteLater()
    assert picker.draft.names == ["Shared"]
    assert "Valid" in picker.input.text()
    assert not picker.validate()
    picker.input.clear()
    QApplication.clipboard().setText("  ＨＤ  Texture，Second\nThird\r\nHD Texture")
    qtbot.wait(80)
    picker.input.paste()
    assert picker.draft.names == ["Shared", "HD Texture", "Second", "Third"]
    assert picker.validate()
    assert index.get_mod("one").info.tags == ["Shared"]


def test_backspace_requires_two_presses_and_escape_closes_panel_first(qtbot, tag_data):
    _, service, mod = tag_data
    host, picker = make_picker(qtbot, service.begin_edit(mod))
    picker.show_popup()
    qtbot.keyClick(picker.input, Qt.Key.Key_Backspace)
    assert picker.draft.names == ["Shared"]
    qtbot.keyClick(picker.input, Qt.Key.Key_Backspace)
    assert picker.draft.names == []
    assert len(picker.draft.candidates()) == 2
    qtbot.keyClick(picker.input, Qt.Key.Key_Escape)
    assert not picker.popup.isVisible()
    assert host.isVisible()


def test_arrows_skip_selected_candidates(qtbot, tag_data):
    _, service, mod = tag_data
    host, picker = make_picker(qtbot, service.begin_edit(mod))
    qtbot.keyClick(picker.input, Qt.Key.Key_Down)
    qtbot.keyClick(picker.input, Qt.Key.Key_Return)
    assert picker.draft.names == ["Shared", "Other"]


def test_composition_never_commits_enter_or_comma(qtbot, tag_data):
    _, service, mod = tag_data
    host, picker = make_picker(qtbot, service.begin_edit(mod))
    QApplication.sendEvent(picker.input, QInputMethodEvent("高清", []))
    qtbot.keyClick(picker.input, Qt.Key.Key_Return)
    qtbot.keyClick(picker.input, Qt.Key.Key_Comma)
    assert picker.draft.names == ["Shared"]
    assert not picker.validate()
    committed = QInputMethodEvent()
    committed.setCommitString("高清")
    QApplication.sendEvent(picker.input, committed)
    assert picker.draft.names == ["Shared"]
    qtbot.keyClick(picker.input, Qt.Key.Key_Return)
    assert picker.draft.names == ["Shared", "高清"]


def test_shared_edit_preserves_id_invalid_local_name_and_pending_input(qtbot, tag_data):
    index, service, mod = tag_data
    host, picker = make_picker(qtbot, service.begin_edit(mod))
    tag_id = picker.draft.selected_ids[0]
    picker.input.setText("Shar")
    picker.open_editor(tag_id, from_candidate=True)
    assert picker.input.text() == ""
    picker.name_input.setText("Other")
    assert picker.draft.definitions[tag_id].name == "Shared"
    assert not picker.validate()
    assert picker.name_input.text() == "Other"
    assert picker.editor_card.isVisible()
    picker.name_input.setText("Updated")
    picker.hex_input.setText("#123abc")
    assert picker.draft.definitions[tag_id].name == "Updated"
    assert picker.draft.definitions[tag_id].color == "#123ABC"
    assert picker.validate()
    picker.input.setText("unfinished")
    picker.open_editor(tag_id)
    assert picker.input.text() == "unfinished"
    assert not picker.validate()
    assert index.get_mod("two").info.tags == ["Shared", "Other"]


def test_invalid_collapsed_editor_still_blocks_save(qtbot, tag_data):
    _, service, mod = tag_data
    host, picker = make_picker(qtbot, service.begin_edit(mod))
    picker.open_editor(picker.draft.selected_ids[0])
    picker.name_input.clear()
    picker.collapse_editor()
    assert not picker.validate()
    assert picker.editor_card.isVisible()
    assert picker.name_input.text() == ""
    assert picker.name_input.hasFocus()


def test_removal_confirmation_defaults_to_cancel_and_stages_all_references(qtbot, tag_data):
    index, service, mod = tag_data
    host, picker = make_picker(qtbot, service.begin_edit(mod))
    tag_id = picker.draft.selected_ids[0]
    picker.show_popup()
    qtbot.mouseClick(picker._rows[tag_id].remove_button, Qt.MouseButton.LeftButton)
    confirmation = picker.removal_dialog
    qtbot.waitUntil(lambda: confirmation.isVisible())
    assert confirmation.cancel_button.isDefault()
    assert "2" in confirmation.usage_label.text()
    qtbot.mouseClick(confirmation.confirm_button, Qt.MouseButton.LeftButton)
    assert picker.draft.deleted_ids == {tag_id}
    assert picker.draft.names == []
    assert tag_id not in picker._rows
    assert {tag.id for tag in service.list_tags()} == set(picker.draft.original_definitions)
    assert index.get_mod("one").info.tags == ["Shared"]
    assert index.get_mod("two").info.tags == ["Shared", "Other"]


def test_removal_escape_preserves_draft(qtbot, tag_data):
    _, service, mod = tag_data
    host, picker = make_picker(qtbot, service.begin_edit(mod))
    picker.open_removal(picker.draft.selected_ids[0])
    qtbot.keyClick(picker.removal_dialog, Qt.Key.Key_Escape)
    assert not picker.draft.is_dirty
    assert picker.popup.isVisible()


def test_chip_clicks_do_not_reach_card_and_flow_wraps(qtbot, temp_data_dir):
    from src.ui.widgets.tag_chip import TagChip, TagFlowLayout

    class Card(QWidget):
        clicked = 0

        def mousePressEvent(self, event):
            self.clicked += 1

        def mouseDoubleClickEvent(self, event):
            self.clicked += 1

    card = Card()
    qtbot.addWidget(card)
    layout = TagFlowLayout(card)
    tag = TagDefinition("t", "g", "<b>very long plain label</b>", "label", "purple")
    chip = TagChip(tag, removable=True)
    chip.setMaximumWidth(120)
    layout.addWidget(chip)
    layout.addWidget(TagChip(tag))
    card.resize(240, 100)
    card.show()
    received = []
    chip.clicked_tag.connect(received.append)
    qtbot.mouseClick(chip, Qt.MouseButton.LeftButton, pos=QPoint(14, 12))
    qtbot.mouseDClick(chip, Qt.MouseButton.LeftButton, pos=QPoint(14, 12))
    assert received
    assert card.clicked == 0
    assert chip.toolTip() == tag.name
    assert tag.name in chip.accessibleName()
    assert chip.width() <= 120
    assert layout.heightForWidth(160) > layout.heightForWidth(500)


def test_popup_is_bounded_without_reflowing_host(qtbot, tag_data):
    _, service, mod = tag_data
    host, picker = make_picker(qtbot, service.begin_edit(mod))
    before = host.size()
    picker.open_editor(picker.draft.selected_ids[0])
    qtbot.wait(10)
    assert host.size() == before
    popup = picker.popup.frameGeometry()
    available = host.screen().availableGeometry()
    assert available.contains(popup)
    boundary_top = picker._popup_boundary.mapToGlobal(QPoint(0, 0)).y()
    assert popup.bottom() < boundary_top
    assert popup.height() <= 420


def test_tab_reaches_candidate_actions_and_leaving_field_closes_panel(qtbot, tag_data):
    _, service, mod = tag_data
    host, picker = make_picker(qtbot, service.begin_edit(mod))
    qtbot.keyClick(picker.input, Qt.Key.Key_Tab)
    focused = QApplication.focusWidget()
    assert isinstance(focused, QPushButton)
    assert focused.accessibleName().startswith("编辑标签 ")
    outside = QLineEdit(host)
    host.layout().insertWidget(0, outside)
    outside.show()
    outside.setFocus()
    qtbot.waitUntil(lambda: not picker.popup.isVisible())


def test_scrolling_tag_field_out_of_view_hides_popup_but_keeps_draft(qtbot, tag_data):
    from src.ui.widgets.tag_picker import TagPicker

    _, service, mod = tag_data
    host = QWidget()
    qtbot.addWidget(host)
    host.resize(520, 500)
    layout = QVBoxLayout(host)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    content = QWidget()
    content_layout = QVBoxLayout(content)
    picker = TagPicker(service.begin_edit(mod), content)
    content_layout.addWidget(picker)
    filler = QWidget()
    filler.setMinimumHeight(1000)
    content_layout.addWidget(filler)
    scroll.setWidget(content)
    layout.addWidget(scroll)
    host.show()
    qtbot.waitUntil(host.isActiveWindow)
    picker.input.setFocus()
    picker.input.setText("still pending")
    assert picker.popup.isVisible()
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    qtbot.waitUntil(lambda: not picker.popup.isVisible(), timeout=500)
    picker.show_popup()
    assert not picker.popup.isVisible()
    assert picker.input.text() == "still pending"
    assert picker.draft.names == ["Shared"]


def test_invalid_new_color_does_not_disappear_when_adding(qtbot, tag_data):
    _, service, mod = tag_data
    host, picker = make_picker(qtbot, service.begin_edit(mod))
    picker.input.setText("New")
    picker.hex_input.setText("#123")
    qtbot.keyClick(picker.input, Qt.Key.Key_Return)
    assert picker.draft.names == ["Shared"]
    assert picker.input.text() == "New"
    assert picker.hex_input.text() == "#123"
    picker.hex_input.setText("#123456")
    qtbot.keyClick(picker.input, Qt.Key.Key_Return)
    assert picker.draft.definitions[picker.draft.selected_ids[-1]].color == "#123456"


def test_duplicate_selection_keeps_explanation_and_query(qtbot, tag_data):
    _, service, mod = tag_data
    host, picker = make_picker(qtbot, service.begin_edit(mod))
    picker.input.setText("shared")
    qtbot.keyClick(picker.input, Qt.Key.Key_Return)
    assert picker.draft.names == ["Shared"]
    assert picker.input.text() == "shared"
    assert "重复" in picker.error_label.text()


def test_theme_change_repaints_editor_without_changing_custom_color(qtbot, tag_data):
    from src.ui import theme

    _, service, mod = tag_data
    host, picker = make_picker(qtbot, service.begin_edit(mod))
    tag_id = picker.draft.selected_ids[0]
    picker.open_editor(tag_id)
    picker.hex_input.setText("#123ABC")
    app = QApplication.instance()
    previous_theme, previous_sheet = theme.current_theme(), app.styleSheet()
    try:
        for mode, background in (("dark", "#252C32"), ("light", "#F2F7FB")):
            theme.set_theme(mode)
            app.setStyleSheet(theme.render_qss(mode))
            app.processEvents()
            assert picker.editor_card.grab().toImage().pixelColor(5, 20).name().upper() == background
            assert picker.draft.definitions[tag_id].color == "#123ABC"
    finally:
        theme.set_theme(previous_theme)
        app.setStyleSheet(previous_sheet)


def test_zero_usage_candidate_removal_deletes_definition_on_save(qtbot, tag_data):
    _, service, mod = tag_data
    host, picker = make_picker(qtbot, service.begin_edit(mod))
    picker.input.setText("New")
    qtbot.keyClick(picker.input, Qt.Key.Key_Return)
    tag_id = picker.draft.selected_ids[-1]
    picker._detach(tag_id)
    picker.open_removal(tag_id)
    assert "0" in picker.removal_dialog.usage_label.text()
    qtbot.mouseClick(picker.removal_dialog.confirm_button, Qt.MouseButton.LeftButton)
    service.commit_edit(picker.draft)
    assert tag_id not in {tag.id for tag in service.list_tags()}


def test_tooltip_displays_markup_like_tag_name_literally(qtbot, temp_data_dir):
    from src.ui.widgets.tag_chip import TagChip

    tag = TagDefinition("t", "g", "<b>literal</b>", "<b>literal</b>", "blue")
    chip = TagChip(tag)
    qtbot.addWidget(chip)
    chip.show()
    qtbot.waitUntil(chip.isActiveWindow)
    event = QHelpEvent(QEvent.Type.ToolTip, QPoint(10, 10), chip.mapToGlobal(QPoint(10, 10)))
    QApplication.sendEvent(chip, event)
    try:
        assert "&lt;b&gt;literal&lt;/b&gt;" in QToolTip.text()
        assert chip.toolTip() == "<b>literal</b>"
    finally:
        QToolTip.hideText()


def commit_ime(edit, value):
    QApplication.sendEvent(edit, QInputMethodEvent(value, []))
    committed = QInputMethodEvent()
    committed.setCommitString(value)
    QApplication.sendEvent(edit, committed)


def test_ime_new_query_edits_new_color_without_mutating_previous_shared_tag(qtbot, tag_data):
    _, service, mod = tag_data
    host, picker = make_picker(qtbot, service.begin_edit(mod))
    tag_id = picker.draft.selected_ids[0]
    previous_color = picker.draft.definitions[tag_id].color
    picker.open_editor(tag_id)
    picker.input.setFocus()
    commit_ime(picker.input, "高清")
    assert picker.draft.names == ["Shared"]
    picker.hex_input.setText("#123ABC")
    assert picker.draft.definitions[tag_id].color == previous_color
    qtbot.keyClick(picker.input, Qt.Key.Key_Return)
    assert picker.draft.names == ["Shared", "高清"]
    assert picker.draft.definitions[picker.draft.selected_ids[-1]].color == "#123ABC"


@pytest.mark.parametrize("value, valid", [("#123ABC", True), ("#123", False)])
def test_ime_hex_commit_validates_and_updates_the_visible_color(qtbot, tag_data, value, valid):
    _, service, mod = tag_data
    host, picker = make_picker(qtbot, service.begin_edit(mod))
    tag_id = picker.draft.selected_ids[0]
    previous_color = picker.draft.definitions[tag_id].color
    picker.open_editor(tag_id)
    picker.hex_input.setFocus()
    picker.hex_input.selectAll()
    commit_ime(picker.hex_input, value)
    assert picker.hex_input.text() == value
    assert picker.draft.definitions[tag_id].color == ("#123ABC" if valid else previous_color)
    assert picker.validate() is valid


def test_ime_committed_separator_waits_for_explicit_enter(qtbot, tag_data):
    _, service, mod = tag_data
    host, picker = make_picker(qtbot, service.begin_edit(mod))
    commit_ime(picker.input, "高清，")
    assert picker.input.text() == "高清，"
    assert picker.draft.names == ["Shared"]
    qtbot.keyClick(picker.input, Qt.Key.Key_Return)
    assert picker.draft.names == ["Shared", "高清"]
