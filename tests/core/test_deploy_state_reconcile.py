from __future__ import annotations

import shutil

from tests.helpers import registered_games as registered_games


def test_external_deploy_delete_restores_disabled_state(temp_data_dir, tmp_path):
    from src.core.config import Config
    from src.core.mod_manager import ModManager

    Config().set("import.format", "zip")
    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色", node_type="character")
    source = tmp_path / "Skin"
    source.mkdir()
    (source / "mod.ini").write_text("[Constants]\nx=1\n", encoding="utf-8")
    mod = mm.import_mod(source_path=str(source), node_id=char.id)
    assert mod is not None

    deploy_root = tmp_path / "deploy"
    mm.set_deploy_dir(str(deploy_root))
    enabled = mm.toggle_mod(mod.mod_id)
    assert enabled is not None and enabled.is_enabled
    deploy_target = deploy_root / mod.folder_name
    assert deploy_target.is_dir()

    shutil.rmtree(deploy_target)
    changed = mm.reconcile_missing_deployments("game_a")

    assert changed == [mod.mod_id]
    persisted = mm.get_mod(mod.mod_id)
    assert persisted is not None and not persisted.is_enabled
    current = mm.preset_manager.get_current("game_a")
    assert mod.mod_id in current.character_mods.get(char.id, [])
    assert not persisted.deployed_path
    mm.stop_ini_sync()


def test_enable_refused_and_reconcile_noop_without_deploy_dir(temp_data_dir, tmp_path):
    """未配置部署目录时拒绝启用，对账不产生部署。"""
    from src.core.mod_manager import ModManager

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色", node_type="character")
    source = tmp_path / "Skin"
    source.mkdir()
    (source / "mod.ini").write_text("[Constants]\nx=1\n", encoding="utf-8")
    mod = mm.import_mod(source_path=str(source), node_id=char.id)
    assert mod is not None
    assert mm.has_deploy_dir() is False

    assert mm.toggle_mod(mod.mod_id) is None, "没配部署目录时不得启用"
    assert mm.get_mod(mod.mod_id).is_enabled is False

    assert mm.reconcile_missing_deployments("game_a") == []
    assert mm.get_mod(mod.mod_id).is_enabled is False
