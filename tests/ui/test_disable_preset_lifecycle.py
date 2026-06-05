from __future__ import annotations

import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QMainWindow, QProgressDialog, QWidget

from src.ui.main_window import MainWindow
from tests.core.test_variant_preset_switch import env as env
from tests.helpers import apply_preset
from tests.helpers import registered_games as registered_games


class _Window(MainWindow):
    def __init__(self):
        QMainWindow.__init__(self)
        self._preset_apply_worker = None
        self._preset_apply_progress = None
        self._preset_apply_preparing = False
        self._deploy_workers = {}
        self._deploy_environment_dirty = False
        self._plugin_variant_pending = False
        self._watcher_changed_paths = set()
        self._pending_gb_installs = []
        self._disable_worker = None
        self._game_service = SimpleNamespace(current_game_id="g")
        self._overview_page = SimpleNamespace(_preset_selector=QWidget(self))
        self._game_page = QWidget(self)
        self._preset_apply_game_id = ""
        self._disable_game_id = ""
        self._disable_entries = ()
        self._disable_done = 0
        self._mod_update_service = SimpleNamespace(active_update_ids=lambda **kw: [])
        self.notices = []

    def _toast_done(self, text):
        self.notices.append(text)

    def closeEvent(self, event):
        QMainWindow.closeEvent(self, event)


class _Panel(QObject):
    stop_requested = Signal()

    def __init__(self):
        super().__init__()
        self.active = False
        self.outcome = None

    def start_batch(self, *args):
        self.active = True

    def update_progress(self, *args):
        pass

    def update_item(self, *args):
        pass

    def set_stop_enabled(self, *args):
        pass

    def finish(self, *args, **kwargs):
        self.active = False
        self.outcome = kwargs.get("ok", True)

    def finish_cancelled(self, *args):
        self.active = False
        self.outcome = "cancelled"


def _manager_window(qtbot, manager):
    window = _Window()
    window._mod_manager = manager
    window._game_service = manager._game_service
    window._intake_progress = _Panel()
    window._raise_task_center = lambda: None
    window._overview_page.set_characters = lambda *args, **kwargs: None
    window._overview_page.update_mods_in_place = lambda *args: None
    window._overview_page.update_mod_toggle_in_place = lambda *args: None
    window._gallery_page = SimpleNamespace(update_mods_in_place=lambda *args: None)
    window._refresh_overview_mods = lambda: None
    qtbot.addWidget(window)
    window.show()
    return window


def test_single_disable_uses_background_entry(qtbot):
    window = _Window()
    qtbot.addWidget(window)
    mod = SimpleNamespace(mod_id="m", is_enabled=True)
    window._mod_manager = SimpleNamespace(
        get_mod=lambda mid: mod, needs_async_deploy=lambda mid: False, toggle_mod=lambda mid: None
    )
    calls = []
    window._on_overview_batch_disable = lambda ids: calls.append(ids)
    window._on_overview_mod_toggle("m")
    assert calls == [["m"]]


def test_preset_file_completion_does_not_publish_100_percent(qtbot):
    from src.ui.dialogs.preset_progress import PresetProgressDialog

    window = _Window()
    qtbot.addWidget(window)
    dialog = PresetProgressDialog("Target", window)
    window._preset_apply_progress = dialog
    window._on_preset_apply_progress(1, 1, SimpleNamespace(action="park", active_path="Mods/Skin"), 1.0)
    assert dialog.value() < dialog.maximum(), "files are complete but SQLite is not committed"


