"""验证含多个受管 Mod 容器的归档展开本体，并保留各自的 INI 覆盖内容。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.core.mod_identity import write_deployment_identity
from src.core.mod_package import (
    body_ini_rels,
    inspect_effective_inis,
    read_body_ini_text,
    resolve_ini_overrides_dir,
    write_container_marker,
    write_override,
)
from src.core.mod_storage import ModStorage
from src.core.variant_operation import IniInventoryGuard, VariantRecoveryError
from src.models.mod import Mod
from src.services.archive import ArchiveService
from src.ui.error_messages import user_error_message
from tests.helpers import register_games, reset_game_service

MOD_ID = "abcdefabcdef4def8defabcdefabcdef"
ORIGINAL = "[Constants]\nvalue = 1\n"
EDITED = "[Constants]\nvalue = 2\n"


def _bundle(tmp_path: Path) -> Path:
    """将两个归档本体及覆盖配置按管理器容器结构封装为外层归档。"""
    library = tmp_path / "library"
    for name in ("First", "Second"):
        container = library / name
        container.mkdir(parents=True)
        body = container / f"{name}.zip"
        with zipfile.ZipFile(body, "w") as archive:
            archive.writestr("skin/mod.ini", ORIGINAL)
            archive.writestr("skin/mesh.bin", b"mesh data")
        assert write_container_marker(
            container, body.name, Mod.compute_sha1(body), mod_id=MOD_ID, imported_at="2026-11-07T00:00:00"
        )
        assert write_override(body, "mod.ini", EDITED)
    source = tmp_path / "source"
    source.mkdir()
    body = source / "bundle.zip"
    with zipfile.ZipFile(body, "w") as archive:
        for member in library.rglob("*"):
            if member.is_file():
                archive.write(member, "Collection/" + member.relative_to(library).as_posix())
    assert write_container_marker(
        source, body.name, Mod.compute_sha1(body), mod_id=MOD_ID, imported_at="2026-11-07T00:00:00"
    )
    overrides = resolve_ini_overrides_dir(body)
    overrides.mkdir(parents=True)
    (overrides / ".seeded").write_bytes(b"1")
    return body


def test_nested_container_source_reads_real_inis_and_preserves_embedded_overrides(tmp_path):
    body = _bundle(tmp_path)
    before = body.read_bytes()
    assert dict(inspect_effective_inis(body).read()) == {"First/mod.ini": EDITED, "Second/mod.ini": EDITED}
    assert set(body_ini_rels(body)) == {"First/mod.ini", "Second/mod.ini"}
    assert read_body_ini_text(body, "First/mod.ini") == EDITED
    assert body.read_bytes() == before


@pytest.mark.parametrize("old_members", [[], ["First/mod.ini"]])
def test_old_json_seed_is_revalidated_against_expanded_archive_members(tmp_path, old_members):
    """旧布局的完整标记也必须重新核对内层成员，不能继续隐藏遗漏的配置。"""
    from src.core.mod_package import _ini_stat_token, write_override

    body = _bundle(tmp_path)
    overrides = resolve_ini_overrides_dir(body)
    for member in old_members:
        assert write_override(body, member, EDITED)
    (overrides / ".seeded").write_bytes(
        json.dumps({"schema": 1, "body_version": _ini_stat_token(body.stat()), "inis": old_members}).encode()
    )
    inventory = inspect_effective_inis(body)
    assert inventory.seed_version is None
    assert dict(inventory.read()) == {"First/mod.ini": EDITED, "Second/mod.ini": EDITED}


def test_nested_container_deployment_expands_assets_and_matches_source_inventory(tmp_path):
    body = _bundle(tmp_path)
    before = body.read_bytes()
    storage = ModStorage()
    storage.set_migoto_mods_dir(tmp_path / "deploy")
    deployed = storage.materialize_deployment(body, MOD_ID, "Collection")
    inis = dict(inspect_effective_inis(body).read())
    IniInventoryGuard.deployed(deployed, inis).verify()
    for name in ("First", "Second"):
        assert (deployed / name / "mod.ini").read_bytes() == EDITED.encode()
        assert (deployed / name / "mesh.bin").read_bytes() == b"mesh data"
        assert not (deployed / name / f"{name}.zip").exists()
        assert not (deployed / name / ".d3dxmod").exists()
        assert not (deployed / name / ".d3dxmeta").exists()
    assert body.read_bytes() == before


def test_legacy_parked_nested_deployment_is_upgraded_without_losing_local_edits(tmp_path):
    body = _bundle(tmp_path)
    storage = ModStorage()
    storage.set_migoto_mods_dir(tmp_path / "deploy")
    parked = storage.get_deploy_dir() / storage._parked_deploy_name(MOD_ID, "Collection")
    extracted = ArchiveService().extract(body, parked)
    storage._flatten_staged_deployment(extracted, parked)
    write_deployment_identity(parked, MOD_ID, Mod.compute_sha1(body))
    local = parked / "First/.d3dxmeta/ini_overrides/mod.ini"
    local.write_bytes(b"[Constants]\nvalue = 3\n")
    storage.prepare_nested_deployment(parked, MOD_ID)
    restored = storage.restore_parked_deployment(parked, "Collection")
    assert restored is not None
    assert (restored / "First/mod.ini").read_bytes() == b"[Constants]\nvalue = 3\n"
    assert (restored / "Second/mod.ini").read_bytes() == EDITED.encode()
    assert not any(path.name.endswith(".backup") for path in restored.parent.iterdir())


def test_failed_nested_upgrade_restores_original_parked_directory(tmp_path, monkeypatch):
    """模拟发布失败，原始停放目录及其中的配置仍须完整保留。"""
    body = _bundle(tmp_path)
    storage = ModStorage()
    storage.set_migoto_mods_dir(tmp_path / "deploy")
    parked = storage.get_deploy_dir() / storage._parked_deploy_name(MOD_ID, "Collection")
    extracted = ArchiveService().extract(body, parked)
    storage._flatten_staged_deployment(extracted, parked)
    write_deployment_identity(parked, MOD_ID, Mod.compute_sha1(body))
    original = (parked / "First/First.zip").read_bytes()
    rename = storage._rename_staged_deployment
    calls = []

    def blocked(source, target):
        """仅拒绝新布局发布，备份和回滚仍使用真实原子改名。"""
        calls.append((source, target))
        if len(calls) == 2:
            raise PermissionError("publish failed")
        rename(source, target)

    monkeypatch.setattr(storage, "_rename_staged_deployment", blocked)
    with pytest.raises(PermissionError):
        storage.prepare_nested_deployment(parked, MOD_ID)
    assert (parked / "First/First.zip").read_bytes() == original
    assert (parked / "First/.d3dxmeta/ini_overrides/mod.ini").read_bytes() == EDITED.encode()
    assert list(parked.parent.iterdir()) == [parked]


def test_archive_without_container_marker_is_not_recursively_unpacked(tmp_path):
    """普通附件压缩包不代表受管 Mod，须按作者原样保留。"""
    container = tmp_path / "source"
    container.mkdir()
    body = container / "plain.zip"
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("mod.ini", ORIGINAL)
        archive.writestr("assets.zip", b"author attachment")
    assert write_container_marker(
        container, body.name, Mod.compute_sha1(body), mod_id=MOD_ID, imported_at="2026-11-07T00:00:00"
    )
    storage = ModStorage()
    storage.set_migoto_mods_dir(tmp_path / "deploy")
    deployed = storage.materialize_deployment(body, MOD_ID, "Plain")
    assert (deployed / "assets.zip").read_bytes() == b"author attachment"


@pytest.mark.parametrize("folder", [False, True])
def test_new_import_expands_nested_containers_only_in_its_working_copy(tmp_path, folder):
    """文件夹及归档导入转换使用相同展开规则，绝不改动外部输入。"""
    from src.core.import_format import prepare_import_source

    body = _bundle(tmp_path)
    source = tmp_path / "library" if folder else body
    original = (tmp_path / "library/First/First.zip").read_bytes()
    original_body = body.read_bytes()
    prepared = prepare_import_source(source, ArchiveService(), "zip", True, tmp_path / "work")
    assert prepared is not None
    assert (prepared.clean_path / "First/mod.ini").read_bytes() == EDITED.encode()
    assert (prepared.clean_path / "First/mesh.bin").is_file()
    assert (tmp_path / "library/First/First.zip").read_bytes() == original
    assert body.read_bytes() == original_body


def test_deployed_inventory_ignores_metadata_but_rejects_real_added_inis(tmp_path):
    (tmp_path / "mod.ini").write_bytes(ORIGINAL.encode())
    metadata = tmp_path / "sub/.D3DXMETA/ini_overrides"
    metadata.mkdir(parents=True)
    (metadata / "copy.ini").write_bytes(EDITED.encode())
    guard = IniInventoryGuard.deployed(tmp_path, ["mod.ini"])
    guard.verify()
    (tmp_path / "external.ini").write_bytes(ORIGINAL.encode())
    with pytest.raises(VariantRecoveryError):
        guard.verify()


@pytest.mark.parametrize("cached", [False, True])
def test_nested_bundle_completes_enable_and_database_publication(temp_data_dir, tmp_path, cached):
    """通过真实管理器完成解压、配置恢复和数据库发布，旧部署中的本地修改须保留。"""
    from src.core.deployment_enable import EnablePreparation
    from src.core.game_config_store import GameConfigStore
    from src.core.mod_manager import ModManager
    from src.core.mod_package import build_local_manifest, write_sidecar
    from src.utils import fs

    migoto = tmp_path / "loader"
    (migoto / "Mods").mkdir(parents=True)
    (migoto / "d3dx.ini").write_bytes(b"[Include]\ninclude_recursive = Mods\nexclude_recursive = DISABLED*\n")
    register_games("g1")
    store = GameConfigStore()
    games = store.get_games()
    games[0].update({"migoto_path": str(migoto), "mods_path": str(migoto / "Mods")})
    store.set_games(games)
    reset_game_service()
    manager = ModManager()
    try:
        manager._game_service.reload_games()
        manager.set_game("g1")
        manager.set_deploy_dir(str(migoto / "Mods"))
        node = manager.add_character(name="示例节点", node_type="character")
        original = _bundle(tmp_path)
        destination = manager._get_character_library(node.id) / "Collection"
        fs.copy_path(original.parent, destination)
        manager.rebuild_index_from_filesystem("g1")
        mod = manager.get_mods(node_id=node.id)[0]
        assert write_sidecar(mod.path, build_local_manifest(mod.to_dict()))
        if cached:
            parked = manager._storage.deployment_paths(mod.mod_id, mod.folder_name)[1]
            extracted = ArchiveService().extract(Path(mod.path), parked)
            manager._storage._flatten_staged_deployment(extracted, parked)
            write_deployment_identity(parked, mod.mod_id, mod.content_hash)
            (parked / "First/.d3dxmeta/ini_overrides/mod.ini").write_bytes(b"[Constants]\nvalue = 3\n")
        else:
            parked = manager._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name, active=False)
        preparation = EnablePreparation(manager, mod.mod_id, parked, stage_only=True).run()
        enabled = manager.finalize_enable(
            mod.mod_id, True, deploy_path=preparation.deploy_path, preparation=preparation
        )
        assert enabled is not None and enabled.is_enabled
        expected = b"[Constants]\nvalue = 3\n" if cached else EDITED.encode()
        assert (Path(enabled.deployed_path) / "First/mod.ini").read_bytes() == expected
        assert (Path(enabled.deployed_path) / "First/mesh.bin").read_bytes() == b"mesh data"
        assert manager._index.get_mod(mod.mod_id).is_enabled
    finally:
        manager.stop_ini_sync()
        manager.variants.stop_watch()
        reset_game_service()


@pytest.mark.parametrize("error", [VariantRecoveryError("inventory mismatch"), PermissionError("access denied")])
def test_unconfirmed_failures_do_not_blame_other_programs(error):
    message = user_error_message(error)
    assert "其他程序" not in message
    assert "关闭游戏" not in message
    assert "占用" not in message
