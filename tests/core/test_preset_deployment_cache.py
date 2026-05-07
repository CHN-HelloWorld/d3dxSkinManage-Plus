from __future__ import annotations

from tests.helpers import apply_preset
from tests.helpers import registered_games as registered_games


def test_storage_parks_and_restores_deployment(tmp_path):
    from src.core.mod_identity import new_mod_id, write_deployment_identity
    from src.core.mod_storage import ModStorage

    storage = ModStorage()
    root = tmp_path / "Mods"
    storage.set_migoto_mods_dir(root)
    deployed = root / "Skin"
    deployed.mkdir()
    (deployed / "mod.ini").write_text("[Constants]\n", encoding="utf-8")

    identity = new_mod_id()
    write_deployment_identity(deployed, identity)
    parked = storage.park_deployment(deployed, identity)
    assert parked is not None
    assert parked.name == f"DISABLED_{identity[:12]}_Skin"
    assert parked.is_dir()
    assert not deployed.exists()

    restored = storage.restore_parked_deployment(parked, "Skin")
    assert restored == deployed
    assert deployed.is_dir()
    assert not parked.exists()


def test_preset_switch_reuses_parked_copy(temp_data_dir, tmp_path):
    from src.core.config import Config
    from src.core.mod_manager import ModManager

    Config().set("import.format", "zip")
    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色", node_type="character")
    source = tmp_path / "Skin"
    source.mkdir()
    (source / "mod.ini").write_text("[Constants]\n", encoding="utf-8")
    mod = mm.import_mod(source_path=str(source), node_id=char.id)
    assert mod is not None
    root = tmp_path / "deploy"
    mm.set_deploy_dir(str(root))
    assert mm.toggle_mod(mod.mod_id).is_enabled

    enabled = mm.preset_manager.create_preset("game_a", "Enabled", snapshot={char.id: [mod.mod_id]})
    empty = mm.preset_manager.create_preset("game_a", "Empty", snapshot={})
    apply_preset(mm.preset_manager, empty.id)
    disabled = mm.get_mod(mod.mod_id)
    assert disabled is not None and not disabled.is_enabled
    parked_name = "DISABLED_" + mod.mod_id.replace("-", "")[:12] + "_" + mod.folder_name
    assert disabled.deployed_path.endswith(parked_name)
    assert (root / parked_name).is_dir()

    apply_preset(mm.preset_manager, enabled.id)
    restored = mm.get_mod(mod.mod_id)
    assert restored is not None and restored.is_enabled
    assert (root / mod.folder_name).is_dir()
    assert not list(root.glob("DISABLED_*"))
    mm.stop_ini_sync()


def test_manual_enable_reuses_preset_parked_copy(temp_data_dir, tmp_path):
    from src.core.config import Config
    from src.core.mod_manager import ModManager

    Config().set("import.format", "zip")
    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色", node_type="character")
    source = tmp_path / "Skin"
    source.mkdir()
    (source / "mod.ini").write_text("[Constants]\n", encoding="utf-8")
    mod = mm.import_mod(source_path=str(source), node_id=char.id)
    root = tmp_path / "deploy"
    mm.set_deploy_dir(str(root))
    assert mm.toggle_mod(mod.mod_id).is_enabled
    empty = mm.preset_manager.create_preset("game_a", "Empty", snapshot={})
    apply_preset(mm.preset_manager, empty.id)

    assert mm.needs_async_deploy(mod.mod_id) is True
    assert mm.toggle_mod(mod.mod_id).is_enabled
    assert (root / mod.folder_name).is_dir()
    assert not list(root.glob("DISABLED_*"))
    mm.stop_ini_sync()


def test_async_enable_restores_parked_identity_without_owner_inventory_scan(
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path

    from src.ui.deploy_worker import DeployWorker
    from tests.core.test_batch_disable import _make_enabled_mods

    mm, mods, root = _make_enabled_mods(tmp_path, 1)
    mod = mods[0]
    try:
        empty = mm.preset_manager.create_preset("game_a", "Empty", snapshot={})
        apply_preset(mm.preset_manager, empty.id)
        mod = mm.get_mod(mod.mod_id)
        parked = Path(mod.deployed_path)
        original_bytes = (parked / "mod.ini").read_bytes()

        def forbidden(*args, **kwargs):
            raise AssertionError("GUI dispatch/commit must not scan the deployment library")

        with monkeypatch.context() as check:
            check.setattr(mm, "reconcile_deployment_locations", forbidden)
            check.setattr(mm._storage, "scan_deployments", forbidden)
            assert mm.needs_async_deploy(mod.mod_id)
        worker = DeployWorker(
            mod.mod_id,
            mod.path,
            mm._storage,
            deploy_name=mod.folder_name,
            restore_existing=True,
            remembered_path=mod.deployed_path,
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(worker.run).result(timeout=5)
        assert worker.result[0]
        assert not parked.exists()
        target = Path(worker.result[1])
        assert (target / "mod.ini").read_bytes() == original_bytes
        monkeypatch.setattr(mm._storage, "scan_deployments", forbidden)
        enabled = mm.finalize_enable(mod.mod_id, True, deploy_path=str(target))
        assert enabled.is_enabled
        assert enabled.deployed_path == str(target)
        assert mod.mod_id in mm.preset_manager.get_current("game_a").enabled_mod_ids
    finally:
        mm.stop_ini_sync()


def test_async_finalize_rechecks_identity_after_worker_completion(
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    from pathlib import Path

    from src.core.mod_identity import new_mod_id, write_deployment_identity
    from tests.core.test_batch_disable import _make_enabled_mods

    mm, mods, root = _make_enabled_mods(tmp_path, 1)
    try:
        mod = mods[0]
        target = Path(mod.deployed_path)
        from src.models.deployment import DeploymentRecord

        mm._index.upsert_deployment(DeploymentRecord(mod.mod_id))
        # 模拟任务返回路径后发生外部替换。
        write_deployment_identity(target, new_mod_id())

        def forbidden(*args, **kwargs):
            raise AssertionError("target validation must not enumerate other deployments")

        monkeypatch.setattr(mm._storage, "scan_deployments", forbidden)
        assert mm.finalize_enable(mod.mod_id, True, deploy_path=str(target)) is None
        assert not mm.get_mod(mod.mod_id).is_enabled
        assert target.is_dir(), "an unrelated replacement must remain untouched"
    finally:
        mm.stop_ini_sync()
