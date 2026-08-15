from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, QRect
from PySide6.QtWidgets import QDialog

from src.models.mod import Mod, ModInfo
from src.ui import theme
from src.ui.dialogs.mod_edit import ModEditDialog


@pytest.fixture
def resize_editor(qtbot, qapp, temp_data_dir, monkeypatch):
    previous_style = qapp.styleSheet()
    previous_theme = theme.current_theme()
    theme.set_theme("light")
    qapp.setStyleSheet(theme.render_qss("light"))
    dialog = ModEditDialog(
        Mod(
            name="Resize while editing",
            path="D:\\Mods\\" + "LongFolderName\\" * 35,
            info=ModInfo(tags=[f"LongTagForResizing{index:02}" for index in range(20)]),
        )
    )
    qtbot.addWidget(dialog)
    monkeypatch.setattr(dialog, "screen", lambda: SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1440, 900)))
    dialog.show()
    qtbot.wait(80)
    yield dialog
    dialog.done(QDialog.DialogCode.Rejected)
    theme.set_theme(previous_theme)
    qapp.setStyleSheet(previous_style)


@pytest.mark.parametrize("start_width,end_width", [(820, 719), (719, 820)])
def test_breakpoint_resize_preserves_pending_tag_focus_and_selection(resize_editor, qtbot, start_width, end_width):
    dialog = resize_editor
    dialog.resize(start_width, 560)
    qtbot.wait(60)
    picker = dialog.tag_picker
    picker.input.setText("Pending tag")
    picker.input.setFocus()
    picker.input.setSelection(8, 3)
    qtbot.wait(60)
    assert picker.input.hasFocus()

    dialog.resize(end_width, 560)
    qtbot.wait(60)

    assert picker.input.hasFocus()
    assert picker.input.selectedText() == "tag"
    assert picker.input.text() == "Pending tag"
    assert dialog.height() == 560


def test_narrow_file_details_expand_and_remain_reachable_after_resizing(resize_editor, qtbot):
    dialog = resize_editor
    dialog.resize(600, 500)
    qtbot.wait(60)
    folded_height = dialog._form_content.height()
    dialog._info_toggle.setChecked(True)
    qtbot.wait(60)

    assert dialog.height() == 500
    assert dialog._form_content.height() > folded_height
    dialog._form_scroll.ensureWidgetVisible(dialog._copy_id_button, 0, 0)
    qtbot.wait(30)
    viewport = dialog._form_scroll.viewport()
    position = dialog._copy_id_button.mapTo(viewport, QPoint())
    assert viewport.rect().contains(QRect(position, dialog._copy_id_button.size()))

    dialog._info_toggle.setChecked(False)
    qtbot.wait(60)
    assert dialog._form_content.height() == folded_height


def test_breakpoint_drag_does_not_reapply_tag_palette(resize_editor, qapp):
    palette_changes = []

    class PaletteEvents(QObject):
        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.PaletteChange:
                palette_changes.append(event.type())
            return False

    picker = resize_editor.tag_picker
    recorder = PaletteEvents(picker)
    picker.installEventFilter(recorder)
    for width in (719, 720, 719, 720, 600, 820):
        resize_editor.resize(width, 560)
        qapp.processEvents()

    assert not palette_changes
