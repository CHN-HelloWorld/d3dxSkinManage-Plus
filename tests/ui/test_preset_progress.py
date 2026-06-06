from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy


def make_dialog(qtbot):
    from src.ui.dialogs.preset_progress import PresetProgressDialog

    dialog = PresetProgressDialog("拍照搭配")
    qtbot.addWidget(dialog)
    dialog.show()
    return dialog


def test_preparation_has_target_and_no_claimed_percentage(qtbot):
    dialog = make_dialog(qtbot)
    assert dialog.target_label.text() == "拍照搭配"
    assert dialog.maximum() == dialog.minimum() == 0
    assert not dialog.progress_label.text()
    dialog.set_item_progress(2, 4, "恢复", "Skin", 0.5)
    assert dialog.value() == 375
    assert "2 / 4" in dialog.progress_label.text()
    dialog.show_phase("finalize", "正在同步变体…")
    assert dialog.maximum() == dialog.minimum() == 0
    assert not dialog.progress_label.text()
    assert dialog.labelText() == "正在同步变体…"


def test_file_completion_cannot_report_final_completion(qtbot):
    dialog = make_dialog(qtbot)
    dialog.set_item_progress(1, 1, "恢复", "Skin", 1.0)
    assert dialog.value() == 999
    assert "100%" not in dialog.progress_label.text()
    assert dialog.isVisible()


def test_cancel_waits_for_owner_and_ignores_queued_progress(qtbot):
    dialog = make_dialog(qtbot)
    cancelled = QSignalSpy(dialog.canceled)
    qtbot.mouseClick(dialog.cancel_button, Qt.MouseButton.LeftButton)
    assert cancelled.count() == 1
    assert dialog.isVisible()
    assert not dialog.cancel_button.isEnabled()
    label = dialog.labelText()
    dialog.set_item_progress(2, 4, "恢复", "Skin", 0.5)
    dialog.setLabelText("迟到的任务进度")
    dialog.setRange(0, 1000)
    qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    assert cancelled.count() == 1
    assert dialog.labelText() == label
    assert dialog.maximum() == 0
    assert dialog.isVisible()


def test_commit_phase_cannot_be_cancelled_by_close_or_escape(qtbot):
    dialog = make_dialog(qtbot)
    cancelled = QSignalSpy(dialog.canceled)
    dialog.show_phase("finalize", "正在提交预设状态…")
    dialog.setCancelButton(None)
    dialog.close()
    qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    assert cancelled.count() == 0
    assert dialog.isVisible()
