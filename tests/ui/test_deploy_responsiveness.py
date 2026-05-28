from __future__ import annotations

import time
import zipfile
from collections import deque
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QLabel, QMainWindow, QWidget

from src.core.config import Config
from src.models.mod import Mod
from src.ui.deploy_worker import DeployWorker
from src.ui.main_window import MainWindow


@pytest.fixture(autouse=True)
def deployment_settings(temp_data_dir):
    # 生命周期测试使用固定并发限制，不依赖本机硬件。
    config = Config()
    config.set("mods.deploy_concurrency", 2)
    return config


@pytest.fixture
def fake_source_confirmation(monkeypatch):
    """仅计时的用例替换文件操作，真实归档排队用例保留实际读写。"""
    from src.core import operation_preflight

    monkeypatch.setattr(
        operation_preflight,
        "confirm_source",
        lambda *args, **kwargs: SimpleNamespace(content_hash="a" * 40, recheck=lambda: None),
    )
    monkeypatch.setattr(operation_preflight, "confirm_metadata", lambda receipt: None)


class _LifecycleStorage:
    """提供暂存和启用边界，由各用例控制文件操作耗时。"""

    def staged(self, name, options):
        assert options.get("active") is False
        return self.get_deploy_dir() / ("DISABLED" + name)

    def active_name_from_parked(self, path, identity):
        name = Path(path).name
        assert name.startswith("DISABLED")
        return name[len("DISABLED") :]

    def restore_parked_deployment(self, path, name):
        return self.get_deploy_dir() / name

    def _check_deployment_identity(self, path, identity):
        return {"mod_id": identity}

    def remove_deploy_path(self, path):
        return True

    def park_deployment(self, path, identity):
        return Path(path).with_name("DISABLED" + Path(path).name)


def test_worker_limits_progress_signals_and_keeps_the_final_value(qtbot, monkeypatch, fake_source_confirmation):
    # 归档可能在同一界面帧内报告数千个成员。
    monkeypatch.setattr(time, "monotonic", lambda: 10.0)

    class Storage:
        def get_deploy_dir(self):
            return Path("deploy")

        def materialize_deployment(self, source, mod_id, deploy_name, *, progress_cb, should_cancel):
            for index in range(10_000):
                progress_cb(index / 10_000)
            return Path("deployed")

    worker = DeployWorker("mod", "source.zip", Storage())
    progress = []
    results = []
    worker.progress.connect(lambda mid, frac: progress.append((mid, frac)))
    worker.finished_deploy.connect(lambda *args: results.append(args))

    worker.run()

    assert len(progress) <= 2, "one burst must not enqueue one GUI update per file"
    assert progress[0] == ("mod", 0.0)
    assert progress[-1] == ("mod", 1.0)
    assert results == [("mod", True, "deployed")]


def test_worker_reports_live_progress_without_repeating_completion(qtbot, monkeypatch, fake_source_confirmation):
    clock = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    class Storage:
        def get_deploy_dir(self):
            return Path("deploy")

        def materialize_deployment(self, source, mod_id, deploy_name, *, progress_cb, should_cancel):
            for value in (0.0, 0.25, 0.5, 0.75, 1.0, 1.0):
                clock[0] += 0.1
                progress_cb(value)
            return Path("deployed")

    worker = DeployWorker("mod", "source.zip", Storage())
    progress = []
    worker.progress.connect(lambda mid, frac: progress.append(frac))

    worker.run()

    assert progress == [0.0, 0.25, 0.5, 0.75, 1.0]


@pytest.mark.parametrize("raises", [False, True])
def test_worker_does_not_report_success_for_failed_deployment(qtbot, raises, fake_source_confirmation):
    class Storage:
        def get_deploy_dir(self):
            return Path("deploy")

        def materialize_deployment(self, source, mod_id, deploy_name, *, progress_cb, should_cancel):
            progress_cb(0.25)
            if raises:
                raise OSError("disk unavailable")
            return None

    worker = DeployWorker("mod", "source.zip", Storage())
    progress = []
    results = []
    worker.progress.connect(lambda mid, frac: progress.append(frac))
    worker.finished_deploy.connect(lambda *args: results.append(args))

    worker.run()

    assert 1.0 not in progress
    assert results == [("mod", False, "")]


