from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from src.core.mod_manager import ModManager
from src.core.mod_update_transaction import cleanup_update, commit_update, make_update_plan
from src.core.mod_update_worker import prepare_update
from src.models.deployment import DeploymentState
from src.services.gamebanana.models import GbInstallMetadata
from tests.helpers import apply_preset, register_games, reset_game_service
from tests.helpers import registered_games as registered_games


def _archive(path: Path, body: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Skin/mod.ini", "[Constants]\nx=1\n")
        archive.writestr("Skin/body.buf", body)
    return path


def _metadata(version: str, file_id: int) -> GbInstallMetadata:
    return GbInstallMetadata(
        mod_id=123,
        file_id=file_id,
        file_md5=version,
        file_ts=file_id,
        page_url="https://gamebanana.com/mods/123",
        version=version,
        submitter_id=1,
        submitter_name="Author",
        last_checked_iso="",
    )


@pytest.fixture
def parked_update(qapp, temp_data_dir):
    register_games("game-a")
    manager = ModManager()
    try:
        manager._config.set("import.format", "zip")
        manager.set_game("game-a")
        manager.set_deploy_dir(str(temp_data_dir / "Mods"))
        character = manager.add_character(name="Character", node_type="character")
        old = _archive(temp_data_dir / "old.zip", b"version one body")
        new = _archive(temp_data_dir / "new.zip", b"version two body")
        mod = manager.import_mod(str(old), character.id, gb_meta=_metadata("v1", 10))
        assert mod is not None
        assert manager.toggle_mod(mod.mod_id).is_enabled
        enabled_preset = manager.preset_manager.create_preset(
            "game-a",
            "Keep enabled",
            snapshot={character.id: [mod.mod_id]},
        )
        # 其他预设仍引用该 Mod，真实禁用操作应停放其部署。
        parked_mod = manager.toggle_mod(mod.mod_id)
        assert not parked_mod.is_enabled
        parked = Path(parked_mod.deployed_path)
        assert parked.name.startswith("DISABLED_")
        assert (parked / "body.buf").read_bytes() == b"version one body"
        manager.stop_ini_sync()

        plan = make_update_plan(manager, mod.mod_id, new, _metadata("v2", 11))
        try:
            prepared = prepare_update(plan, lambda event: None)
            updated = commit_update(manager, plan, prepared)
        finally:
            cleanup_update(plan)
        assert not updated.is_enabled
        assert updated.info.version == "v2"
        assert (parked / "body.buf").read_bytes() == b"version two body"
        yield manager, updated, parked, enabled_preset
    finally:
        manager.stop_ini_sync()
        reset_game_service()


@pytest.mark.parametrize("enable_via", ["toggle", "preset"])
def test_updated_parked_deployment_enables_the_new_body(parked_update, enable_via):
    manager, updated, parked, enabled_preset = parked_update
    assert updated.info.version == "v2"
    assert not updated.is_enabled
    with zipfile.ZipFile(updated.path) as archive:
        assert archive.read("Skin/body.buf") == b"version two body"
    deployment_before_enable = manager._index.get_deployment(updated.mod_id)

    if enable_via == "toggle":
        assert manager.toggle_mod(updated.mod_id).is_enabled
    else:
        assert apply_preset(manager.preset_manager, enabled_preset.id) == [updated.mod_id]
    enabled = manager.get_mod(updated.mod_id)
    assert enabled.is_enabled
    assert (Path(enabled.deployed_path) / "body.buf").read_bytes() == b"version two body"
    assert deployment_before_enable.state == DeploymentState.DISABLED
    assert deployment_before_enable.actual_path == str(parked)
    assert not parked.exists()
    assert updated.mod_id in manager.preset_manager.get_preset(enabled_preset.id).enabled_mod_ids
