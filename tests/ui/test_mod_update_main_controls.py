from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMainWindow, QMessageBox, QStackedWidget, QWidget

from src.core.config import Config
from src.core.mod_update_process import UpdateProcess
from src.ui.main_window import MainWindow
from tests.core.test_mod_update_controls import several_updates as several_updates
from tests.core.test_mod_update_controls import update_service as update_service
from tests.core.test_mod_update_transaction import _archive, _metadata
from tests.core.test_mod_update_transaction import update_case as update_case
from tests.qt_dialogs import interact


class UpdateHost(MainWindow):
    """保留真实更新和关闭处理，仅替换无关页面及监控器。"""

    def __init__(self, manager, service):
        QMainWindow.__init__(self)
        self._config = Config()
        self._mod_manager = manager
        self._game_service = manager._game_service
        self._mod_update_service = service
        self._mod_updates_dialog = None
        self._deploy_workers = {}
        self._pending_deploy_ids = deque()
        self._deploy_active = set()
        self._deploy_stopping = False
        self._deploy_close_pending = False
        self._deploy_game_id = ""
        self._deploy_environment_dirty = False
        self.messages = []
        self.updated_models = []
        self.cleaned = []
        self.toasts = []
        self.deployment_feedback = {}
        self._overview_page = SimpleNamespace(
            set_update_status_text=self.messages.append,
            set_updatable=lambda ids: None,
            update_mod_in_view=self.updated_models.append,
            _flush_pending_preview_reorder=lambda: self.cleaned.append("preview"),
            begin_deploy_overlay=lambda mid: self.deployment_feedback.update({mid: "pending"}),
            update_deploy_overlay=lambda mid, progress: None,
            finish_deploy_overlay=lambda mid, success=True, **kwargs: self.deployment_feedback.update(
                {mid: "done" if success else "failed"}
            ),
            update_mod_toggle_in_place=lambda *args: None,
            set_characters=lambda *args, **kwargs: None,
            _adult_mode="show",
        )
        self._intake_worker = None
        self._preset_apply_worker = None
        self._disable_worker = None
        self._export_controller = None
        self._stack = QStackedWidget(self)
        self._game_page = QWidget(self)
        self._gallery_page = SimpleNamespace()
        self._plugin_manager = None
        for name in (
            "_file_watcher",
            "_deploy_watcher",
            "_deploy_state_poll",
            "_source_state_poll",
            "_image_memory_monitor",
        ):
            setattr(self, name, SimpleNamespace(stop=lambda name=name: self.cleaned.append(name)))
        self._gb_store_page = SimpleNamespace(shutdown=lambda: self.cleaned.append("downloads"))
        self._gb_image_loader = SimpleNamespace(shutdown=lambda: self.cleaned.append("images"))
        assert callable(getattr(self, "_connect_mod_update_service", None)), "主窗口尚未统一连接真实更新事件"
        self._connect_mod_update_service()
        manager.add_event_listener(self._on_core_event)

    def _set_overview_update_count(self, count):
        self.update_count = count

    def _ensure_toast(self):
        return SimpleNamespace(done=lambda text, **kwargs: self.toasts.append(text))

    def _load_characters(self, game_id):
        pass

    def _refresh_gallery(self):
        pass


@pytest.fixture
def make_main(qtbot):
    hosts = []

    def create(manager, service):
        host = UpdateHost(manager, service)
        hosts.append(host)
        # pytest-qt 先关闭控件再运行清理器，须提前结束剩余任务，避免清理时打开无人响应的确认框。
        qtbot.addWidget(host, before_close_func=lambda widget: service.shutdown_updates())
        host.show()
        host._open_mod_updates_center()
        return host

    yield create
    for host in hosts:
        host._mod_update_service.shutdown_updates()
        listeners = host._mod_manager._event_listeners
        if host._on_core_event in listeners:
            listeners.remove(host._on_core_event)