class _DeployWindow(MainWindow):
    """使用真实协调器，仅替换部署文件操作和无关页面。"""

    def __init__(self, mods, storage):
        QMainWindow.__init__(self)
        self._config = Config()
        self._deploy_workers = {}
        self._pending_deploy_ids = deque()
        self._deploy_active = set()
        self._deploy_stopping = False
        self._deploy_close_pending = False
        self._deploy_game_id = ""
        self._deploy_environment_dirty = False
        self._deployment_activation = SimpleNamespace(
            busy=False,
            is_current=lambda root: storage.get_deploy_dir() == Path(root),
            start=lambda root: self._mod_manager.set_deploy_dir(root),
        )
        self._preset_apply_worker = None
        self._mod_update_service = SimpleNamespace(
            active_update_ids=lambda **kwargs: [],
            refresh_update_concurrency=lambda: None,
        )
        self._game_page = QWidget(self)
        self._game_page.reload = lambda: None
        self.finished_mods = []
        self.feedback = {}
        self.protected_operations = {}
        self._mods = {mod.mod_id: mod for mod in mods}
        self._game_service = SimpleNamespace(current_game_id="game")
        self._mod_manager = SimpleNamespace(
            _storage=storage,
            has_deploy_dir=lambda: storage.get_deploy_dir() is not None,
            protect_library_operation=lambda token, **values: self.protected_operations.update({token: values}),
            release_library_operation=lambda token: self.protected_operations.pop(token, None),
            get_mod=self._mods.get,
            finalize_enable=self._commit_enable,
            report_operation_failure=lambda error: self.feedback.update({error.mod_id: "failed"}),
            get_characters_for_ui=lambda gid: [],
        )
        self._overview_page = SimpleNamespace(
            _deploy_status_label=QLabel(self),
            begin_deploy_overlay=lambda mid: self.feedback.update({mid: "pending"}),
            update_deploy_overlay=lambda mid, frac: None,
            finish_deploy_overlay=self._finish_feedback,
            update_mod_toggle_in_place=lambda *args: None,
            set_characters=lambda *args, **kwargs: None,
        )

    def _commit_enable(self, mod_id, *, deploy_ok, deploy_path="", source_receipt=None):
        assert QThread.currentThread() == self.thread(), "SQLite commit must stay on the GUI thread"
        assert "deploy:" + mod_id in self.protected_operations
        assert self.protected_operations["deploy:" + mod_id]["mod_ids"] == (mod_id,)
        assert source_receipt is not None
        source_receipt.recheck()
        mod = self._mods[mod_id]
        mod.enable()
        self.finished_mods.append(mod_id)
        return mod

    def _finish_feedback(self, mod_id, *, success=True, error=""):
        self.feedback[mod_id] = "success" if success else "failed"

    def _ensure_toast(self):
        return SimpleNamespace(done=lambda *args, **kwargs: None)

    def closeEvent(self, event):
        # 此样本没有创建应用服务。
        QMainWindow.closeEvent(self, event)


