import json
import os
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import QEvent, QThread

from tests.ui.test_deployment_root_watch import _environment


def _refresh_idle(window):
    probe = window._library_probe
    discoveries = (window._library_discovery, window._library_local_discovery)
    return (
        not window._deployment_activation.running
        and probe.worker is None
        and not probe.pending
        and not probe.applying
        and probe._snapshot_iterator is None
        and all(
            controller.worker is None and controller.active is None and controller.pending is None
            for controller in discoveries
        )
        and window._intake_worker is None
        and not window._intake_settling
    )


@pytest.fixture
def real_window(temp_data_dir, qtbot, monkeypatch, native_qt_teardown, qapp):
    from src.core.config import Config
    from src.core.game_config_store import GameConfigStore
    from src.ui.main_window import MainWindow

    Config().set("gamebanana.enabled", False)
    for name in (
        "_init_plugins",
        "_maybe_startup_update_check",
        "apply_background",
        "_prepare_interaction_dialogs",
        "_prepare_directory_cards",
    ):
        monkeypatch.setattr(MainWindow, name, lambda self: None)
    windows = []

    def create(*, offline=False):
        from src.core.mod_package import write_container_marker
        from src.models.mod import Mod

        manager, service, mods, root = _environment(temp_data_dir / "deployment-home")
        # 轻量启用样本使用合成标记信息，真实窗口还会校验源元数据。
        for mod in mods:
            body = Path(mod.path)
            (body / "mod.ini").write_bytes((Path(mod.deployed_path) / "mod.ini").read_bytes())
            mod.content_hash = Mod.compute_sha1(mod.path)
            assert manager._index.update_mod(mod)
            write_container_marker(
                body.parent, body.name, mod.content_hash, mod_id=mod.mod_id, imported_at=mod.imported_at
            )
        GameConfigStore().set_last_game_id(manager.current_game_id)
        moved = temp_data_dir / "moved-while-closed"
        if offline:
            root.rename(moved)
        window = MainWindow()
        windows.append(window)
        qtbot.addWidget(window)
        window.show()
        return window, manager, service, mods, root, moved

    yield create

    for window in reversed(windows):
        window.close()
        qtbot.waitUntil(lambda: bool(getattr(window, "_appearance_disposed", False)), timeout=15000)
        qtbot.waitUntil(lambda: all(not thread.isRunning() for thread in window.findChildren(QThread)), timeout=15000)
        assert not window._file_watcher.is_running()
        assert not window._deploy_watcher.is_running()
        assert not window._deployment_activation.running
        assert window._library_probe.worker is None
        assert window._library_discovery.worker is None
        assert window._library_local_discovery.worker is None
        metadata = getattr(window, "_library_metadata", None)
        if metadata is not None:
            qtbot.waitUntil(lambda: all(not thread.is_alive() for thread in metadata.pool._threads), timeout=15000)
        window._mod_manager._index.close()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()


def _assert_active_root(window, manager, service, mods, root):
    """核对便携配置的真实目标、活动部署及监听均绑定到指定根目录。"""
    assert service.get_current_game().mods_path == str(root)
    saved = json.loads(window._game_store._path.read_text(encoding="utf-8"))
    stored_root = Path(next(game["mods_path"] for game in saved["games"] if game["id"] == manager.current_game_id))
    assert not stored_root.is_absolute() and not stored_root.drive
    app_root = window._game_store.path.parent.parent
    assert (app_root / stored_root).resolve(strict=True) == root.resolve(strict=True)
    assert manager._storage.get_deploy_dir() == root
    assert window._deploy_watcher.is_running()
    assert window._deploy_watcher._watch_path == root
    assert window._file_watcher.is_running()
    assert window._file_watcher._watch_path == manager._index.paths.roots[manager.current_game_id]
    for original in mods:
        current = manager.get_mod(original.mod_id)
        target = root / Path(original.deployed_path).name
        assert current.is_enabled and current.deployed_path == str(target)
        pairs = manager._ini_sync._pairs[original.mod_id]
        assert pairs and {pair.deployed_path for pair in pairs} == {target / "mod.ini"}
        assert manager._ini_sync._observer is not None
    assert manager._ini_sync._watched == {
        os.path.normcase(str(root)),
        os.path.normcase(str(manager._index.paths.roots[manager.current_game_id])),
    }
    assert not window._library_probe.paused