def choose(box, *, stop):
    role = QMessageBox.ButtonRole.DestructiveRole if stop else QMessageBox.ButtonRole.RejectRole
    next(button for button in box.buttons() if box.buttonRole(button) == role).click()
    return 0


def test_real_worker_pause_reopen_resume_and_finish_while_dialog_is_closed(update_service, make_main, qtbot):
    manager, mod, service = update_service
    host = make_main(manager, service)
    dialog = host._mod_updates_dialog
    before = Path(mod.path).read_bytes()
    dialog._rows_by_id[mod.mod_id].update_button.click()
    process = service._update_tasks.findChild(UpdateProcess)
    assert process is not None and process.running
    dialog._rows_by_id[mod.mod_id].pause_button.click()
    state = service.update_task_snapshots()[mod.mod_id]
    assert state["phase"] == "paused" and process.paused
    assert dialog._rows_by_id[mod.mod_id].task_snapshot == state

    def background(box):
        next(button for button in box.buttons() if button.text() == "后台更新").click()
        return 0

    with interact(QMessageBox, background):
        dialog.reject()
    qtbot.wait(100)
    assert host._mod_updates_dialog is None and process.running and process.paused
    assert Path(mod.path).read_bytes() == before

    host._open_mod_updates_center()
    reopened = host._mod_updates_dialog
    assert reopened._rows_by_id[mod.mod_id].task_snapshot == state
    reopened._rows_by_id[mod.mod_id].pause_button.click()
    assert not process.paused
    with interact(QMessageBox, background):
        reopened.reject()
    qtbot.waitUntil(lambda: service.update_task_snapshots()[mod.mod_id]["phase"] in {"done", "failed"}, timeout=15000)
    state = service.update_task_snapshots()[mod.mod_id]
    assert state["phase"] == "done", state
    assert host._mod_updates_dialog is None, "后台完成不能自行重新打开弹窗"
    assert manager.get_mod(mod.mod_id).info.version == "v2"
    assert host.updated_models[-1].mod_id == mod.mod_id

    host._open_mod_updates_center()
    completed = host._mod_updates_dialog._rows_by_id[mod.mod_id]
    assert completed.task_snapshot == state
    assert completed.phase == "done" and not completed.update_button.isEnabled()
    assert not service.get_update_status(), "完成历史必须来自任务信息，而不是可更新列表"


def test_row_stop_confirmation_holds_real_process_and_preserves_prior_pause(update_service, make_main, qtbot):
    manager, mod, service = update_service
    host = make_main(manager, service)
    dialog = host._mod_updates_dialog
    original = Path(mod.path).read_bytes()
    row = dialog._rows_by_id[mod.mod_id]
    row.update_button.click()
    process = service._update_tasks.findChild(UpdateProcess)
    observed = []

    def back(box):
        observed.append(service.update_task_snapshots()[mod.mod_id]["phase"])
        assert process.paused and row.phase == "paused"
        return choose(box, stop=False)

    with interact(QMessageBox, back):
        row.stop_button.click()
    assert observed == ["paused"] and not process.paused
    row.pause_button.click()
    with interact(QMessageBox, back):
        row.stop_button.click()
    assert process.paused and row.phase == "paused"
    assert Path(mod.path).read_bytes() == original

    with interact(QMessageBox, lambda box: choose(box, stop=True)):
        row.stop_button.click()
    qtbot.waitUntil(lambda: service.update_task_snapshots()[mod.mod_id]["phase"] == "canceled", timeout=5000)
    assert row.task_snapshot == service.update_task_snapshots()[mod.mod_id]
    assert row.phase == "canceled" and row.update_button.isEnabled()
    assert Path(mod.path).read_bytes() == original and manager.get_mod(mod.mod_id).info.version == "v1"
    assert "终止" in host.messages[-1]


