from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import QDialog, QMessageBox, QRadioButton

from src.core.mod_update_process import UpdateProcess
from tests.core.test_mod_update_controls import several_updates as several_updates
from tests.core.test_mod_update_controls import update_service as update_service
from tests.core.test_mod_update_transaction import update_case as update_case
from tests.qt_dialogs import interact
from tests.ui.test_mod_update_main_controls import make_main as make_main


def _variants(service, mod, base_id):
    info = service._status[mod.mod_id]
    first = replace(info.latest_file, file_id=base_id, file_name=f"Standard-{base_id}.zip")
    second = replace(first, file_id=base_id + 1, file_name=f"Alternative-{base_id}.zip")
    info.latest_file = first
    info.candidate_files = [first, second]
    return second


def test_bulk_cancel_does_not_start_single_or_multi_file_members(several_updates, make_main):
    manager, mods, service = several_updates
    _variants(service, mods[0], 100)
    _variants(service, mods[1], 200)
    host = make_main(manager, service)
    seen = []

    def cancel(picker):
        seen.append(picker)
        assert service.update_task_snapshots() == {}
        picker.reject()
        return QDialog.DialogCode.Rejected

    with interact(QDialog, cancel):
        host._on_update_all()
    assert len(seen) == 1, "所有多文件 Mod 必须集中询问一次"
    assert service.update_task_snapshots() == {}, "取消选择时单文件成员也不能抢先启动"


def test_bulk_confirms_each_variant_before_start_and_includes_filtered_members(several_updates, make_main):
    manager, mods, service = several_updates
    first = _variants(service, mods[0], 100)
    second = _variants(service, mods[1], 200)
    service._status[mods[0].mod_id].local_name = "visible"
    host = make_main(manager, service)
    host._mod_updates_dialog._search.setText("visible")
    seen = []

    def confirm(picker):
        seen.append(picker)
        assert service.update_task_snapshots() == {}
        for file in (first, second):
            next(radio for radio in picker.findChildren(QRadioButton) if file.file_name in radio.text()).click()
        picker.accept()
        return QDialog.DialogCode.Accepted

    with interact(QDialog, confirm):
        host._on_update_all()
    assert len(seen) == 1
    snapshots = service.update_task_snapshots()
    assert set(snapshots) == {mod.mod_id for mod in mods}
    assert snapshots[mods[0].mod_id]["file_name"] == first.file_name
    assert snapshots[mods[1].mod_id]["file_name"] == second.file_name


def test_close_freezes_real_worker_then_background_retains_manual_pause(update_service, make_main, qtbot):
    manager, mod, service = update_service
    host = make_main(manager, service)
    dialog = host._mod_updates_dialog
    original = Path(mod.path).read_bytes()
    dialog._rows_by_id[mod.mod_id].update_button.click()
    process = service._update_tasks.findChild(UpdateProcess)
    service.pause_update(mod.mod_id)
    seen = []

    def background(box):
        seen.append(service.update_task_snapshots()[mod.mod_id]["phase"])
        assert process.paused
        assert Path(mod.path).read_bytes() == original
        next(button for button in box.buttons() if button.text() == "后台更新").click()
        return 0

    with interact(QMessageBox, background):
        dialog.close()
    assert seen == ["paused"]
    assert host._mod_updates_dialog is None
    assert process.paused and service.update_task_snapshots()[mod.mod_id]["phase"] == "paused"
    panel = host._mod_update_progress
    assert panel.isVisible() and panel._paused
    assert panel in host._panel_stack._order
    panel.resume_requested.emit()
    assert not process.paused
    qtbot.waitUntil(lambda: service.update_task_snapshots()[mod.mod_id]["phase"] == "done", timeout=15000)
    assert panel.isVisible() and manager.get_mod(mod.mod_id).info.version == "v2"
    panel.dismiss_requested.emit()
    assert service.update_task_snapshots() == {}
    assert service.check_snapshot()["phase"] == "idle"
    host._open_mod_updates_center()
    assert not host._mod_updates_dialog._rows_by_id


def test_return_from_close_keeps_dialog_and_resumes_only_automatic_hold(update_service, make_main):
    manager, mod, service = update_service
    host = make_main(manager, service)
    dialog = host._mod_updates_dialog
    dialog._rows_by_id[mod.mod_id].update_button.click()
    process = service._update_tasks.findChild(UpdateProcess)
    seen = []

    def back(box):
        assert process.paused
        seen.append(True)
        box.reject()
        return 0

    with interact(QMessageBox, back):
        dialog.reject()
    assert seen == [True]
    assert dialog.isVisible() and host._mod_updates_dialog is dialog
    assert not process.paused


