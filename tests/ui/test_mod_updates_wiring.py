from time import monotonic
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget

from src.ui.dialogs.mod_updates_dialog import ModUpdatesDialog
from src.ui.main_window import MainWindow
from tests.core.test_mod_update_game_switch import game_updates as game_updates
from tests.ui.test_mod_updates_dialog import _info, keep_dialog


class _Service(QObject):
    status_changed = Signal()
    check_started = Signal(int)
    progress = Signal(int, int)
    check_finished = Signal(dict)
    update_task_changed = Signal(str, dict)
    update_applied = Signal(str, object)
    update_failed = Signal(str, str)
    update_cancelled = Signal(str)
    update_deferred = Signal(str)

    def __init__(self):
        super().__init__()
        self.infos = {mid: _info(mid) for mid in ("m1", "m2")}
        self.requested = []
        self.errors = {}
        self.tasks = {}
        self.task_infos = {}
        self.snapshot = dict(
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
        return dict(self.infos)

    def check_snapshot(self):
        return dict(self.snapshot)

    def updatable_ids(self):
        return set(self.infos)

    def failures(self):
        return dict(self.errors)

    def is_checking(self):
        return False

    def update_task_snapshots(self):
        return {mid: dict(state) for mid, state in self.tasks.items()}

    def update_task_infos(self):
        return dict(self.task_infos)

    def active_update_ids(self, *, all_games=False):
        return [
            mid for mid, state in self.tasks.items() if state["phase"] not in {"done", "failed", "canceled", "deferred"}
        ]

    def publish_task(self, mid, phase, *, error=""):
        self.task_infos.setdefault(mid, self.infos[mid])
        self.tasks[mid] = dict(phase=phase, game_id="game-a", error=error)
        self.update_task_changed.emit(mid, self.tasks[mid])

    def apply_update(self, mid, chosen_file=None):
        self.requested.append(mid)
        self.publish_task(mid, "preparing")


class _Host(QWidget):
    _connect_mod_update_service = MainWindow._connect_mod_update_service
    _on_update_check_started = MainWindow._on_update_check_started
    _on_update_check_progress = MainWindow._on_update_check_progress
    _on_update_check_finished = MainWindow._on_update_check_finished
    _on_update_status_changed = MainWindow._on_update_status_changed
    _on_apply_update = MainWindow._on_apply_update
    _start_mod_update = MainWindow._start_mod_update
    _on_update_all = MainWindow._on_update_all
    _on_update_applied = MainWindow._on_update_applied
    _on_update_failed = MainWindow._on_update_failed
    _on_update_cancelled = MainWindow._on_update_cancelled
    _on_update_deferred = MainWindow._on_update_deferred
    _on_update_task_changed = MainWindow._on_update_task_changed
    _is_current_mod_update = MainWindow._is_current_mod_update
    _restore_mod_update_task = MainWindow._restore_mod_update_task
    _sync_mod_update_notification = MainWindow._sync_mod_update_notification

    def __init__(self, service=None, manager=None):
        super().__init__()
        self._config = {}
        self._deploy_workers = {}
        self._deploy_active = set()
        self._mod_update_service = service if service is not None else _Service()
        self._mod_manager = (
            manager
            if manager is not None
            else SimpleNamespace(
                current_game_id="game-a",
                get_mods=lambda: [SimpleNamespace(id=mid) for mid in self._mod_update_service.infos],
            )
        )
        self.messages = []
        self.opened = 0
        self._overview_page = SimpleNamespace(
            set_update_status_text=self.messages.append,
            set_updatable=lambda ids: None,
        )
        self._mod_updates_dialog = ModUpdatesDialog(
            list(self._mod_update_service.get_update_status().values()),
            enable_actions=True,
            parent=self,
        )
        self._mod_updates_dialog.setProperty("gameId", self._mod_manager.current_game_id)
        self._connect_mod_update_service()

    def _open_mod_updates_center(self):
        self.opened += 1

    def _set_overview_update_count(self, count):
        self.overview_count = count


def test_recheck_updates_existing_dialog_without_opening_another(qtbot):
    host = _Host()
    keep_dialog(qtbot, host)
    dialog = host._mod_updates_dialog
    host._update_check_is_manual = True
    host._mod_update_service.snapshot.update(
        checking=True, phase="connecting", total=2, started_at=monotonic(), last_progress_at=monotonic()
    )
    host._on_update_check_started(2)
    assert not dialog._recheck_btn.isEnabled()
    host._mod_update_service.infos.pop("m2")
    host._mod_update_service.snapshot.update(checking=False, phase="done", found=1, done=2, finished_at=monotonic())
    host._on_update_check_finished({"found": 1, "failed": 0})
    assert host.opened == 0
    assert dialog.pending_ids() == ["m1"]
    assert dialog._recheck_btn.isEnabled()
    assert not host._update_check_is_manual


def test_real_update_handlers_disable_duplicates_and_preserve_completion(qtbot):
    host = _Host()
    keep_dialog(qtbot, host)
    dialog = host._mod_updates_dialog
    host._on_apply_update("m1")
    host._on_apply_update("m1")
    assert host._mod_update_service.requested == ["m1"]
    assert not dialog._rows_by_id["m1"].update_button.isEnabled()
    host._mod_update_service.publish_task("m1", "failed", error="下载失败")
    host._mod_update_service.update_failed.emit("m1", "下载失败")
    assert dialog._rows_by_id["m1"].update_button.isEnabled()
    host._on_apply_update("m1")
    host._mod_update_service.publish_task("m1", "done")
    host._mod_update_service.infos.pop("m1")
    host._mod_update_service.update_applied.emit("m1", SimpleNamespace())
    host._on_update_status_changed()
    assert dialog._rows_by_id["m1"].state == "done"
    assert dialog.pending_ids() == ["m2"]


def test_failed_check_never_reports_all_mods_current(qtbot):
    host = _Host()
    keep_dialog(qtbot, host)
    host._update_check_is_manual = True
    host._mod_update_service.infos.clear()
    host._mod_update_service.errors = {"m1": "连接失败"}
    host._mod_update_service.snapshot.update(phase="partial", total=1, done=1, failed=1)
    host._on_update_check_finished({"found": 0, "failed": 1})
    assert "最新" not in host.messages[-1]
    assert host._mod_updates_dialog._failures == {"m1": "连接失败"}


def test_previous_game_check_finishing_unlocks_current_dialog(qtbot, game_updates):
    manager, service, _, _ = game_updates
    service.check_updates(force=True)
    manager.set_game("game-b")
    service.load_status()
    host = _Host(service, manager)
    keep_dialog(qtbot, host)
    dialog = host._mod_updates_dialog
    dialog.set_checking(service.is_checking())
    assert not dialog._recheck_btn.isEnabled()
    service._checker.finished.emit({"found": 1, "up_to_date": 0, "failed": 0})
    assert dialog._recheck_btn.isEnabled()
    assert dialog.pending_ids() == [], "不能把旧游戏结果展示到当前游戏"
