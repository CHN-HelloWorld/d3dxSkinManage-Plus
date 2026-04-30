from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel

from src.core.game_service import GameService
from tests.core.test_deployment_activation import _activation_library

pytestmark = pytest.mark.usefixtures("native_qt_teardown")


def _environment(temp_data_dir, count=2):
    manager, mods = _activation_library(temp_data_dir, count)
    service = GameService()
    service.current_game_id = manager.current_game_id
    root = manager._storage.get_deploy_dir()
    service.save_game(replace(service.get_current_game(), mods_path=str(root)))
    return manager, service, mods, root


def test_live_root_move_updates_configuration_and_only_deployment_paths(qtbot, temp_data_dir):
    from src.ui.deployment_root_watch import DeploymentRootWatch

    manager, service, mods, old = _environment(temp_data_dir)
    source_bytes = [(Path(mod.path).parent / ".d3dxmod").read_bytes() for mod in mods]
    watch = DeploymentRootWatch(manager, service, busy=lambda: False)
    moved = []
    watch.relocated.connect(lambda *args: moved.append(args))
    new = old.with_name("Renamed Mods")
    try:
        old.rename(new)
        result = watch.check_current()
        assert result.status == "current"
        assert service.get_current_game().mods_path == str(new)
        assert moved == [(manager.current_game_id, str(old), str(new))]
        assert not old.exists()
        for mod, before in zip(mods, source_bytes):
            actual = manager.get_mod(mod.mod_id)
            assert actual.is_enabled
            assert actual.deployed_path == str(new / Path(mod.deployed_path).name)
            assert (Path(mod.path).parent / ".d3dxmod").read_bytes() == before
        watch.check_current()
        assert len(moved) == 1
    finally:
        watch.close()


def test_offline_missing_root_is_reported_once_and_keeps_records(qtbot, temp_data_dir):
    from src.ui.deployment_root_watch import DeploymentRootWatch

    manager, service, mods, old = _environment(temp_data_dir)
    old.rename(old.with_name("Moved while closed"))
    watch = DeploymentRootWatch(manager, service, busy=lambda: False)
    invalid = []
    watch.invalid.connect(lambda *args: invalid.append(args))
    try:
        for _ in range(3):
            assert watch.check_current().status == "missing"
            watch.poll()
        assert invalid == [(manager.current_game_id, str(old))]
        assert service.get_current_game().mods_path == str(old)
        assert not old.exists()
        assert manager.get_mod(mods[0].mod_id).is_enabled
        assert manager.get_mod(mods[0].mod_id).deployed_path == mods[0].deployed_path
    finally:
        watch.close()


def test_rebase_waits_for_an_active_operation_then_uses_latest_location(qtbot, temp_data_dir):
    from src.ui.deployment_root_watch import DeploymentRootWatch

    manager, service, mods, old = _environment(temp_data_dir)
    busy = [True]
    watch = DeploymentRootWatch(manager, service, busy=lambda: busy[0])
    try:
        intermediate = old.with_name("Intermediate")
        final = old.with_name("Final")
        old.rename(intermediate)
        assert watch.check_current().status == "moved"
        assert service.get_current_game().mods_path == str(old)
        intermediate.rename(final)
        busy[0] = False
        watch.poll()
        assert service.get_current_game().mods_path == str(final)
        assert manager.get_mod(mods[0].mod_id).deployed_path == str(final / "Mod-0000")
    finally:
        watch.close()


def test_rebase_does_not_load_full_mod_metadata_or_commit_each_row(qtbot, temp_data_dir, monkeypatch):
    manager, service, mods, old = _environment(temp_data_dir, 20)
    new = old.with_name("Renamed")
    old.rename(new)
    statements = []
    manager._index._db().set_trace_callback(statements.append)

    def full_metadata(*args, **kwargs):
        raise AssertionError("Root remapping must only update deployment records")

    monkeypatch.setattr(manager._index, "get_all_mods", full_metadata)
    assert manager.rebase_deployments(manager.current_game_id, old, new) == 20
    assert manager.get_mod(mods[-1].mod_id).deployed_path == str(new / "Mod-0019")
    assert sum(sql.upper() == "COMMIT" for sql in statements) == 1


def test_activation_rejects_missing_root_without_recreating_it(qtbot, temp_data_dir):
    from src.ui.deployment_activation import DeploymentActivationController

    manager, service, mods, old = _environment(temp_data_dir)
    old.rename(old.with_name("Offline"))
    controller = DeploymentActivationController(manager)
    errors = []
    controller.failed.connect(lambda *args: errors.append(args))
    try:
        assert not controller.start(str(old))
        assert errors and "部署目录无效，请重新选择" in errors[0][1]
        assert not old.exists()
        assert manager.get_mod(mods[0].mod_id).is_enabled
    finally:
        controller.close()
        qtbot.waitUntil(lambda: not controller.running)


