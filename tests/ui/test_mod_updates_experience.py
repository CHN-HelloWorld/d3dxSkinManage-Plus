from __future__ import annotations

from time import monotonic
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QSize, Signal
from PySide6.QtWidgets import QWidget

from src.core.config import Config
from src.ui.dialogs.mod_updates_dialog import ModUpdatesDialog
from src.ui.main_window import MainWindow
from tests.ui.test_mod_updates_dialog import _info, keep_dialog


def test_window_size_survives_closing_and_reloading_config(qtbot, temp_data_dir):
    config = Config()
    dialog = ModUpdatesDialog([], config=config)
    keep_dialog(qtbot, dialog)
    dialog.show()
    dialog.resize(730, 470)
    qtbot.waitUntil(lambda: dialog.size() == QSize(730, 470))
    dialog.reject()
    Config._instance = None
    restored = ModUpdatesDialog([], config=Config())
    keep_dialog(qtbot, restored)
    restored.show()
    assert restored.size() == QSize(730, 470)


def test_dragging_does_not_write_settings_for_every_resize(qtbot, temp_data_dir):
    config = Config()
    config.set("ui.mod_updates", {"width": 730, "height": 470})
    settings = temp_data_dir / "config" / "settings.json"
    before = settings.read_bytes()
    dialog = ModUpdatesDialog([], config=config)
    keep_dialog(qtbot, dialog)
    dialog.show()
    for width in (750, 700, 650, 610):
        dialog.resize(width, 440)
    assert settings.read_bytes() == before
    dialog.close()
    Config._instance = None
    assert Config().get("ui.mod_updates") == {"width": 610, "height": 440}


@pytest.mark.parametrize("width,height", [(1, 1), (20000, 20000), ("invalid", None)])
def test_saved_sizes_fit_the_current_screen(qtbot, temp_data_dir, width, height):
    config = Config()
    config.set("ui.mod_updates", {"width": width, "height": height})
    Config._instance = None
    dialog = ModUpdatesDialog([], config=Config())
    keep_dialog(qtbot, dialog)
    dialog.show()
    available = dialog.screen().availableGeometry()
    assert dialog.width() >= 420 and dialog.height() >= 340
    assert dialog.width() <= available.width() and dialog.height() <= available.height()


class ControlledService(QObject):
    check_started = Signal(int)
    progress = Signal(int, int)
    check_finished = Signal(dict)
    status_changed = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.checking = False
        self.requests = 0
        self.visible_at_request = False
        self.infos = {}
        self.errors = {}
        self.previous = {}
        self.state = dict(
            checking=False,
            phase="idle",
            total=0,
            done=0,
            found=0,
            failed=0,
            started_at=0.0,
            last_progress_at=0.0,
            finished_at=0.0,
            retry_only=False,
        )

    def get_update_status(self, *, completed_only=False):
        if completed_only and self.checking:
            return dict(self.previous)
        return dict(self.infos)

    def failures(self):
        return dict(self.errors)

    def check_snapshot(self):
        return dict(self.state)

    def updatable_ids(self):
        return set(self.infos)

    def update_task_infos(self):
        return {}

    def update_task_snapshots(self):
        return {}

    def active_update_ids(self, *, all_games=False):
        return []

    def pause_update(self, mid):
        return False

    def resume_update(self, mid):
        return False

    def pause_all_updates(self):
        return False

    def resume_all_updates(self):
        pass

    def is_checking(self):
        return self.checking

    def check_updates(self, *, force, only_failed=False):
        self.requests += 1
        self.previous = dict(self.infos)
        dialog = self.parent()._mod_updates_dialog
        self.visible_at_request = dialog is not None and dialog.isVisible()
        self.checking = True
        now = monotonic()
        self.state = dict(
            checking=True,
            phase="connecting",
            started_at=now,
            last_progress_at=now,
            finished_at=0.0,
            total=8,
            done=0,
            found=0,
            failed=0,
            retry_only=only_failed,
        )
        self.check_started.emit(8)
        return True

    def advance(self, done, total, found=0):
        self.state.update(phase="checking", done=done, total=total, found=found, last_progress_at=monotonic())
        self.progress.emit(done, total)

    def cancel_check(self):
        self.checking = False
        self.infos = dict(self.previous)
        self.state.update(checking=False, phase="cancelled", finished_at=monotonic())
        self.check_finished.emit({"found": 0, "failed": 0, "cancelled": True})
        return True

    def finish(self, *, found=0, failed=0):
        self.checking = False
        if found:
            self.infos = {"m1": _info("m1")}
        self.errors = {f"error{i}": "连接失败" for i in range(failed)}
        self.state.update(
            checking=False,
            phase="partial" if failed else "done",
            found=found,
            failed=failed,
            done=8,
            finished_at=monotonic(),
        )
        self.check_finished.emit({"found": found, "up_to_date": 8 - found - failed, "failed": failed})


