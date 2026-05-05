import cProfile
import hashlib
import json
import os
import pstats
import threading
import time
import uuid
from pathlib import Path

from src.core.ini_sync import IniSyncManager
from src.core.mod_identity import write_deployment_identity
from src.core.mod_manager import ModManager
from src.core.mod_package import write_container_marker
from src.models.mod import ModStatus


def _activation_library(root, count):
    from src.models.deployment import DeploymentRecord, DeploymentState
    from tests.helpers import make_mod, register_games

    register_games("activation-test")
    manager = ModManager()
    manager.set_game("activation-test")
    deploy_root = root / "deploy"
    manager._storage.set_migoto_mods_dir(deploy_root)
    source_root = manager._get_library_root()
    mods = []
    for index in range(count):
        container = source_root / f"Mod-{index:04}"
        body = container / "body"
        body.mkdir(parents=True)
        did = uuid.uuid4().hex
        write_container_marker(container, "body", "0" * 40, mod_id=did, imported_at="2026-01-01T00:00:00")
        target = deploy_root / f"Mod-{index:04}"
        write_deployment_identity(target, did)
        (target / "mod.ini").write_text("[KeySwap]\nkey = VK_A\n", encoding="utf-8")
        mod = make_mod(
            manager._index,
            container.name,
            mod_id=did,
            body_name="body",
            status=ModStatus.ENABLED,
            deployed_path=str(target),
        )
        assert manager._index.add_mod(mod)
        manager._index.upsert_deployment(DeploymentRecord(did, DeploymentState.ACTIVE, str(target)))
        mods.append(mod)
    return manager, mods


def test_existing_ini_registration_does_not_reindex_other_deployments(temp_data_dir, monkeypatch):
    import src.core.ini_sync as module

    count = 500 if os.environ.get("ACTIVATION_PERF_OUTPUT") else 20
    manager, mods = _activation_library(temp_data_dir, count)
    manager._ini_sync = IniSyncManager(enable_watch=False)
    path_calls = 0
    original_key = module._path_key
    timings = []
    original_register = manager._ini_sync.register_mod

    def path_key(path):
        nonlocal path_calls
        path_calls += 1
        return original_key(path)

    def register(*args, **kwargs):
        start = time.perf_counter()
        result = original_register(*args, **kwargs)
        timings.append((time.perf_counter() - start) * 1000)
        return result

    monkeypatch.setattr(module, "_path_key", path_key)
    monkeypatch.setattr(manager._ini_sync, "register_mod", register)
    profile = cProfile.Profile()
    start = time.perf_counter()
    profile.enable()
    manager._register_existing_deployments()
    profile.disable()
    elapsed = (time.perf_counter() - start) * 1000
    result = {
        "count": count,
        "register_profiled_ms": elapsed,
        "per_item_max_ms": max(timings),
        "per_item_p95_ms": sorted(timings)[int(0.95 * (count - 1))],
        "path_key_calls": path_calls,
        "source_sha256": {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (Path("src/core/mod_manager.py"), Path("src/core/ini_sync.py"))
        },
    }
    target = os.environ.get("ACTIVATION_PERF_OUTPUT")
    if target:
        output = Path(target)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        with output.with_suffix(".profile.txt").open("w", encoding="utf-8") as stream:
            pstats.Stats(profile, stream=stream).sort_stats("cumulative").print_stats(25)
    print(json.dumps(result))
    assert len(manager._ini_sync._pairs) == count
    manager.stop_ini_sync()
    assert path_calls < count * 10, "adding one deployment must not rebuild all prior path keys"


def test_deferred_activation_still_recovers_planned_preset_journal(temp_data_dir):
    from src.core.preset_deployment_plan import PresetDeploymentPlan
    from src.models.deployment import PresetApplyJournal

    manager, mods = _activation_library(temp_data_dir, 1)
    plan = PresetDeploymentPlan("planned-op", "activation-test", "a", "b", ())
    manager._index.save_journal(
        PresetApplyJournal("planned-op", "activation-test", "a", "b", "planned", plan.to_json())
    )
    assert manager.preset_manager.has_pending_apply("activation-test")
    manager.set_deploy_dir(str(manager._storage.get_deploy_dir()), defer_activation=True)
    assert not manager.preset_manager.has_pending_apply("activation-test")
    assert manager._ini_sync is None
    assert manager.get_mod(mods[0].mod_id).is_enabled


def test_incremental_reregistration_replaces_old_paths_only(temp_data_dir):
    from src.core.ini_sync import _path_key

    manager, mods = _activation_library(temp_data_dir, 2)
    sync = IniSyncManager(enable_watch=False)
    manager._ini_sync = sync
    manager._register_existing_deployments()
    first, second = mods
    old = Path(first.deployed_path)
    new = old.with_name("renamed")
    old.rename(new)
    sync.register_mod(first.mod_id, first.path, str(new))
    assert _path_key(old / "mod.ini") not in sync._path_index
    assert len(sync._path_index[_path_key(new / "mod.ini")]) == 1
    second_key = _path_key(Path(second.deployed_path) / "mod.ini")
    assert sync._path_index[second_key][0][0] == second.mod_id
    sync.unregister_mod(first.mod_id)
    assert _path_key(new / "mod.ini") not in sync._path_index
    assert sync._path_index[second_key][0][0] == second.mod_id
    manager.stop_ini_sync()


def test_stop_during_registration_does_not_reinstall_pairs(temp_data_dir, monkeypatch):
    import src.core.ini_sync as module

    manager, mods = _activation_library(temp_data_dir, 1)
    sync = IniSyncManager(enable_watch=False)
    started, release = threading.Event(), threading.Event()
    original = module._hash_file

    def read(path):
        started.set()
        release.wait(2)
        return original(path)

    monkeypatch.setattr(module, "_hash_file", read)
    mod = mods[0]
    result = []
    worker = threading.Thread(target=lambda: result.append(sync.register_mod(mod.mod_id, mod.path, mod.deployed_path)))
    worker.start()
    assert started.wait(2)
    sync.stop()
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert result == [0]
    assert not sync._pairs and not sync._path_index
