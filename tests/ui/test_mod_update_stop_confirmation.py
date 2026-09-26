from pathlib import Path

from PySide6.QtWidgets import QMessageBox, QWidget

from tests.core.test_mod_update_controls import update_service as update_service
from tests.core.test_mod_update_transaction import update_case as update_case
from tests.qt_dialogs import interact


def test_confirmation_freezes_before_show_and_confirm_keeps_old_mod(update_service, qtbot):
    from src.ui.mod_update_actions import confirm_stop_updates

    manager, mod, service = update_service
    parent = QWidget()
    qtbot.addWidget(parent)
    original = Path(mod.path).read_bytes()
    service.apply_update(mod.mod_id)
    seen = []

    def choose_stop(box):
        snapshot = service.update_task_snapshots()[mod.mod_id]
        seen.append(snapshot["phase"])
        assert snapshot["phase"] == "paused"
        assert manager.get_mod(mod.mod_id).info.version == "v1"
        assert box.defaultButton().text() == "返回更新"
        stop = next(
            button for button in box.buttons() if box.buttonRole(button) == QMessageBox.ButtonRole.DestructiveRole
        )
        stop.click()
        return 0

    with interact(QMessageBox, choose_stop):
        assert confirm_stop_updates(parent, service, [mod.mod_id])
    assert seen == ["paused"]
    qtbot.waitUntil(lambda: service.update_task_snapshots()[mod.mod_id]["phase"] == "canceled", timeout=5000)
    assert Path(mod.path).read_bytes() == original
    assert manager.get_mod(mod.mod_id).info.version == "v1"


def test_closing_confirmation_restores_prior_pause_state(update_service, qtbot):
    from src.ui.mod_update_actions import confirm_stop_updates

    _, mod, service = update_service
    parent = QWidget()
    qtbot.addWidget(parent)
    service.apply_update(mod.mod_id)
    service.pause_update(mod.mod_id)

    def close_box(box):
        assert service.update_task_snapshots()[mod.mod_id]["phase"] == "paused"
        box.reject()
        return 0

    with interact(QMessageBox, close_box):
        assert not confirm_stop_updates(parent, service, [mod.mod_id])
    assert service.update_task_snapshots()[mod.mod_id]["phase"] == "paused"