def test_reselecting_offline_moved_root_recovers_by_mod_identity(qtbot, temp_data_dir):
    from src.ui.deployment_activation import DeploymentActivationController

    manager, service, mods, old = _environment(temp_data_dir)
    new = old.with_name("Selected new location")
    old.rename(new)
    service.save_game(replace(service.get_current_game(), mods_path=str(new)))
    controller = DeploymentActivationController(manager)
    settled = []
    controller.settled.connect(lambda *args: settled.append(args))
    try:
        controller.start(str(new))
        qtbot.waitUntil(lambda: not controller.running)
        assert settled
        assert manager.get_mod(mods[0].mod_id).is_enabled
        assert manager.get_mod(mods[0].mod_id).deployed_path == str(new / "Mod-0000")
        assert mods[0].mod_id in manager._ini_sync._pairs
    finally:
        controller.close()
        qtbot.waitUntil(lambda: not controller.running)
        manager.stop_ini_sync()


def test_failed_root_config_save_keeps_index_and_can_retry(qtbot, temp_data_dir, monkeypatch):
    from src.core import game_config_store
    from src.ui.deployment_root_watch import DeploymentRootWatch

    manager, service, mods, old = _environment(temp_data_dir)
    watch = DeploymentRootWatch(manager, service, busy=lambda: False)
    errors, moves = [], []
    watch.failed.connect(lambda *args: errors.append(args))
    watch.relocated.connect(lambda *args: moves.append(args))
    new = old.with_name("Renamed")
    original = game_config_store.write_json_atomic

    def fail_save(*args, **kwargs):
        raise PermissionError("config file locked")

    try:
        old.rename(new)
        monkeypatch.setattr(game_config_store, "write_json_atomic", fail_save)
        watch.check_current()
        assert errors and not moves
        assert service.get_current_game().mods_path == str(old)
        assert manager.get_mod(mods[0].mod_id).deployed_path == mods[0].deployed_path
        monkeypatch.setattr(game_config_store, "write_json_atomic", original)
        watch.poll()
        assert len(moves) == 1
        assert service.get_current_game().mods_path == str(new)
    finally:
        watch.close()


def _refresh_host(manager, service):
    from src.ui.deployment_activation import DeploymentActivationController
    from src.ui.deployment_root_watch import DeploymentRootWatch
    from src.ui.library_discovery import LibraryDiscoveryController
    from src.ui.library_reconcile_worker import LibraryReconcileController
    from src.ui.main_window import MainWindow

    messages = []
    probe = LibraryReconcileController(manager, busy=lambda: False)
    discovery = LibraryDiscoveryController(manager, probe, busy=lambda: False, consume=lambda *args: None)
    local = LibraryDiscoveryController(manager, probe, busy=lambda: False, consume=lambda *args: None)
    root_watch = DeploymentRootWatch(manager, service, busy=lambda: False)
    label = QLabel()
    host = SimpleNamespace(
        _mod_manager=manager,
        _game_service=service,
        _library_probe=probe,
        _library_discovery=discovery,
        _library_local_discovery=local,
        _deployment_root_watch=root_watch,
        _deployment_activation=DeploymentActivationController(manager),
        _deploy_workers={},
        _deploy_active=set(),
        _deploy_environment_dirty=False,
        _preset_apply_worker=None,
        _intake_worker=None,
        _intake_settling=False,
        _import_active=False,
        _watcher_suppress_until=0,
        _intake_rescan_pending=False,
        _intake_rescan_manual=False,
        _deploy_changed_paths=set(),
        _deployment_events=[],
        _deploy_watcher=SimpleNamespace(stop=lambda: None),
        _deploy_state_poll=QTimer(),
        _deploy_watcher_debounce=QTimer(),
        _overview_page=SimpleNamespace(_deploy_status_label=label, _refresh_deploy_summary=lambda: None),
        _migoto=SimpleNamespace(),
        _update_launch_buttons=lambda: None,
        _ensure_toast=lambda: SimpleNamespace(done=lambda text, **kw: messages.append(text)),
        _show_deployment_root_notice=lambda text, path: messages.append(text),
        _cancel_intake=lambda: None,
        _intake_progress=SimpleNamespace(
            start_batch=lambda *a: None,
            update_progress=lambda *a: None,
            finish_cancelled=lambda *a: messages.append("cancelled"),
            finish=lambda *a, **kw: None,
        ),
    )
    for name in (
        "_apply_migoto_for_current_game",
        "_on_deployment_root_invalid",
        "_on_deployment_root_failed",
        "_suspend_deployment_root",
        "_on_library_discovery_failed",
        "_reconcile_library",
    ):
        method = getattr(MainWindow, name, None)
        if method is not None:
            setattr(host, name, lambda *a, _method=method, **kw: _method(host, *a, **kw))
    if hasattr(host, "_on_deployment_root_invalid"):
        root_watch.invalid.connect(host._on_deployment_root_invalid)
    if hasattr(host, "_on_deployment_root_failed"):
        root_watch.failed.connect(host._on_deployment_root_failed)
    if hasattr(host, "_on_library_discovery_failed"):
        discovery.failed.connect(host._on_library_discovery_failed)
    return host, messages