def test_batch_pause_resume_stop_controls_hidden_real_tasks(several_updates, make_main, qtbot):
    manager, mods, service = several_updates
    service._config.set("import.parallel_workers", 1)
    for index, mod in enumerate(mods):
        service._status[mod.mod_id].local_name = "visible" if index == 0 else f"hidden {index}"
    host = make_main(manager, service)
    dialog = host._mod_updates_dialog
    original = {mod.mod_id: Path(mod.path).read_bytes() for mod in mods}
    dialog._update_all_btn.click()
    assert len(service.active_update_ids()) == 4
    dialog._search.setText("visible")
    assert all(dialog._rows_by_id[mod.mod_id].isHidden() for mod in mods[1:])
    dialog._pause_all_btn.click()
    assert {state["phase"] for state in service.update_task_snapshots().values()} == {"paused"}
    dialog._resume_all_btn.click()
    assert all(state["phase"] != "paused" for state in service.update_task_snapshots().values())

    def stop(box):
        assert {state["phase"] for state in service.update_task_snapshots().values()} == {"paused"}
        return choose(box, stop=True)

    with interact(QMessageBox, stop):
        dialog._stop_all_btn.click()
    qtbot.waitUntil(lambda: not service.active_update_ids(), timeout=5000)
    assert {state["phase"] for state in service.update_task_snapshots().values()} == {"canceled"}
    assert all(Path(mod.path).read_bytes() == original[mod.mod_id] for mod in mods)


def test_game_switch_rebuilds_rows_and_rejects_late_foreign_events(update_service, make_main, temp_data_dir):
    manager, mod, service = update_service
    info = deepcopy(service._status[mod.mod_id])
    host = make_main(manager, service)
    first = host._mod_updates_dialog
    first._rows_by_id[mod.mod_id].update_button.click()
    first._rows_by_id[mod.mod_id].pause_button.click()
    old_snapshot = service.update_task_snapshots()[mod.mod_id]

    manager.set_game("game-b")
    current = host._mod_updates_dialog
    assert current is not None and current is not first
    assert not current._rows_by_id and not current._task_snapshots
    character = manager.add_character(name="Other", node_type="character")
    package = _archive(temp_data_dir / "other.zip", b"other game body")
    other = manager.import_mod(str(package), character.id, gb_meta=_metadata("v1", 10))
    assert other is not None
    info.local_mod_id = other.mod_id
    service._status[other.mod_id] = info
    service.status_changed.emit()
    assert set(current._rows_by_id) == {other.mod_id}
    before_messages = list(host.messages)
    service.update_task_changed.emit(mod.mod_id, old_snapshot)
    service.update_failed.emit(mod.mod_id, "late failure")
    service.update_cancelled.emit(mod.mod_id)
    service.update_deferred.emit(mod.mod_id)
    service.update_applied.emit(mod.mod_id, mod)
    assert set(current._rows_by_id) == {other.mod_id}
    assert current._rows_by_id[other.mod_id].phase == "ready"
    assert not current._task_snapshots and host.messages == before_messages

    manager.set_game("game-a")
    restored = host._mod_updates_dialog._rows_by_id[mod.mod_id]
    assert restored.task_snapshot == service.update_task_snapshots()[mod.mod_id]
    assert restored.phase == "paused"


def test_failure_handler_keeps_real_worker_stage_and_error_snapshot(update_service, make_main, qtbot):
    manager, mod, service = update_service
    service._download_manager.has_local_md5("").write_bytes(b"not an archive")
    host = make_main(manager, service)
    host._mod_updates_dialog._rows_by_id[mod.mod_id].update_button.click()
    qtbot.waitUntil(lambda: service.update_task_snapshots()[mod.mod_id]["phase"] == "failed", timeout=10000)
    state = service.update_task_snapshots()[mod.mod_id]
    row = host._mod_updates_dialog._rows_by_id[mod.mod_id]
    assert state["error"] and state["resume_phase"] == "verifying"
    assert row.task_snapshot == state
    assert row.task_detail.text() == "更新文件校验失败，请重新下载后重试。"
    assert state["error"] in row.task_detail.toolTip()
    assert manager.get_mod(mod.mod_id).info.version == "v1"