@pytest.mark.parametrize("movement", ["rename_root", "rename_parent", "move_root"])
def test_open_window_tracks_root_move_before_immediate_f5(real_window, qtbot, movement):
    window, manager, service, mods, old, _ = real_window()
    qtbot.waitUntil(lambda: _refresh_idle(window), timeout=15000)
    _assert_active_root(window, manager, service, mods, old)
    if movement == "rename_root":
        new = old.with_name("Renamed Mods")
        old.rename(new)
    elif movement == "rename_parent":
        parent = old.parent.with_name("Renamed Parent")
        old.parent.rename(parent)
        new = parent / old.name
    else:
        destination = old.parent.parent / "another-location"
        destination.mkdir()
        new = destination / old.name
        old.rename(new)
    window._refresh_current(manual=True)
    qtbot.waitUntil(lambda: not window._manual_refresh_active and _refresh_idle(window), timeout=15000)
    _assert_active_root(window, manager, service, mods, new)
    assert not old.exists()
    assert window._intake_progress._title.text() == "Mod 库已是最新"
    assert window._intake_progress._stop_btn.isHidden()


def test_offline_missing_root_remains_invalid_until_saved_selection(real_window, qtbot):
    """缺失部署根阻止启动及手动刷新，显式重选后按原身份恢复完整环境。"""
    from PySide6.QtWidgets import QMessageBox

    window, manager, service, mods, old, new = real_window(offline=True)
    qtbot.waitUntil(lambda: _refresh_idle(window), timeout=15000)
    notices = [
        box for box in window.findChildren(QMessageBox) if "部署目录无效，请重新选择" in box.text() and box.isVisible()
    ]
    assert len(notices) == 1
    assert str(old) in notices[0].informativeText()
    notices[0].accept()
    window._on_deployment_root_invalid(manager.current_game_id, str(old))
    assert not notices[0].isVisible(), "polling must not repeatedly reopen the same warning"
    assert manager._storage.get_deploy_dir() is None
    assert window._overview_page._deploy_status_label.text() == "部署目录无效，请重新选择"
    assert window._library_probe.paused
    assert not old.exists()
    for original in mods:
        current = manager.get_mod(original.mod_id)
        assert current.is_enabled and current.deployed_path == original.deployed_path
    window._refresh_current(manual=True)
    assert not window._manual_refresh_active
    assert window._intake_progress._detail.text().strip()
    assert window._intake_progress._stop_btn.isHidden()
    assert service.get_current_game().id == manager.current_game_id
    assert service.get_current_game().mods_path == str(old)
    assert manager._storage.get_deploy_dir() is None
    assert window._library_probe.paused
    for original in mods:
        current = manager.get_mod(original.mod_id)
        assert current.is_enabled and current.deployed_path == original.deployed_path
    assert not old.exists()
    service.save_game(replace(service.get_current_game(), mods_path=str(new)))
    window._on_games_changed()
    qtbot.waitUntil(lambda: _refresh_idle(window), timeout=15000)
    _assert_active_root(window, manager, service, mods, new)
    assert window._overview_page._deploy_status_label.text() != "部署目录无效，请重新选择"
    window._refresh_current(manual=True)
    qtbot.waitUntil(lambda: not window._manual_refresh_active and _refresh_idle(window), timeout=15000)
    assert window._intake_progress._title.text() == "Mod 库已是最新"
    assert not old.exists()


@pytest.mark.parametrize("phase", ["queued", "waiting"])
def test_task_panel_stop_cancels_f5_and_next_refresh_finishes(real_window, qtbot, monkeypatch, phase):
    from src.ui import library_reconcile_worker as probes

    window, manager, service, mods, root, _ = real_window()
    qtbot.waitUntil(lambda: _refresh_idle(window), timeout=15000)
    entered, release = Event(), Event()
    original_probe = probes.probe_library

    def held_probe(request, **kwargs):
        entered.set()
        assert release.wait(10)
        return original_probe(request, **kwargs)

    if phase == "waiting":
        monkeypatch.setattr(probes, "probe_library", held_probe)
    try:
        window._refresh_current(manual=True)
        assert window._manual_refresh_active
        if phase == "waiting":
            qtbot.waitUntil(entered.is_set, timeout=5000)
        window._intake_progress.stop_requested.emit()
        assert not window._manual_refresh_active
        assert window._intake_progress._detail.text() == "刷新已停止"
        assert window._intake_progress._stop_btn.isHidden()
        assert window._library_discovery.active is None and window._library_discovery.pending is None
        assert not window._library_probe.pending
        release.set()
        qtbot.waitUntil(lambda: _refresh_idle(window), timeout=15000)
        assert not window._library_probe.closing and not window._library_discovery.closing
        window._refresh_current(manual=True)
        qtbot.waitUntil(lambda: not window._manual_refresh_active and _refresh_idle(window), timeout=15000)
        assert window._intake_progress._title.text() == "Mod 库已是最新"
        _assert_active_root(window, manager, service, mods, root)
    finally:
        release.set()