def test_failed_preset_finalization_always_releases_ui(qtbot, monkeypatch):
    window = _Window()
    qtbot.addWidget(window)
    dialog = QProgressDialog(window)
    window._preset_apply_progress = dialog
    window._overview_page._preset_selector.setEnabled(False)

    def fail(*args, **kwargs):
        raise OSError("journal unavailable")

    released = []
    window._mod_manager = SimpleNamespace(
        preset_manager=SimpleNamespace(fail_preset_apply=fail), release_library_operation=released.append
    )
    monkeypatch.setattr("src.ui.main_window.QMessageBox.critical", lambda *args: None)
    window._finish_preset_apply_failure(SimpleNamespace(), RuntimeError("cancelled"))
    assert window._preset_apply_progress is None
    assert window._overview_page._preset_selector.isEnabled()
    assert released == ["preset-apply"]


def test_clear_does_not_delete_a_running_preset_worker(qtbot):
    window = _Window()
    qtbot.addWidget(window)
    deleted = []
    worker = SimpleNamespace(isRunning=lambda: True, deleteLater=lambda: deleted.append(True))
    window._preset_apply_worker = worker
    window._clear_preset_apply_ui()
    assert not deleted
    assert window._preset_apply_worker is worker


@pytest.mark.parametrize("operation", ["disable", "preset", "preset_zero"])
def test_actual_operation_keeps_gui_live_during_preparation_and_commits(
    qtbot,
    temp_data_dir,
    tmp_path,
    monkeypatch,
    operation,
):
    from src.core.deployment_preparation import DeploymentPreparation
    from tests.core.test_batch_disable import _make_enabled_mods

    mm, mods, root = _make_enabled_mods(tmp_path, 1)
    if operation == "preset_zero":
        assert mm.toggle_mod(mods[0].mod_id)
    target = mm.preset_manager.create_preset("game_a", "Empty")
    window = _manager_window(qtbot, mm)
    entered, release = Event(), Event()
    real_prepare = DeploymentPreparation.prepare

    def slow_prepare(preparation):
        entered.set()
        assert release.wait(5)
        return real_prepare(preparation)

    monkeypatch.setattr(DeploymentPreparation, "prepare", slow_prepare)
    errors = []
    monkeypatch.setattr("src.ui.main_window.QMessageBox.critical", lambda *a: errors.append(a))
    heartbeats = []
    try:
        if operation == "disable":
            window._on_overview_mod_toggle(mods[0].mod_id)
            assert window._intake_progress.active
        else:
            window._on_preset_apply(target.id)
            assert window._preset_apply_progress.isVisible()
        QTimer.singleShot(0, lambda: heartbeats.append(True))
        qtbot.waitUntil(lambda: entered.is_set() and bool(heartbeats), timeout=1500)
        assert mm.get_mod(mods[0].mod_id).is_enabled == (operation != "preset_zero")
        if operation != "preset_zero":
            assert mm.is_library_path_protected(mods[0].path)
        assert not window._game_page.isEnabled()
        release.set()
        qtbot.waitUntil(
            lambda: (
                window._disable_worker is None
                and window._preset_apply_worker is None
                and not window._preset_apply_preparing
            ),
            timeout=5000,
        )
        assert not mm.get_mod(mods[0].mod_id).is_enabled
        assert window._game_page.isEnabled()
        assert not mm.preset_manager.has_pending_apply("game_a")
        assert mm._library_protection.snapshot() == (set(), set())
        assert not errors
        if operation.startswith("preset"):
            assert window._preset_apply_progress is None
            assert mm.preset_manager.get_current("game_a").id == target.id
        else:
            assert window._intake_progress.outcome is True
    finally:
        release.set()
        if window._preset_apply_worker is not None:
            window._stop_preset_apply()
        if window._disable_worker is not None:
            window._disable_worker.wait(5000)
            qtbot.waitUntil(lambda: window._disable_worker is None, timeout=5000)
        mm.stop_ini_sync()