def test_new_available_file_supersedes_finished_history_on_reopen(update_service, make_main, qtbot):
    manager, mod, service = update_service
    host = make_main(manager, service)
    host._mod_updates_dialog._rows_by_id[mod.mod_id].update_button.click()
    qtbot.waitUntil(lambda: not service.active_update_ids(), timeout=15000)
    assert service.update_task_snapshots()[mod.mod_id]["phase"] == "done"
    newer = deepcopy(service.update_task_infos()[mod.mod_id])
    newer.latest_file = replace(newer.latest_file, file_id=12, version="v3", date_added_ts=3000)
    newer.candidate_files = [newer.latest_file]
    newer.current_version, newer.latest_version = "v2", "v3"
    service._status[mod.mod_id] = newer
    service.status_changed.emit()
    host._mod_updates_dialog.reject()
    host._open_mod_updates_center()
    row = host._mod_updates_dialog._rows_by_id[mod.mod_id]
    assert row.info.latest_file.file_id == 12 and row.phase == "ready"
    assert row.update_button.isEnabled(), "已完成的 v2 记录不能覆盖新检测到的 v3"


def test_update_waits_for_real_deployment_and_its_pending_commit(
    update_service, make_main, temp_data_dir, monkeypatch, qtbot
):
    manager, mod, service = update_service
    manager.set_deploy_dir(str(temp_data_dir / "Mods"))
    host = make_main(manager, service)
    prepared, release = Event(), Event()
    materialize = manager._storage.materialize_deployment

    def hold_completed_deployment(*args, **kwargs):
        result = materialize(*args, **kwargs)
        prepared.set()
        assert release.wait(5), "test did not release its completed deployment"
        return result

    monkeypatch.setattr(manager._storage, "materialize_deployment", hold_completed_deployment)
    host._on_overview_mod_toggle(mod.mod_id)
    worker = host._deploy_workers[mod.mod_id]
    row = host._mod_updates_dialog._rows_by_id[mod.mod_id]
    try:
        assert prepared.wait(3) and worker.isRunning()
        assert not manager.get_mod(mod.mod_id).is_enabled
        row.update_button.click()
        assert not service.active_update_ids(), "解压尚未收尾时不能用 disabled 快照开始更新"
        assert "部署" in host.toasts[-1]
        release.set()
        assert worker.wait(3000)
        assert not manager.get_mod(mod.mod_id).is_enabled
        row.update_button.click()
        assert not service.active_update_ids(), "线程结束但启用结果未落库时仍需互斥"
    finally:
        release.set()
        worker.wait(3000)
        qtbot.waitUntil(lambda: not host._deploy_workers, timeout=3000)

    assert manager.get_mod(mod.mod_id).is_enabled
    row.update_button.click()
    qtbot.waitUntil(lambda: not service.active_update_ids(), timeout=15000)
    state = service.update_task_snapshots()[mod.mod_id]
    assert state["phase"] == "done", state
    updated = manager.get_mod(mod.mod_id)
    assert updated.is_enabled and updated.info.version == "v2"
    assert (Path(updated.deployed_path) / "body.buf").read_bytes() == b"new body"


@pytest.mark.parametrize("entry", ["overview", "gallery", "background"])
def test_paused_update_blocks_deployment_from_every_entry(update_service, make_main, temp_data_dir, qtbot, entry):
    manager, mod, service = update_service
    deploy_root = temp_data_dir / "Mods"
    manager.set_deploy_dir(str(deploy_root))
    host = make_main(manager, service)
    row = host._mod_updates_dialog._rows_by_id[mod.mod_id]
    row.update_button.click()
    row.pause_button.click()
    paused = service.update_task_snapshots()[mod.mod_id]
    if entry == "overview":
        host._on_overview_mod_toggle(mod.mod_id)
    elif entry == "gallery":
        host._on_gallery_mod_toggle(mod.mod_id)
    else:
        host._start_async_deploy(mod.mod_id, [])
    assert not host._deploy_workers and not host._deploy_active
    assert not manager.get_mod(mod.mod_id).is_enabled
    assert not list(deploy_root.glob("*/body.buf"))
    assert service.update_task_snapshots()[mod.mod_id] == paused
    assert "更新" in host.toasts[-1]
    row.pause_button.click()
    qtbot.waitUntil(lambda: not service.active_update_ids(), timeout=15000)
    assert manager.get_mod(mod.mod_id).info.version == "v2"


