from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from src.core.config import Config
from src.core.operation_preflight import OperationPreflightError
from src.ui.dialogs.operation_failure import OperationFailureDialog
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm
from tests.ui.test_deploy_responsiveness import _DeployWindow


@pytest.mark.parametrize(
    "kind, method, action",
    [
        ("mod", "permanent", "永久删除 Mod"),
        ("mod", "recycle_bin", "移入回收站"),
        ("category", "recycle_bin", "永久删除类别"),
    ],
)
def test_failure_action_uses_the_displayed_object_and_deletion_method(
    qtbot,
    mm,
    tmp_path,
    monkeypatch,
    kind,
    method,
    action,
):
    from src.ui.main_window import MainWindow
    from src.utils import fs

    node = mm.add_character("分类 A", node_type="character")
    mod = _import_mod(mm, node.id, tmp_path, "外观 A")
    body = Path(mod.path)
    recycle = tmp_path / "recycle"
    recycle.mkdir()
    monkeypatch.setattr(fs, "move_to_trash", lambda path: Path(path).rename(recycle / Path(path).name))
    config = Config()
    config.set("mods.delete_method", method)
    window = QWidget()
    qtbot.addWidget(window)
    window._mod_manager, window._config = mm, config
    refreshed = []
    window._refresh_intake_views = lambda: refreshed.append(True)
    window._toast_done = lambda message: pytest.fail(message)
    error = OperationPreflightError(mod.mod_id, body, "无法完成上次操作")
    if kind == "category":
        error.node_id = node.id

    def choose():
        dialog = QApplication.activeModalWidget()
        try:
            name = node.display_name if kind == "category" else mod.display_name
            assert any(name in label.text() for label in dialog.findChildren(QLabel))
            button = next(b for b in dialog.findChildren(QPushButton) if b.text() == action)
            # 提示期间配置变化也须按已向用户展示的动作执行。
            config.set("mods.delete_method", "recycle_bin" if method == "permanent" else "permanent")
            qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
        finally:
            if dialog.isVisible():
                dialog.reject()

    QTimer.singleShot(0, choose)
    MainWindow._show_operation_failure(window, {"game_id": mm.current_game_id, "error": error})

    assert mm.get_mod(mod.mod_id) is None
    assert not body.exists()
    assert bool(list(recycle.iterdir())) is (kind == "mod" and method == "recycle_bin")
    assert (mm.get_character(node.id) is None) is (kind == "category")
    assert refreshed == [True]


@pytest.mark.parametrize("dismiss", ["close", "escape", "button", "enter"])
def test_failure_dialog_can_close_without_deleting(qtbot, dismiss):
    dialog = OperationFailureDialog(OperationPreflightError("id", "path", "文件不存在"))
    qtbot.addWidget(dialog)
    dialog.show()
    if dismiss == "close":
        dialog.close()
    elif dismiss == "escape":
        qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    elif dismiss == "enter":
        qtbot.keyClick(dialog, Qt.Key.Key_Return)
    else:
        button = next(b for b in dialog.findChildren(QPushButton) if b.text() == "关闭")
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert not dialog.isVisible()
    assert dialog.result() == 0


def test_delete_requires_button_click(qtbot):
    dialog = OperationFailureDialog(OperationPreflightError("id", "path", "文件不存在"))
    qtbot.addWidget(dialog)
    button = next(b for b in dialog.findChildren(QPushButton) if b.text() == "永久删除 Mod")
    QTimer.singleShot(0, lambda: qtbot.mouseClick(button, Qt.MouseButton.LeftButton))
    assert dialog.exec() == 1


def test_file_check_runs_off_owner_thread_and_keeps_events_responsive(qtbot):
    from threading import Event

    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QWidget

    from src.ui.operation_check import run_file_check

    parent = QWidget()
    qtbot.addWidget(parent)
    release = Event()
    heartbeat = []
    owner = QThread.currentThread()

    def check():
        assert QThread.currentThread() != owner
        assert release.wait(3)
        return "confirmed"

    def respond():
        heartbeat.append(True)
        release.set()

    QTimer.singleShot(10, respond)
    assert run_file_check(check, parent) == "confirmed"
    assert heartbeat and parent.isEnabled()


@pytest.mark.parametrize("failure_stage", ["missing", "extract", "switch"])
def test_failed_enable_keeps_previous_mod_enabled(qtbot, mm, tmp_path, monkeypatch, failure_stage):
    from src.ui.main_window import QMessageBox

    node = mm.add_character("Character", node_type="character")
    old, new = [_import_mod(mm, node.id, tmp_path, name) for name in ("old", "new")]
    mm.set_deploy_dir(str(tmp_path / "Mods"))
    assert mm.toggle_mod(old.mod_id).is_enabled
    window = _DeployWindow([old, new], mm._storage)
    window._mod_manager = mm
    window._game_service = mm._game_service
    window._config.set(window._CONFLICT_SUPPRESS_KEY, True)
    qtbot.addWidget(window)
    errors = []
    mm.add_event_listener(lambda event, **data: errors.append(data) if event == "operation_failed" else None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: None)
    if failure_stage == "missing":
        Path(new.path).unlink()
    elif failure_stage == "extract":

        def broken(*args, **kwargs):
            raise OSError("extract failed")

        monkeypatch.setattr(mm._storage, "materialize_deployment", broken)
    else:
        original = mm.disable_batch_step

        def changed(entry):
            result = original(entry)
            Path(new.path).write_bytes(b"replaced while switching")
            return result

        monkeypatch.setattr(mm, "disable_batch_step", changed)
    try:
        window._on_overview_mod_toggle(new.mod_id)
        qtbot.waitUntil(lambda: not window._deploy_workers, timeout=5000)
        current = mm.get_mod(old.mod_id)
        assert current.is_enabled and Path(current.deployed_path).is_dir()
        assert not mm.get_mod(new.mod_id).is_enabled
        assert not (tmp_path / "Mods" / new.folder_name).exists()
        if failure_stage != "extract":
            assert errors
    finally:
        assert window._stop_async_deploys()