class CheckHost(QWidget):
    _on_check_mod_updates = MainWindow._on_check_mod_updates
    _on_update_check_started = MainWindow._on_update_check_started
    _on_update_check_finished = MainWindow._on_update_check_finished
    _on_update_check_progress = MainWindow._on_update_check_progress
    _on_update_status_changed = MainWindow._on_update_status_changed
    _open_mod_updates_center = MainWindow._open_mod_updates_center
    _on_stop_mod_updates = MainWindow._on_stop_mod_updates
    _request_mod_updates_close = MainWindow._request_mod_updates_close
    _sync_mod_update_notification = MainWindow._sync_mod_update_notification

    def __init__(self, config):
        super().__init__()
        self._config = config
        self._mod_manager = SimpleNamespace(current_game_id="game-a")
        self._mod_updates_dialog = None
        self.messages = []
        self._overview_page = SimpleNamespace(
            set_update_status_text=self.messages.append,
            set_updatable=lambda ids: None,
        )
        self._mod_update_service = ControlledService(self)
        self._mod_update_service.check_started.connect(self._on_update_check_started)
        self._mod_update_service.check_finished.connect(self._on_update_check_finished)
        self._mod_update_service.progress.connect(self._on_update_check_progress)
        self._mod_update_service.status_changed.connect(self._on_update_status_changed)

    def _open_gb_for_local_mod(self, mid):
        pass

    def _on_apply_update(self, mid):
        pass

    def _on_update_all(self):
        pass

    def _set_overview_update_count(self, count):
        pass


def make_host(qtbot, config):
    host = CheckHost(config)
    keep_dialog(qtbot, host)
    return host


def test_manual_check_opens_a_checking_window_before_starting_network(qtbot, temp_data_dir):
    host = make_host(qtbot, Config())
    host._on_check_mod_updates(force=True)
    service = host._mod_update_service
    assert service.visible_at_request, "手动检查必须立即打开弹窗，不能等网络结束"
    dialog = host._mod_updates_dialog
    assert dialog._checking
    assert "暂无" not in dialog._empty_title.text()
    assert not dialog._recheck_btn.isEnabled()
    service.finish()
    assert not dialog._checking
    assert dialog.isVisible()
    assert dialog._recheck_btn.isEnabled()


def test_manual_click_joins_background_check_without_another_request(qtbot, temp_data_dir):
    host = make_host(qtbot, Config())
    host._on_check_mod_updates(force=False)
    assert host._mod_updates_dialog is None
    host._on_check_mod_updates(force=True)
    assert host._mod_updates_dialog is not None
    assert host._mod_updates_dialog._checking
    assert host._mod_update_service.requests == 1
    host._mod_update_service.finish()


def test_closing_during_check_does_not_reopen_the_window_on_completion(qtbot, temp_data_dir):
    host = make_host(qtbot, Config())
    host._on_check_mod_updates(force=True)
    dialog = host._mod_updates_dialog
    assert dialog is not None
    dialog.reject()
    assert host._mod_updates_dialog is None
    host._mod_update_service.finish(found=1)
    assert host._mod_updates_dialog is None


