from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from tests.helpers import apply_preset, scan_and_sync
from tests.helpers import registered_games as registered_games


def test_local_deployment_path_never_enters_shared_metadata(tmp_path):
    from src.core.mod_package import share_meta_from_mod
    from src.models.mod import Mod

    mod = Mod(mod_id="local-only", name="Shared Mod", folder_name="SharedMod")
    local_path = tmp_path / "private-machine" / "Mods" / "SharedMod"
    mod.remember_deployment(local_path)
    mod.info.custom_data["gamebanana"] = {
        "mod_id": 123,
        "file_id": 456,
        "ignored": True,
    }

    shared = share_meta_from_mod(mod)
    serialized = json.dumps(shared, ensure_ascii=False)
    assert str(local_path) not in serialized
    assert "deployed_path" not in serialized
    assert shared["info"]["custom_data"]["gamebanana"] == {
        "mod_id": 123,
        "file_id": 456,
        "last_checked_iso": "",
    }


def _enabled_mod(temp_data_dir: Path, tmp_path: Path, fmt: str):
    from src.core.config import Config
    from src.core.mod_manager import ModManager

    Config().set("import.format", fmt)
    Config().set("import.strict_format", False)

    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="Character", node_type="character")
    source = tmp_path / f"Source_{fmt}"
    source.mkdir()
    (source / "mod.ini").write_text("[Constants]\nx=1\n", encoding="utf-8")
    (source / "mesh.buf").write_bytes(fmt.encode("ascii"))

    mod = mm.import_mod(source_path=str(source), node_id=char.id, name=f"Mod {fmt}")
    assert mod is not None
    deploy_root = tmp_path / "deploy"
    mm.set_deploy_dir(str(deploy_root))
    enabled = mm.toggle_mod(mod.mod_id)
    assert enabled is not None and enabled.is_enabled
    deploy_target = deploy_root / mod.folder_name
    assert deploy_target.exists()
    return mm, char, mod, deploy_target


def _assert_no_preset_reference(mm, mod_id: str) -> None:
    presets = mm.preset_manager.get_all_presets("game_a")
    assert not any(mod_id in ids for preset in presets for ids in preset.character_mods.values())


@pytest.mark.parametrize("fmt", ["zip", "7z"])
def test_external_container_delete_cleans_every_managed_artifact(
    temp_data_dir,
    tmp_path,
    fmt,
):
    mm, char, mod, deploy_target = _enabled_mod(temp_data_dir, tmp_path, fmt)
    try:
        # 默认预设由 toggle 写入；再造一个自定义预设，验证是“全部预设”级联。
        mm.preset_manager.create_preset("game_a", "Custom", snapshot={char.id: [mod.mod_id]})

        data_root = temp_data_dir / "data" / mm._res_dir("game_a")
        preview_cache = data_root / "preview_cache" / mod.mod_id
        preview_cache.mkdir(parents=True, exist_ok=True)
        (preview_cache / "cached.png").write_bytes(b"cache")

        events: list[str] = []
        mm.add_event_listener(lambda event, **_payload: events.append(event))

        body = Path(mod.path)
        container = body.parent
        shutil.rmtree(container)

        assert mm.reconcile_missing_sources("game_a") == 1
        assert mm.get_mod(mod.mod_id) is None
        assert not deploy_target.exists()
        assert not preview_cache.exists()
        assert not mm.has_pending_source_cleanup()
        assert mod.mod_id not in mm._ini_sync._pairs
        assert mm._ini_sync._watched == set()
        _assert_no_preset_reference(mm, mod.mod_id)
        assert "mod_deleted" in events
    finally:
        mm.stop_ini_sync()


def test_missing_archive_source_uses_stable_deploy_name_for_all_formats(tmp_path):
    from src.core.mod_identity import write_deployment_identity
    from src.core.mod_storage import ModStorage

    storage = ModStorage()
    deploy_root = tmp_path / "deploy"
    storage.set_migoto_mods_dir(deploy_root)

    for suffix in (".zip", ".7z", ".rar"):
        deploy_target = deploy_root / "ArchiveMod"
        deploy_target.mkdir(parents=True, exist_ok=True)
        (deploy_target / "mod.ini").write_text("[Constants]\n", encoding="utf-8")
        deployment_id = uuid4().hex
        write_deployment_identity(deploy_target, deployment_id)
        missing_source = tmp_path / "library" / f"ArchiveMod{suffix}"

        assert storage.undeploy_mod(missing_source, capture_changes=False, mod_id=deployment_id) is True
        assert not deploy_target.exists()