def test_completed_dialog_close_clears_history_and_reopens_idle(update_service, make_main, qtbot):
    manager, mod, service = update_service
    host = make_main(manager, service)
    dialog = host._mod_updates_dialog
    dialog._rows_by_id[mod.mod_id].update_button.click()
    qtbot.waitUntil(lambda: service.update_task_snapshots()[mod.mod_id]["phase"] == "done", timeout=15000)
    dialog._update_all_btn.click()
    assert host._mod_updates_dialog is None
    assert not service.update_task_snapshots()
    assert service.check_snapshot()["phase"] == "idle"
    host._open_mod_updates_center()
    assert not host._mod_updates_dialog._rows_by_id


def test_batch_rejects_choices_if_a_newer_file_arrives_while_picker_is_open(several_updates, make_main):
    manager, mods, service = several_updates
    _variants(service, mods[0], 100)
    host = make_main(manager, service)

    def refreshed_while_choosing(picker):
        info = service._status[mods[0].mod_id]
        info.latest_file = replace(info.latest_file, file_id=500, date_added_ts=4000)
        info.candidate_files = [info.latest_file]
        picker.accept()
        return QDialog.DialogCode.Accepted

    with interact(QDialog, refreshed_while_choosing):
        host._on_update_all()
    assert not service.update_task_snapshots()
    assert host.toasts, "发生版本变化时应让用户知道需要重新确认"


def test_close_stop_holds_every_task_before_confirmation_and_keeps_uncommitted_mods(several_updates, make_main, qtbot):
    manager, mods, service = several_updates
    service._config.set("import.parallel_workers", 1)
    host = make_main(manager, service)
    original = {mod.mod_id: Path(mod.path).read_bytes() for mod in mods}
    host._on_update_all()
    seen = []

    def stop(box):
        seen.append({s["phase"] for s in service.update_task_snapshots().values()})
        process = service._update_tasks.findChild(UpdateProcess)
        assert process is not None and process.paused
        stop_button = next(
            button for button in box.buttons() if box.buttonRole(button) == QMessageBox.ButtonRole.DestructiveRole
        )
        stop_button.click()
        return 0

    with interact(QMessageBox, stop):
        host._mod_updates_dialog.close()
    assert seen == [{"paused"}]
    assert host._mod_updates_dialog is None
    qtbot.waitUntil(lambda: not service.active_update_ids(), timeout=5000)
    assert {s["phase"] for s in service.update_task_snapshots().values()} == {"canceled"}
    assert all(Path(mod.path).read_bytes() == original[mod.mod_id] for mod in mods)
    assert not host._mod_update_background_games


def test_game_switch_during_real_close_confirmation_releases_hold_without_deleted_widget_access(
    update_service, make_main
):
    manager, mod, service = update_service
    host = make_main(manager, service)
    old_dialog = host._mod_updates_dialog
    old_dialog._rows_by_id[mod.mod_id].update_button.click()
    service.pause_update(mod.mod_id)
    process = service._update_tasks.findChild(UpdateProcess)
    with interact(QMessageBox, lambda box: manager.set_game("game-b")):
        old_dialog.reject()
    assert host._mod_updates_dialog is not old_dialog
    assert host._mod_updates_dialog.property("gameId") == "game-b"
    assert not host._mod_update_background_games
    assert process.paused
    manager.set_game("game-a")
    assert service.update_task_snapshots()[mod.mod_id]["phase"] == "paused"


def test_completed_background_mod_does_not_reappear_when_its_success_receipt_has_elapsed(
    update_service, make_main, qtbot
):
    manager, mod, service = update_service
    host = make_main(manager, service)

    def background(box):
        next(button for button in box.buttons() if button.text() == "后台更新").click()
        return 0

    host._mod_updates_dialog._rows_by_id[mod.mod_id].update_button.click()
    with interact(QMessageBox, background):
        host._mod_updates_dialog.reject()
    qtbot.waitUntil(lambda: service.update_task_snapshots()[mod.mod_id]["phase"] == "done", timeout=15000)
    qtbot.wait(2100)
    host._open_mod_updates_center()
    assert not host._mod_updates_dialog._rows_by_id
    assert host._mod_updates_dialog.is_batch_complete()
    assert service.update_task_snapshots()[mod.mod_id]["phase"] == "done", "查看结果前保留完成历史"
    host._mod_updates_dialog._update_all_btn.click()
    assert service.update_task_snapshots() == {}
