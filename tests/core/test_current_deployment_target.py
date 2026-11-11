"""验证当前填写的部署目录优先于旧设备、停放副本及预设中的历史位置。"""

from dataclasses import replace
from pathlib import Path

import pytest

from src.core.preset_deployment_plan import build_preset_deployment_plan
from src.models.deployment import DeploymentRecord, DeploymentState
from tests.core.test_enable_without_runtime_namespace import _prepare
from tests.core.test_named_variant_manager import enable, save
from tests.core.test_variant_preset_switch import _KEY, _user_keys, _write_user_ini
from tests.core.test_variant_preset_switch import env as env
from tests.helpers import apply_preset


def _snapshot(root):
    """记录旧环境全部文件的内容，验证新部署没有恢复、停放或修改旧副本。"""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("parked", [False, True])
@pytest.mark.parametrize("operation", ["foreground", "background", "preset"])
def test_current_directory_is_used_while_previous_deployment_still_exists(env, parked, operation):
    manager, mod, _ = enable(env)
    variant = save(env, value="4")
    preset = manager.preset_manager.create_preset("g1", "Saved selection", snapshot={env["char"].id: [mod.mod_id]})
    preset.mod_variants[mod.mod_id] = variant.variant_id
    manager.preset_manager._save()
    if parked:
        assert manager.toggle_mod(mod.mod_id) is not None
        assert Path(manager.get_mod(mod.mod_id).deployed_path).name.startswith("DISABLED_")
    old_files = _snapshot(env["migoto"])
    current = env["migoto"].parent / "current-loader"
    deployment = current / "Mods"
    deployment.mkdir(parents=True)
    (current / "d3dx.ini").write_bytes((env["migoto"] / "d3dx.ini").read_bytes())
    runtime = current / "d3dx_user.ini"
    _write_user_ini(runtime, "9")
    game = manager._game_service.get_game("g1")
    manager._game_service.save_game(replace(game, mods_path=str(deployment)))

    manager.set_deploy_dir(str(deployment))

    assert not manager.get_mod(mod.mod_id).is_enabled
    assert not manager.get_mod(mod.mod_id).deployed_path
    if operation == "preset":
        apply_preset(manager.preset_manager, preset.id)
        enabled = manager.get_mod(mod.mod_id)
    elif operation == "background":
        prepared = _prepare(env).run()
        enabled = manager.finalize_enable(
            mod.mod_id,
            True,
            deploy_path=prepared.deploy_path,
            source_receipt=prepared.source_receipt,
            preparation=prepared,
        )
    else:
        enabled = manager.toggle_mod(mod.mod_id)

    assert enabled is not None and enabled.is_enabled
    assert Path(enabled.deployed_path).parent == deployment
    assert _user_keys(runtime)[_KEY] == "4"
    assert _snapshot(env["migoto"]) == old_files


def test_parked_copy_outside_current_directory_is_not_restored(env):
    manager, mod = env["mm"], env["mod"]
    storage = manager._storage
    parked = storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name, active=False)
    before = _snapshot(env["migoto"])
    current = env["migoto"].parent / "selected-mods"
    storage.set_migoto_mods_dir(current)

    assert storage.restore_parked_deployment(parked, mod.folder_name) is None
    assert _snapshot(env["migoto"]) == before
    assert not list(current.iterdir())


@pytest.mark.parametrize("state", [DeploymentState.ACTIVE, DeploymentState.DISABLED])
def test_preset_plan_uses_current_root_instead_of_historical_record(env, state):
    mod = env["mod"]
    current = env["migoto"].parent / "selected-mods"
    old_path = (
        env["migoto"]
        / "Mods"
        / (mod.folder_name if state == DeploymentState.ACTIVE else f"DISABLED_{mod.mod_id[:12]}_{mod.folder_name}")
    )
    plan = build_preset_deployment_plan(
        game_id="g1",
        from_preset_id="old",
        to_preset_id="selected",
        mods=[mod],
        target_mod_ids={mod.mod_id},
        deployments=[DeploymentRecord(mod.mod_id, state, str(old_path))],
        deploy_root=current,
    )

    assert len(plan.actions) == 1
    assert plan.actions[0].action == "materialize_active"
    assert Path(plan.actions[0].active_path).parent == current
    assert Path(plan.actions[0].disabled_path).parent == current


def test_deleted_configured_directory_can_be_recreated_during_deployment(env):
    storage, mod = env["mm"]._storage, env["mod"]
    current = storage.get_deploy_dir()
    current.rmdir()

    deployed = storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name)

    assert current.is_dir()
    assert deployed.parent == current
    assert (deployed / "merged.ini").is_file()
