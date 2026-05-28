from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer

from src.core import ini_sync
from src.core.mod_manager import ModManager
from tests.core.test_deployment_activation import _activation_library


def _controller(manager, qtbot):
    assert importlib.util.find_spec("src.ui.deployment_activation") is not None, (
        "deployment activation needs a cancellable filesystem worker"
    )
    from src.ui.deployment_activation import DeploymentActivationController

    controller = DeploymentActivationController(manager)
    return controller


def test_activation_registers_in_worker_and_publishes_on_owner(qtbot, temp_data_dir, monkeypatch):
    manager, mods = _activation_library(temp_data_dir, 3)
    owner = threading.get_ident()
    reads = []
    original = ini_sync._hash_file

    def read(path):
        reads.append(threading.get_ident())
        return original(path)

    monkeypatch.setattr(ini_sync, "_hash_file", read)
    controller = _controller(manager, qtbot)
    completed = []
    controller.settled.connect(lambda game_id, result: completed.append((threading.get_ident(), result)))
    controller.start(str(manager._storage.get_deploy_dir()))
    qtbot.waitUntil(lambda: bool(completed), timeout=5000)
    assert completed[0][0] == owner
    assert reads and all(thread != owner for thread in reads)
    assert set(manager._ini_sync._pairs) == {mod.mod_id for mod in mods}
    assert manager._ini_sync_refs[mods[0].mod_id] == (mods[0].mod_id,)
    controller.close()
    qtbot.waitUntil(lambda: not controller.running, timeout=5000)
    manager.stop_ini_sync()


def test_repeated_same_environment_preserves_pending_and_ready_activation(qtbot, temp_data_dir):
    manager, mods = _activation_library(temp_data_dir, 1)
    manager.preset_manager.ensure_default(manager.current_game_id)
    controller = _controller(manager, qtbot)
    root = str(manager._storage.get_deploy_dir())
    assert controller.start(root)
    pending = controller.worker
    manager.preset_manager.ensure_default(manager.current_game_id)
    assert controller.start(root)
    assert controller.worker is pending
    qtbot.waitUntil(lambda: not controller.running, timeout=5000)
    sync = manager._ini_sync
    assert controller.is_current(root)
    assert controller.start(root)
    assert controller.worker is None and manager._ini_sync is sync
    assert len(controller.metrics["capture_ms"]) == 1
    changed = manager.get_mod(mods[0].mod_id)
    changed.name = "Changed"
    manager._index.add_mod(changed)
    assert controller.is_current(root)
    manager.stop_ini_sync()
    assert not controller.is_current(root)
    controller.close()
    manager.stop_ini_sync()


def test_activation_projection_omits_previews_and_keeps_source_paths(qtbot, temp_data_dir, monkeypatch):
    manager, mods = _activation_library(temp_data_dir, 2)
    expected = manager._index.get_library_rows(manager.current_game_id)

    def unexpected(*args):
        raise AssertionError("activation must not decode the full preview projection")

    monkeypatch.setattr(manager._index, "get_library_rows", unexpected)
    from src.ui.deployment_activation import capture_deployment_activation

    actual = capture_deployment_activation(manager).rows
    assert [(r.mod_id, r.path, r.deployed_path, r.enabled) for r in actual] == [
        (r.mod_id, r.path, r.deployed_path, r.enabled) for r in expected
    ]
    assert all(row.preview_images == () for row in actual)


def test_game_change_rejects_old_activation_and_stops_its_watchers(qtbot, temp_data_dir, monkeypatch):
    manager, mods = _activation_library(temp_data_dir, 2)
    started, release = threading.Event(), threading.Event()
    original = ini_sync._hash_file

    def read(path):
        started.set()
        release.wait(2)
        return original(path)

    monkeypatch.setattr(ini_sync, "_hash_file", read)
    controller = _controller(manager, qtbot)
    settled = []
    controller.settled.connect(lambda *args: settled.append(args))
    controller.start(str(manager._storage.get_deploy_dir()))
    old_worker = controller.worker
    qtbot.waitUntil(started.is_set, timeout=2000)
    manager.set_game("new-game")
    controller.start("")
    release.set()
    qtbot.waitUntil(lambda: not controller.running, timeout=5000)
    assert all(args[0] == "new-game" for args in settled)
    assert manager._ini_sync is None
    assert old_worker.result is None or old_worker.result.sync._stopped
    controller.close()


def test_index_mutation_rejects_prepared_activation(qtbot, temp_data_dir, monkeypatch):
    manager, mods = _activation_library(temp_data_dir, 1)
    started, release = threading.Event(), threading.Event()
    original = ini_sync._hash_file

    def read(path):
        started.set()
        release.wait(2)
        return original(path)

    monkeypatch.setattr(ini_sync, "_hash_file", read)
    controller = _controller(manager, qtbot)
    settled = []
    controller.settled.connect(lambda *args: settled.append(args))
    controller.start(str(manager._storage.get_deploy_dir()))
    worker = controller.worker
    qtbot.waitUntil(started.is_set, timeout=2000)
    changed = manager.get_mod(mods[0].mod_id)
    changed.name = "Changed during activation"
    assert manager._index.add_mod(changed)
    release.set()
    qtbot.waitUntil(lambda: not controller.running, timeout=5000)
    assert not settled
    assert manager._ini_sync is None
    assert worker.result.sync._stopped
    controller.close()


