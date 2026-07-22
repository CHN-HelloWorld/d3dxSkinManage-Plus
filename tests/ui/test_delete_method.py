from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget

from src.models.mod import Mod
from src.ui.dialogs.delete_progress import DeleteProgressDialog, delete_mod_with_fallback
from src.utils import fs


class _FailingRecycleManager:
    def __init__(self) -> None:
        self.mod = Mod(game_id="game", mod_id="long", name="Long Mod", path=r"D:\mods\Long")
        self.calls: list[str] = []

    def get_mod(self, _mod_id: str) -> Mod:
        return self.mod

    def delete_mod(self, _mod_id: str, *, delete_method: str) -> bool:
        self.calls.append(delete_method)
        if delete_method == "recycle_bin":
            raise fs.RecycleBinError(
                self.mod.path,
                path_too_long=True,
            )
        return True


def test_recycle_long_path_can_fall_back_to_permanent(qtbot, monkeypatch):
    parent = QWidget()
    qtbot.addWidget(parent)
    manager = _FailingRecycleManager()
    shown: dict[str, str] = {}

    def _question(_parent, title, message, *_args):
        shown["title"] = title
        shown["message"] = message
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", _question)

    assert delete_mod_with_fallback(parent, manager, manager.mod.mod_id, "recycle_bin")
    assert manager.calls == ["recycle_bin", "permanent"]
    assert shown["title"] == "无法放入回收站"
    assert "路径过长" in shown["message"]
    assert "是否改为直接永久删除" in shown["message"]


def test_recycle_long_path_can_be_kept(qtbot, monkeypatch):
    parent = QWidget()
    qtbot.addWidget(parent)
    manager = _FailingRecycleManager()
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: QMessageBox.StandardButton.No,
    )

    assert not delete_mod_with_fallback(
        parent,
        manager,
        manager.mod.mod_id,
        "recycle_bin",
    )
    assert manager.calls == ["recycle_bin"]


def test_batch_timer_pauses_during_recycle_fallback(qtbot, monkeypatch):
    manager = _FailingRecycleManager()
    dialog = DeleteProgressDialog(
        [manager.mod.mod_id],
        manager,
        delete_method="recycle_bin",
    )
    qtbot.addWidget(dialog)

    def _question(*_args):
        assert not dialog._timer.isActive()
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", _question)
    dialog.start_deletion()
    dialog._process_next()

    assert dialog._timer.isActive()
    assert dialog._current_index == 1
    dialog._timer.stop()
