from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QPushButton, QWidget

from src.core.mod_storage import ModStorage
from src.ui.dialogs.leftover_intake import (
    ACTION_DELETE,
    ACTION_KEEP,
    ACTION_RECYCLE,
    REASON_BROKEN,
    REASON_DUPLICATE,
    REASON_FAILED,
    REASON_STOPPED,
    LeftoverIntakeDialog,
    resolve_leftover_paths,
)
from src.ui.main_window import MainWindow
from src.utils import fs


@pytest.mark.parametrize("reason", [REASON_FAILED, REASON_DUPLICATE, REASON_BROKEN])
@pytest.mark.parametrize("dismiss", ["close", "escape"])
def test_dismissing_unusable_leftover_permanently_deletes_only_that_item(
    qtbot,
    tmp_path,
    monkeypatch,
    reason,
    dismiss,
):
    leftover = tmp_path / "leftover"
    leftover.mkdir()
    (leftover / "mod.ini").write_text("incomplete", encoding="utf-8")
    neighbour = tmp_path / "keep.txt"
    neighbour.write_text("keep", encoding="utf-8")

    window = QWidget()
    qtbot.addWidget(window)
    window._mod_manager = SimpleNamespace(_storage=ModStorage())

    def refuse_recycle(_path):
        raise AssertionError("Dismissal must not use the recycle bin")

    monkeypatch.setattr(fs, "move_to_trash", refuse_recycle)

    def dismiss_dialog():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, LeftoverIntakeDialog)
        if dismiss == "close":
            dialog.close()
        else:
            qtbot.keyClick(dialog, Qt.Key.Key_Escape)

    QTimer.singleShot(0, dismiss_dialog)
    result = MainWindow._resolve_intake_leftovers(
        window,
        [leftover],
        allow_keep=False,
        reason=reason,
    )

    assert result == (0, 1)
    assert not leftover.exists()
    assert neighbour.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("dismiss", ["close", "escape"])
def test_dismissing_stopped_import_keeps_the_leftover(qtbot, dismiss):
    dialog = LeftoverIntakeDialog(
        "unfinished.zip",
        remaining=1,
        total=1,
        allow_apply_all=False,
        reason=REASON_STOPPED,
    )
    qtbot.addWidget(dialog)
    QTimer.singleShot(0, dialog.close if dismiss == "close" else lambda: qtbot.keyClick(dialog, Qt.Key.Key_Escape))

    assert dialog.exec() == QDialog.DialogCode.Rejected
    assert dialog.action == ACTION_KEEP


@pytest.mark.parametrize("label, action", [("放入回收站", ACTION_RECYCLE), ("永久删除", ACTION_DELETE)])
def test_explicit_disposal_choice_applies_to_remaining_items(qtbot, tmp_path, label, action):
    paths = [tmp_path / "first.zip", tmp_path / "second.zip"]

    def choose_for_all():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, LeftoverIntakeDialog)
        dialog._apply_all_box.setChecked(True)
        button = next(button for button in dialog.findChildren(QPushButton) if button.text() == label)
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)

    QTimer.singleShot(0, choose_for_all)
    decisions = resolve_leftover_paths(paths, allow_keep=False, reason=REASON_DUPLICATE)

    assert decisions == {str(paths[0]): action, str(paths[1]): action}