@pytest.mark.parametrize("failure", ["cancel_close", "commit", "journal"])
def test_preset_cancel_or_failed_commit_restores_files_and_old_preset(
    qtbot,
    temp_data_dir,
    tmp_path,
    monkeypatch,
    failure,
):
    from tests.core.test_batch_disable import _make_enabled_mods

    mm, mods, root = _make_enabled_mods(tmp_path, 1)
    pm = mm.preset_manager
    old_id = pm.get_current("game_a").id
    target = pm.create_preset("game_a", "Empty")
    window = _manager_window(qtbot, mm)
    errors = []
    monkeypatch.setattr("src.ui.main_window.QMessageBox.critical", lambda *a: errors.append(a))
    if failure in ("commit", "journal"):

        def fail(*args, **kwargs):
            raise OSError("database busy")

        monkeypatch.setattr(pm, "commit_preset_apply" if failure == "commit" else "record_preset_apply_steps", fail)
    try:
        window._on_preset_apply(target.id)
        if failure == "cancel_close":
            qtbot.waitUntil(lambda: window._preset_apply_plan is not None, timeout=3000)
            # closeEvent 等待后台线程时不执行 Qt 事件循环。
            assert window._stop_preset_apply()
        else:
            qtbot.waitUntil(
                lambda: window._preset_apply_worker is None and not window._preset_apply_preparing, timeout=5000
            )
        saved = mm.get_mod(mods[0].mod_id)
        assert saved.is_enabled
        assert Path(mods[0].deployed_path).is_dir()
        assert pm.get_current("game_a").id == old_id
        assert not pm.has_pending_apply("game_a")
        assert mm._library_protection.snapshot() == (set(), set())
        assert window._preset_apply_progress is None
        assert window._game_page.isEnabled()
        assert bool(errors) == (failure != "cancel_close")
    finally:
        if window._preset_apply_worker is not None:
            window._stop_preset_apply()
        mm.stop_ini_sync()


def test_close_drains_owner_journal_request_and_restores_runtime_patch(qtbot, env):
    from tests.core.test_variant_preset_switch import _write_user_ini

    mm, mod, char = env["mm"], env["mod"], env["char"]
    pm = mm.preset_manager
    p1 = pm.create_preset("g1", "First", snapshot={char.id: [mod.mod_id]})
    p2 = pm.create_preset("g1", "Second", snapshot={char.id: [mod.mod_id]})
    apply_preset(pm, p1.id)
    _write_user_ini(env["user_ini"], "2")
    mm.variants.collect(trigger="test")
    before = env["user_ini"].read_bytes()
    window = _manager_window(qtbot, mm)
    try:
        window._on_preset_apply(p2.id)
        qtbot.waitUntil(lambda: window._preset_apply_plan is not None, timeout=3000)
        worker = window._preset_apply_worker
        deadline = time.monotonic() + 3
        # 模拟关闭时暂停界面事件循环，后台任务仍需由所有者提交数据库写入。
        while getattr(worker, "pending_patch", None) is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert worker.pending_patch is not None
        assert window._stop_preset_apply()
        assert pm.get_current("g1").id == p1.id
        assert not pm.has_pending_apply("g1")
        assert env["user_ini"].read_bytes() == before
        assert window._preset_apply_worker is None
        assert mm._library_protection.snapshot() == (set(), set())
    finally:
        if window._preset_apply_worker is not None:
            window._stop_preset_apply()


def test_disable_publication_failure_releases_busy_after_commit(qtbot, temp_data_dir, tmp_path):
    from tests.core.test_batch_disable import _make_enabled_mods

    mm, mods, root = _make_enabled_mods(tmp_path, 1)
    window = _manager_window(qtbot, mm)

    def fail(*args, **kwargs):
        raise RuntimeError("view disposed")

    window._overview_page.set_characters = fail
    try:
        window._on_overview_mod_toggle(mods[0].mod_id)
        qtbot.waitUntil(lambda: window._disable_worker is None, timeout=5000)
        assert not mm.get_mod(mods[0].mod_id).is_enabled
        assert window._game_page.isEnabled()
        assert not window._intake_progress.active
    finally:
        if window._disable_worker is not None:
            window._disable_worker.wait(5000)
        mm.stop_ini_sync()