def test_exit_confirmation_includes_other_games_and_cancellation_preserves_resources(update_service, make_main):
    manager, mod, service = update_service
    host = make_main(manager, service)
    host._mod_updates_dialog._rows_by_id[mod.mod_id].update_button.click()
    manager.set_game("game-b")
    assert service.active_update_ids() == [] and service.active_update_ids(all_games=True) == [mod.mod_id]
    job = service._update_tasks._records[("game-a", mod.mod_id)]
    seen = []

    def back(box):
        seen.append(box.windowTitle())
        assert job.snapshot()["phase"] == "paused" and job.process.paused
        return choose(box, stop=False)

    with interact(QMessageBox, back):
        assert not host.close()
    assert seen == ["终止更新并退出"] and host.isVisible()
    assert host.cleaned == [] and not job.process.paused

    process = job.process
    with interact(QMessageBox, lambda box: choose(box, stop=True)):
        assert host.close()
    assert not process.running
    assert job.process is None
    assert "downloads" in host.cleaned and "images" in host.cleaned
    assert manager.get_mod(mod.mod_id).info.version == "v1"


def test_aborted_close_leaves_the_update_service_usable(update_service, make_main, monkeypatch, qtbot):
    manager, mod, service = update_service
    host = make_main(manager, service)
    monkeypatch.setattr(host, "_stop_async_deploys", lambda: False)
    assert not host.close()
    assert host.isVisible() and host.cleaned == []
    row = host._mod_updates_dialog._rows_by_id[mod.mod_id]
    row.update_button.click()
    assert service.active_update_ids() == [mod.mod_id], "其他关闭守卫拒绝退出时不能永久关闭更新引擎"
    qtbot.waitUntil(lambda: not service.active_update_ids(), timeout=15000)
    assert manager.get_mod(mod.mod_id).info.version == "v2"


def test_close_waits_for_native_shutdown_before_releasing_gui_resources(update_service, make_main, monkeypatch, qtbot):
    import src.core.mod_update_process as process_module

    class SlowExitProcess(QObject):
        progress = Signal(dict)
        succeeded = Signal(dict)
        failed = Signal(str)
        canceled = Signal()
        running = True

        def __init__(self, plan, parent=None):
            super().__init__(parent)
            self.work_root = Path(plan["work_root"])

        def start(self):
            pass

        def pause(self):
            return True

        def resume(self):
            return True

        def cancel(self):
            pass

        def shutdown(self):
            pass

    monkeypatch.setattr(process_module, "UpdateProcess", SlowExitProcess)
    manager, mod, service = update_service
    host = make_main(manager, service)
    host._mod_updates_dialog._rows_by_id[mod.mod_id].update_button.click()
    process = service._update_tasks.findChild(SlowExitProcess)
    confirmations = []

    def stop(box):
        confirmations.append(box.windowTitle())
        return choose(box, stop=True)

    with interact(QMessageBox, stop):
        assert not host.close()
    assert process.running and process.work_root.is_dir()
    assert host.isVisible() and host.cleaned == []
    assert service.update_task_snapshots()[mod.mod_id]["phase"] == "stopping"
    assert "正在停止" in host.toasts[-1]
    process.running = False
    process.canceled.emit()
    assert host.close()
    qtbot.waitUntil(lambda: not process.work_root.exists(), timeout=5000)
    assert confirmations == ["终止更新并退出"]
    assert "downloads" in host.cleaned