def _close_refresh_host(host, qtbot):
    host._deployment_root_watch.close()
    host._library_discovery.stop()
    host._library_local_discovery.stop()
    host._library_probe.stop()
    host._deployment_activation.close()
    qtbot.waitUntil(
        lambda: (
            not host._deployment_activation.running
            and host._library_probe.worker is None
            and host._library_discovery.worker is None
            and host._library_local_discovery.worker is None
        )
    )
    host._mod_manager.stop_ini_sync()
    for obj in (
        host._deployment_root_watch,
        host._deployment_activation,
        host._library_probe,
        host._library_discovery,
        host._library_local_discovery,
        host._deploy_state_poll,
        host._deploy_watcher_debounce,
        host._overview_page._deploy_status_label,
    ):
        obj.deleteLater()


def test_main_environment_blocks_missing_root_and_retains_enabled_records(qtbot, temp_data_dir):
    manager, service, mods, old = _environment(temp_data_dir)
    old.rename(old.with_name("Offline move"))
    host, messages = _refresh_host(manager, service)
    try:
        host._apply_migoto_for_current_game()
        assert manager._storage.get_deploy_dir() is None
        assert host._library_probe.paused
        assert host._overview_page._deploy_status_label.text() == "部署目录无效，请重新选择"
        assert manager.get_mod(mods[0].mod_id).is_enabled
        assert not old.exists()
    finally:
        _close_refresh_host(host, qtbot)


def test_main_stop_cancels_refresh_waiting_for_probe_and_allows_another_f5(qtbot, temp_data_dir):
    from src.ui.main_window import MainWindow

    manager, service, mods, old = _environment(temp_data_dir)
    host, messages = _refresh_host(manager, service)
    try:
        host._reconcile_library(manager.current_game_id, trigger="manual", manual=True)
        assert host._library_discovery.pending is not None
        MainWindow._on_panel_stop_requested(host)
        assert host._library_discovery.pending is None
        assert not host._library_probe.pending
        assert "cancelled" in messages
        assert not host._library_probe.closing
        host._reconcile_library(manager.current_game_id, trigger="manual", manual=True)
        assert host._library_discovery.pending is not None
    finally:
        _close_refresh_host(host, qtbot)


def test_enable_entry_resolves_root_move_before_periodic_check(qtbot, temp_data_dir):
    from src.ui.main_window import MainWindow

    manager, service, mods, old = _environment(temp_data_dir)
    host, messages = _refresh_host(manager, service)
    new = old.with_name("Moved before click")
    try:
        old.rename(new)
        assert not MainWindow._require_deploy_dir(host)
        assert service.get_current_game().mods_path == str(new)
        qtbot.waitUntil(lambda: not host._deployment_activation.running)
        assert manager._storage.get_deploy_dir() == new
        assert MainWindow._require_deploy_dir(host)
        assert not old.exists()
    finally:
        _close_refresh_host(host, qtbot)


def test_failed_remap_prevents_enabling_into_the_old_root(qtbot, temp_data_dir, monkeypatch):
    from src.core import game_config_store
    from src.ui.main_window import MainWindow

    manager, service, mods, old = _environment(temp_data_dir)
    host, messages = _refresh_host(manager, service)
    new = old.with_name("Moved but not saved")

    def fail_save(*args, **kwargs):
        raise PermissionError("config file locked")

    try:
        old.rename(new)
        monkeypatch.setattr(game_config_store, "write_json_atomic", fail_save)
        assert not MainWindow._require_deploy_dir(host)
        assert manager._storage.get_deploy_dir() is None
        assert manager.get_mod(mods[0].mod_id).is_enabled
        assert not old.exists()
    finally:
        _close_refresh_host(host, qtbot)