def test_reopening_ongoing_check_restores_its_latest_progress(qtbot, temp_data_dir):
    host = make_host(qtbot, Config())
    host._on_check_mod_updates(force=True)
    service = host._mod_update_service
    service.advance(5, 8)
    assert "5 / 8" in host._mod_updates_dialog._check_panel.title.text()
    started = host._mod_updates_dialog._check_snapshot["started_at"]
    host._mod_updates_dialog.reject()
    host._on_check_mod_updates(force=True)
    assert "5 / 8" in host._mod_updates_dialog._check_panel.title.text()
    assert host._mod_updates_dialog._check_snapshot["started_at"] == started
    assert service.requests == 1
    service.finish()


def test_reopening_update_center_reuses_the_cached_window(qtbot, temp_data_dir):
    host = make_host(qtbot, Config())
    dialogs = []

    for _ in range(4):
        host._open_mod_updates_center()
        dialog = host._mod_updates_dialog
        dialogs.append(dialog)
        dialog.reject()

    assert host._mod_updates_dialog is None
    assert len({id(dialog) for dialog in dialogs}) == 1
    assert host.findChildren(ModUpdatesDialog) == [dialogs[0]]


def test_check_progress_remains_visible_when_partial_results_arrive(qtbot):
    dialog = ModUpdatesDialog([], enable_actions=True)
    keep_dialog(qtbot, dialog)
    dialog.set_checking(True)
    dialog.set_check_progress(3, 8)
    dialog.set_updates([_info("m1")])
    dialog.set_checking(True)
    assert "3 / 8" in dialog._check_panel.title.text()
    assert not dialog._update_all_btn.isEnabled()
    dialog.set_checking(False)
    assert "3 / 8" not in dialog._subtitle.text()
    assert dialog._update_all_btn.isEnabled()


def test_real_handlers_keep_previous_rows_even_after_closing_and_reopening(qtbot, temp_data_dir):
    host = make_host(qtbot, Config())
    service = host._mod_update_service
    service.infos = {"old": _info("old")}
    host._on_check_mod_updates(force=True)
    service.infos = {"new": _info("new")}
    service.advance(3, 8, found=1)
    service.status_changed.emit()
    assert host._mod_updates_dialog.pending_ids() == ["old"]
    host._mod_updates_dialog.reject()
    host._open_mod_updates_center()
    assert host._mod_updates_dialog.pending_ids() == ["old"]
    assert "1 个可用更新" in host._mod_updates_dialog._check_panel.detail.text()
    service.finish(found=1)
    assert host._mod_updates_dialog.pending_ids() == ["m1"]


def test_cancel_button_routes_to_service_and_does_not_report_latest(qtbot, temp_data_dir):
    host = make_host(qtbot, Config())
    host._on_check_mod_updates(force=True)
    dialog = host._mod_updates_dialog
    dialog._check_panel.action.click()
    assert not host._mod_update_service.is_checking()
    assert "取消" in dialog._check_panel.title.text()
    assert "取消" in host.messages[-1]
    assert dialog._recheck_btn.isEnabled()


def test_failed_retry_button_starts_a_retry_round_with_a_fresh_timer(qtbot, temp_data_dir):
    host = make_host(qtbot, Config())
    host._on_check_mod_updates(force=True)
    service = host._mod_update_service
    service.finish(failed=2)
    dialog = host._mod_updates_dialog
    finished = dialog._check_snapshot["finished_at"]
    dialog._check_panel.action.click()
    assert service.is_checking()
    assert service.check_snapshot()["retry_only"]
    assert dialog._check_snapshot["started_at"] >= finished
    assert "仅重试失败项" in dialog._check_panel.title.text()
    assert not dialog._recheck_btn.isEnabled()
    service.finish()