def test_cleanup_failure_keeps_index_as_durable_retry_record(
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    mm, _char, mod, deploy_target = _enabled_mod(temp_data_dir, tmp_path, "zip")
    try:
        shutil.rmtree(Path(mod.path).parent)
        real_undeploy = mm._storage.undeploy_mod
        attempts = 0

        def _flaky_undeploy(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return False
            return real_undeploy(*args, **kwargs)

        monkeypatch.setattr(mm._storage, "undeploy_mod", _flaky_undeploy)

        assert mm.reconcile_missing_sources("game_a") == 0
        assert mm.get_mod(mod.mod_id) is not None
        assert deploy_target.exists()
        assert mm.has_pending_source_cleanup()

        assert mm.reconcile_missing_sources("game_a") == 1
        assert mm.get_mod(mod.mod_id) is None
        assert not deploy_target.exists()
        assert not mm.has_pending_source_cleanup()
    finally:
        mm.stop_ini_sync()


def test_locked_preview_cache_does_not_keep_ghost_mod_record(
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    mm, _char, mod, deploy_target = _enabled_mod(temp_data_dir, tmp_path, "zip")
    try:
        data_root = temp_data_dir / "data" / mm._res_dir("game_a")
        preview_cache = data_root / "preview_cache" / mod.mod_id
        preview_cache.mkdir(parents=True, exist_ok=True)
        (preview_cache / "locked.png").write_bytes(b"cache")

        real_remove = mm._remove_managed_path
        cache_locked = True

        def _remove_unless_cache_locked(path):
            if cache_locked and Path(path) == preview_cache:
                return False
            return real_remove(path)

        monkeypatch.setattr(mm, "_remove_managed_path", _remove_unless_cache_locked)

        assert mm.delete_mod(mod.mod_id) is True
        assert mm.get_mod(mod.mod_id) is None
        assert not Path(mod.path).exists()
        assert not deploy_target.exists()
        assert preview_cache.exists()

        cache_locked = False
        assert mm.reconcile_missing_sources("game_a") == 0
        assert not preview_cache.exists()
        assert not mm.has_pending_source_cleanup()
    finally:
        mm.stop_ini_sync()


def test_deleted_body_is_not_relocated_to_empty_container(temp_data_dir, tmp_path):
    mm, _char, mod, deploy_target = _enabled_mod(temp_data_dir, tmp_path, "zip")
    try:
        body = Path(mod.path)
        container = body.parent
        body.unlink()  # 本体是压缩档
        assert container.exists()  # 仅剩 marker/meta 空壳

        assert mm.reconcile_missing_sources("game_a") == 1
        assert mm.get_mod(mod.mod_id) is None
        assert not container.exists()
        assert not deploy_target.exists()
    finally:
        mm.stop_ini_sync()


def test_external_body_rename_preserves_identity_and_repairs_marker(
    temp_data_dir,
    tmp_path,
):
    mm, _char, mod, deploy_target = _enabled_mod(temp_data_dir, tmp_path, "zip")
    try:
        body = Path(mod.path)
        container = body.parent
        renamed_body = body.with_name("RenamedBody" + body.suffix)
        body.rename(renamed_body)

        imported_at = mod.imported_at
        assert mm.reconcile_missing_sources("game_a") == 0
        assert scan_and_sync(mm) == 0

        recovered = mm.get_mod(mod.mod_id)
        assert recovered is not None and recovered.is_enabled
        assert recovered.imported_at == imported_at
        assert Path(recovered.path) == renamed_body
        assert container.exists()
        assert renamed_body.is_file()
        assert deploy_target.exists()
        from src.utils.constants import MOD_CONTAINER_MARKER

        marker = json.loads((container / MOD_CONTAINER_MARKER).read_text(encoding="utf-8"))
        assert marker["body"] == renamed_body.name
        assert marker["mod_id"] == mod.mod_id
    finally:
        mm.stop_ini_sync()


def test_cleanup_survives_application_restart(temp_data_dir, tmp_path):
    from src.core.mod_manager import ModManager

    mm, _char, mod, deploy_target = _enabled_mod(temp_data_dir, tmp_path, "zip")
    shutil.rmtree(Path(mod.path).parent)
    mm.stop_ini_sync()
    mm._index.close()

    # 模拟软件关闭后重新创建核心对象；缺失的源记录仍在 SQLite 中，成为耐久清理凭据。
    ModManager._instance = None
    restarted = ModManager()
    restarted.set_game("game_a")
    # 即使当前配置已不再提供部署根，也必须依靠上次成功部署时持久化的完整路径清理。
    restarted.set_deploy_dir("")
    try:
        assert restarted.reconcile_missing_sources("game_a") == 1
        assert restarted.get_mod(mod.mod_id) is None
        assert not deploy_target.exists()
        _assert_no_preset_reference(restarted, mod.mod_id)
    finally:
        restarted.stop_ini_sync()


def test_preset_disable_parks_at_recorded_path_after_deploy_root_changes(
    temp_data_dir,
    tmp_path,
):
    mm, _char, mod, old_target = _enabled_mod(temp_data_dir, tmp_path, "zip")
    try:
        # 只切换配置、不迁移旧副本：禁用仍必须按部署成功时记录的旧路径停放。
        mm.set_deploy_dir(str(tmp_path / "new-deploy-root"))
        empty = mm.preset_manager.create_preset("game_a", "Empty", snapshot={})
        apply_preset(mm.preset_manager, empty.id)

        persisted = mm.get_mod(mod.mod_id)
        assert persisted is not None and not persisted.is_enabled
        assert Path(persisted.deployed_path).parent == old_target.parent
        assert Path(persisted.deployed_path).name.startswith("DISABLED_")
        assert Path(persisted.deployed_path).is_dir()
        assert not old_target.exists()
    finally:
        mm.stop_ini_sync()