@pytest.mark.parametrize(("configured", "concurrency"), [(1, 1), (2, 2), (4, 4), (0, 3)])
def test_rapid_enable_is_bounded_keeps_duplicates_out_and_drains_queue(
    qtbot,
    deployment_settings,
    monkeypatch,
    configured,
    concurrency,
    fake_source_confirmation,
):
    monkeypatch.setattr("src.core.deploy_concurrency.recommended_deploy_concurrency", lambda: 3)
    deployment_settings.set("mods.deploy_concurrency", configured)
    release = Event()
    lock = Lock()
    started = []
    active = 0
    maximum = 0

    class Storage(_LifecycleStorage):
        def get_deploy_dir(self):
            return Path("deploy")

        def materialize_deployment(self, source, deployment_id, name, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                started.append(source)
            try:
                assert release.wait(5), "test failed to release deployment IO"
                return self.staged(name, kwargs)
            finally:
                with lock:
                    active -= 1

    mods = [Mod(mod_id=str(i), game_id="game", path=f"{i}.zip") for i in range(8)]
    window = _DeployWindow(mods, Storage())
    qtbot.addWidget(window)
    heartbeat = []
    try:
        for mod in mods:
            window._start_async_deploy(mod.mod_id, [])
            window._start_async_deploy(mod.mod_id, [])
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        qtbot.waitUntil(lambda: len(started) >= concurrency and bool(heartbeat), timeout=1500)
        qtbot.wait(30)
        assert len(started) == concurrency, "rapid clicks must respect the user's deployment limit"
        assert window._mods_being_deployed() == {mod.mod_id for mod in mods}
        assert set(window.protected_operations) == {"deploy:" + mod.mod_id for mod in mods}
        assert all(state == "pending" for state in window.feedback.values())

        release.set()
        qtbot.waitUntil(lambda: not window._deploy_workers, timeout=5000)
        assert not window.protected_operations
        assert maximum <= concurrency
        assert sorted(started) == [f"{i}.zip" for i in range(8)]
        assert sorted(window.finished_mods) == [str(i) for i in range(8)]
        assert all(mod.is_enabled for mod in mods)
        assert set(window.feedback.values()) == {"success"}
    finally:
        release.set()
        for worker in list(window._deploy_workers.values()):
            worker.wait(5000)
        qtbot.waitUntil(lambda: not window._deploy_workers, timeout=5000)


def test_settings_change_resizes_running_queue_without_interrupting_work(qtbot, fake_source_confirmation):
    from src.ui.pages.settings import SettingsPage

    releases = [Event() for _ in range(7)]
    started = []

    class Storage(_LifecycleStorage):
        def get_deploy_dir(self):
            return Path("deploy")

        def materialize_deployment(self, source, deployment_id, name, **kwargs):
            index = int(Path(source).stem)
            started.append(index)
            assert releases[index].wait(5)
            assert not kwargs["should_cancel"](), "lowering concurrency must not cancel active Mods"
            return self.staged(name, kwargs)

    mods = [Mod(mod_id=str(i), game_id="game", path=f"{i}.zip") for i in range(7)]
    window = _DeployWindow(mods, Storage())
    page = SettingsPage()
    qtbot.addWidget(window)
    qtbot.addWidget(page)
    page.settings_changed.connect(window._on_settings_changed)
    try:
        for mod in mods:
            window._start_async_deploy(mod.mod_id, [])
        qtbot.waitUntil(lambda: len(started) == 2, timeout=1500)

        page._deploy_concurrency_input.setText("4")
        page._deploy_concurrency_input.editingFinished.emit()
        qtbot.waitUntil(lambda: len(started) == 4, timeout=1500)

        page._deploy_concurrency_input.setText("1")
        page._deploy_concurrency_input.editingFinished.emit()
        assert len(window._deploy_active) == 4
        for index in range(3):
            releases[index].set()
            qtbot.waitUntil(lambda i=index: str(i) in window.finished_mods, timeout=1500)
            qtbot.wait(20)
            assert len(started) == 4, "queued work must wait until active deployments fall below the new limit"

        releases[3].set()
        qtbot.waitUntil(lambda: len(started) == 5, timeout=1500)
        assert started[-1] == 4, "new slots must still be assigned in click order"
        assert len(window._deploy_active) == 1
    finally:
        for release in releases:
            release.set()
        qtbot.waitUntil(lambda: not window._deploy_workers, timeout=5000)
    assert set(window.finished_mods) == {mod.mod_id for mod in mods}
    assert all(mod.is_enabled for mod in mods)


@pytest.mark.parametrize("failure", ["deploy", "commit"])
def test_failed_deploy_releases_queue_slot_and_can_be_retried(qtbot, monkeypatch, failure, fake_source_confirmation):
    from src.ui import main_window

    monkeypatch.setattr(main_window.QMessageBox, "warning", lambda *args: None)
    should_fail = [True]

    class Storage(_LifecycleStorage):
        def get_deploy_dir(self):
            return Path("deploy")

        def materialize_deployment(self, source, deployment_id, name, **kwargs):
            if failure == "deploy" and source == "0.zip" and should_fail[0]:
                raise OSError("disk unavailable")
            return self.staged(name, kwargs)

    mods = [Mod(mod_id=str(i), game_id="game", path=f"{i}.zip") for i in range(5)]
    window = _DeployWindow(mods, Storage())
    qtbot.addWidget(window)
    commit = window._commit_enable

    def commit_or_fail(mod_id, *, deploy_ok, deploy_path="", source_receipt=None):
        if failure == "commit" and mod_id == "0" and should_fail[0]:
            raise OSError("database busy")
        return commit(mod_id, deploy_ok=deploy_ok, deploy_path=deploy_path, source_receipt=source_receipt)

    window._mod_manager.finalize_enable = commit_or_fail
    for mod in mods:
        window._start_async_deploy(mod.mod_id, [])
    qtbot.waitUntil(lambda: not window._deploy_workers, timeout=3000)

    assert not mods[0].is_enabled
    assert window.feedback["0"] == "failed"
    assert sorted(window.finished_mods) == ["1", "2", "3", "4"]
    assert not window._deploy_active
    assert not window.protected_operations

    should_fail[0] = False
    window._start_async_deploy("0", [])
    qtbot.waitUntil(lambda: not window._deploy_workers, timeout=3000)
    assert mods[0].is_enabled
    assert window.feedback["0"] == "success"


def test_stopping_queue_commits_completed_work_and_allows_retry_before_old_signals(qtbot, fake_source_confirmation):
    both_started = Event()
    unblock = Event()
    lock = Lock()
    started = []

    class Storage(_LifecycleStorage):
        def get_deploy_dir(self):
            return Path("deploy")

        def materialize_deployment(self, source, deployment_id, name, **kwargs):
            with lock:
                started.append(source)
                if len(started) == 2:
                    both_started.set()
            if source == "1.zip":
                while not unblock.wait(0.005):
                    if kwargs["should_cancel"]():
                        return None
            return self.staged(name, kwargs)

    mods = [Mod(mod_id=str(i), game_id="game", path=f"{i}.zip") for i in range(5)]
    window = _DeployWindow(mods, Storage())
    qtbot.addWidget(window)
    try:
        for mod in mods:
            window._start_async_deploy(mod.mod_id, [])
        # 保持完成信号排队，模拟 closeEvent 等待线程时不分发事件的状态。
        assert both_started.wait(1)
        assert window._deploy_workers["0"].wait(1000)

        assert window._stop_async_deploys(timeout_ms=1500)
        assert sorted(started) == ["0.zip", "1.zip"]
        assert window.finished_mods == ["0"]
        assert mods[0].is_enabled
        assert all(not mod.is_enabled for mod in mods[1:])
        assert not window._deploy_workers
        assert not window._deploy_active
        assert not window.protected_operations

        unblock.set()
        window._start_async_deploy("1", [])
        # 已取消任务的迟到完成信号不能移除或销毁新任务。
        qtbot.waitUntil(lambda: not window._deploy_workers, timeout=3000)
        assert window.finished_mods == ["0", "1"]
        assert mods[1].is_enabled
        assert window.feedback["1"] == "success"
    finally:
        unblock.set()
        qtbot.waitUntil(lambda: not window._deploy_workers, timeout=5000)


@pytest.mark.parametrize("change", ["game", "deploy_root", "deploy_root_after_close_timeout"])
def test_queue_keeps_game_and_deploy_root_until_every_result_is_committed(qtbot, change, fake_source_confirmation):
    release = Event()
    entered = []
    commits = []

    class Storage(_LifecycleStorage):
        root = Path("original-Mods")

        def get_deploy_dir(self):
            return self.root

        def materialize_deployment(self, source, deployment_id, name, **kwargs):
            entered.append((source, self.root))
            assert release.wait(5)
            return self.staged(name, kwargs)

    storage = Storage()
    mods = [Mod(mod_id=str(i), game_id="game", path=f"{i}.zip") for i in range(3)]
    window = _DeployWindow(mods, storage)
    qtbot.addWidget(window)
    commit = window._commit_enable

    def record_commit(mod_id, *, deploy_ok, deploy_path="", source_receipt=None):
        commits.append(window._game_service.current_game_id)
        return commit(mod_id, deploy_ok=deploy_ok, deploy_path=deploy_path, source_receipt=source_receipt)

    window._mod_manager.finalize_enable = record_commit
    window._mod_manager.set_deploy_dir = lambda root: setattr(storage, "root", Path(root))
    window._mod_manager.variants = SimpleNamespace(refresh_watch=lambda: None)
    window._game_service.get_current_game = lambda: SimpleNamespace(
        mods_path="updated-Mods", migoto_path="", exe_path=""
    )
    window._migoto = SimpleNamespace()
    watcher = SimpleNamespace(stop=lambda: None, start=lambda *args: None)
    window._deploy_watcher = watcher
    window._deploy_state_poll = watcher
    window._reconcile_deploy_state = lambda: None
    window._update_launch_buttons = lambda: None
    try:
        for mod in mods:
            window._start_async_deploy(mod.mod_id, [])
        qtbot.waitUntil(lambda: len(entered) == 2, timeout=1500)
        assert not window._game_page.isEnabled(), "game/environment edits must wait for deployment commits"

        if change == "game":
            # 游戏页在发出游戏切换信号前更新共享服务。
            window._game_service.current_game_id = "other-game"
            window._on_game_changed("other-game")
        else:
            window._apply_migoto_for_current_game()
        assert window._game_service.current_game_id == "game"
        assert storage.root == Path("original-Mods")

        if change == "deploy_root_after_close_timeout":
            assert not window._stop_async_deploys(timeout_ms=1)
        release.set()
        qtbot.waitUntil(lambda: not window._deploy_workers, timeout=3000)
        completed = 2 if change == "deploy_root_after_close_timeout" else 3
        assert sorted(entered) == [(f"{i}.zip", Path("original-Mods")) for i in range(completed)]
        assert commits == ["game"] * completed
        assert window._game_page.isEnabled()
        if change != "game":
            qtbot.waitUntil(lambda: storage.root == Path("updated-Mods"), timeout=1000)
    finally:
        release.set()
        qtbot.waitUntil(lambda: not window._deploy_workers, timeout=5000)


def test_queued_archives_reach_disk_index_and_current_preset(qtbot, temp_data_dir, tmp_path, monkeypatch):
    from src.core.game_config_store import GameConfigStore
    from src.core.mod_manager import ModManager
    from src.ui import main_window
    from tests.helpers import register_games, reset_game_service

    register_games("game")
    games = GameConfigStore().get_games()
    games[0]["migoto_path"] = str(tmp_path)
    GameConfigStore().set_games(games)
    (tmp_path / "d3dx.ini").write_text("[Include]\ninclude_recursive = Mods\n", encoding="utf-8")
    manager = ModManager()
    manager.set_game("game")
    manager._get_library_root("game").mkdir(parents=True, exist_ok=True)
    category = manager.add_character(name="Skins", node_type="category")
    deploy_root = tmp_path / "Mods"
    manager.set_deploy_dir(str(deploy_root))
    errors = []
    monkeypatch.setattr(main_window.QMessageBox, "warning", lambda *args: errors.append(args))
    window = None
    try:
        mods = []
        for index in range(4):
            archive = tmp_path / f"Skin{index}.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr(
                    f"Skin{index}/mod.ini",
                    f"namespace = Skin{index}\n[Constants]\nglobal persist $variant = 0\n",
                )
                output.writestr(f"Skin{index}/mesh.buf", bytes([index]) * 1024)
            mod = manager.import_mod(
                str(archive),
                node_id=category.id,
                auto_preview=False,
                import_format="zip",
                strict_format=True,
            )
            assert mod is not None
            mods.append(mod)
        source_hashes = {mod.mod_id: Mod.compute_sha1(mod.path) for mod in mods}
        window = _DeployWindow(mods, manager._storage)
        window._mod_manager = manager
        window._game_service = manager._game_service
        qtbot.addWidget(window)

        for mod in mods:
            window._on_overview_mod_toggle(mod.mod_id)
        qtbot.waitUntil(lambda: not window._deploy_workers, timeout=5000)

        assert errors == []
        assert set(window.feedback.values()) == {"success"}
        assert set(manager.preset_manager.get_current("game").enabled_mod_ids) == {mod.mod_id for mod in mods}
        for index, mod in enumerate(mods):
            saved = manager.get_mod(mod.mod_id)
            assert saved.is_enabled
            target = Path(saved.deployed_path)
            assert target.parent == deploy_root
            assert (target / "mesh.buf").read_bytes() == bytes([index]) * 1024
            assert f"namespace = Skin{index}" in (target / "mod.ini").read_text(encoding="utf-8")
            assert Mod.compute_sha1(mod.path) == source_hashes[mod.mod_id]
    finally:
        if window is not None:
            assert window._stop_async_deploys()
        manager.stop_ini_sync()
        manager.variants.stop_watch()
        reset_game_service()