def test_close_cancels_without_waiting_for_file_read(qtbot, temp_data_dir, monkeypatch):
    manager, mods = _activation_library(temp_data_dir, 1)
    started, release = threading.Event(), threading.Event()
    original = ini_sync._hash_file

    def read(path):
        started.set()
        release.wait(2)
        return original(path)

    monkeypatch.setattr(ini_sync, "_hash_file", read)
    controller = _controller(manager, qtbot)
    settled = []
    controller.settled.connect(lambda *args: settled.append(args))
    controller.start(str(manager._storage.get_deploy_dir()))
    qtbot.waitUntil(started.is_set, timeout=2000)
    start = time.perf_counter()
    controller.close()
    elapsed = time.perf_counter() - start
    release.set()
    qtbot.waitUntil(lambda: not controller.running, timeout=5000)
    assert elapsed < 0.05
    assert not settled and manager._ini_sync is None


def test_destroyed_controller_cannot_receive_worker_completion(qtbot, temp_data_dir, monkeypatch):
    import shiboken6
    from PySide6.QtCore import QCoreApplication, QEvent

    manager, _mods = _activation_library(temp_data_dir, 1)
    started, release = threading.Event(), threading.Event()
    original = ini_sync._hash_file

    def read(path):
        started.set()
        release.wait(3)
        return original(path)

    monkeypatch.setattr(ini_sync, "_hash_file", read)
    controller = _controller(manager, qtbot)
    assert controller.start(str(manager._storage.get_deploy_dir()))
    worker = controller.worker
    try:
        qtbot.waitUntil(started.is_set, timeout=2000)
        controller.close()
        shiboken6.delete(controller)
    finally:
        release.set()
        assert worker.wait(5000)
    qtbot.wait(20)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert manager._ini_sync is None
    assert not shiboken6.isValid(worker)


def test_corrupt_deployment_identity_preserves_index(qtbot, temp_data_dir):
    from pathlib import Path

    from src.utils.constants import MOD_CONTAINER_MARKER

    manager, mods = _activation_library(temp_data_dir, 1)
    (Path(mods[0].deployed_path) / MOD_CONTAINER_MARKER).write_text("invalid", encoding="utf-8")
    controller = _controller(manager, qtbot)
    failed = []
    controller.failed.connect(lambda *args: failed.append(args))
    controller.start(str(manager._storage.get_deploy_dir()))
    qtbot.waitUntil(lambda: not controller.running, timeout=5000)
    assert failed and failed[0][0] == "activation-test"
    assert manager.get_mod(mods[0].mod_id).is_enabled
    assert manager.get_mod(mods[0].mod_id).deployed_path == mods[0].deployed_path
    assert manager._ini_sync is None
    controller.close()


def test_empty_activation_does_not_create_a_library(qtbot, temp_data_dir):
    manager = ModManager()
    manager.set_game("")
    controller = _controller(manager, qtbot)
    settled = []
    controller.settled.connect(lambda *args: settled.append(args))
    controller.start("")
    qtbot.waitUntil(lambda: not controller.running, timeout=3000)
    assert settled
    assert not (temp_data_dir / "data" / "mods").exists()
    assert manager._ini_sync is None
    controller.close()


def test_one_invalid_source_keeps_other_ini_registrations(qtbot, temp_data_dir):
    from src.utils.constants import MOD_CONTAINER_MARKER

    manager, mods = _activation_library(temp_data_dir, 2)
    (Path(mods[0].path).parent / MOD_CONTAINER_MARKER).write_text("invalid", encoding="utf-8")
    controller = _controller(manager, qtbot)
    settled = []
    controller.settled.connect(lambda game_id, result: settled.append(result))
    controller.start(str(manager._storage.get_deploy_dir()))
    qtbot.waitUntil(lambda: not controller.running, timeout=5000)
    assert settled and settled[0].errors
    assert mods[1].mod_id in manager._ini_sync._pairs
    assert manager.get_mod(mods[0].mod_id).is_enabled
    controller.close()
    manager.stop_ini_sync()


@pytest.mark.skipif(not os.environ.get("ACTIVATION_ASYNC_OUTPUT"), reason="opt-in 500-deployment timing")
def test_500_deployments_activate_without_owner_stall(qtbot, temp_data_dir):
    manager, mods = _activation_library(temp_data_dir, 500)
    controller = _controller(manager, qtbot)
    settled, heartbeats = [], []
    controller.settled.connect(lambda *args: settled.append(args))
    timer = QTimer()
    timer.setInterval(16)
    last = time.perf_counter()

    def beat():
        nonlocal last
        now = time.perf_counter()
        heartbeats.append(max(0, (now - last) * 1000 - 16))
        last = now

    timer.timeout.connect(beat)
    timer.start()
    controller.start(str(manager._storage.get_deploy_dir()))
    qtbot.waitUntil(lambda: bool(settled), timeout=15000)
    qtbot.wait(35)
    timer.stop()
    metrics = dict(controller.metrics)
    metrics.update(
        count=500,
        profiled=False,
        heartbeat_samples=len(heartbeats),
        heartbeat_max_ms=max(heartbeats),
        heartbeat_p95_ms=sorted(heartbeats)[int(0.95 * (len(heartbeats) - 1))],
        source_sha256={
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                Path("src/core/mod_manager.py"),
                Path("src/core/ini_sync.py"),
                Path("src/ui/deployment_activation.py"),
            )
        },
    )
    output = Path(os.environ["ACTIVATION_ASYNC_OUTPUT"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    controller.close()
    qtbot.waitUntil(lambda: not controller.running, timeout=5000)
    assert len(manager._ini_sync._pairs) == 500
    manager.stop_ini_sync()
    assert max(metrics["capture_ms"]) < 200
    assert max(metrics["publish_ms"]) < 200
    assert metrics["heartbeat_max_ms"] < 200
    assert metrics["heartbeat_p95_ms"] < 50
