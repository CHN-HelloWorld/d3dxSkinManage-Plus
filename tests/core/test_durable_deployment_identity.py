from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.core.config import Config
from src.core.mod_identity import read_source_mod_id
from src.core.mod_manager import ModManager
from src.core.mod_package import read_sidecar
from tests.helpers import registered_games as registered_games


@pytest.fixture
def library(temp_data_dir, tmp_path):
    Config().set("import.format", "zip")
    manager = ModManager()
    manager.set_game("game_a")
    manager.set_deploy_dir(str(tmp_path / "deploy"))
    yield manager
    manager.stop_ini_sync()


def import_skin(manager, tmp_path, name="Skin", character="Character", value=1):
    char = manager.add_character(name=character, node_type="character")
    source = tmp_path / f"input-{value}" / name
    source.mkdir(parents=True)
    (source / "mod.ini").write_text(f"[Constants]\nx={value}\n", encoding="utf-8")
    mod = manager.import_mod(source_path=str(source), node_id=char.id)
    assert mod is not None
    return mod


def set_identity(mod, value):
    path = Path(mod.path).parent / ".d3dxmod"
    data = json.loads(path.read_text(encoding="utf-8"))
    if value is None:
        data.pop("mod_id", None)
    else:
        data["mod_id"] = value
    path.write_text(json.dumps(data), encoding="utf-8")


def test_import_and_deployment_persist_same_identity(library, tmp_path):
    mod = import_skin(library, tmp_path)
    assert read_source_mod_id(mod.path) == mod.mod_id
    assert "mod_id" not in read_sidecar(mod.path).get("local", {})
    enabled = library.toggle_mod(mod.mod_id)
    marker = Path(enabled.deployed_path) / ".d3dxmod"
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data == {"kind": "d3dxSkinManage-Plus.deployment", "mod_id": mod.mod_id, "content_hash": mod.content_hash}


def _tree_bytes(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("identity", [None, "malformed", "22222222222242228222222222222222"])
def test_rename_rejects_invalid_source_identity_before_any_mutation(library, tmp_path, monkeypatch, enabled, identity):
    mod = import_skin(library, tmp_path)
    if enabled:
        mod = library.toggle_mod(mod.mod_id)
        assert mod is not None and mod.is_enabled
    library.stop_ini_sync()
    set_identity(mod, identity)
    container = Path(mod.path).parent
    source_before = _tree_bytes(container)
    deployment_root = tmp_path / "deploy"
    deployment_before = _tree_bytes(deployment_root)
    index_before = [item.to_dict() for item in library.get_mods()]
    mutations = []
    original_rename = Path.rename
    original_undeploy = library._remove_known_deployments

    def observe_rename(path, target):
        mutations.append(("rename", str(path), str(target)))
        return original_rename(path, target)

    def observe_undeploy(*args, **kwargs):
        mutations.append(("undeploy",))
        return original_undeploy(*args, **kwargs)

    monkeypatch.setattr(Path, "rename", observe_rename)
    monkeypatch.setattr(library, "_remove_known_deployments", observe_undeploy)
    assert library.rename_mod(mod.mod_id, "Renamed") is None
    assert mutations == []
    assert Path(mod.path).is_file()
    assert _tree_bytes(container) == source_before
    assert _tree_bytes(deployment_root) == deployment_before
    assert [item.to_dict() for item in library.get_mods()] == index_before
    assert not container.with_name("Renamed").exists()


def test_rename_rollback_restores_paths_without_overwriting_reassigned_identity(library, tmp_path, monkeypatch):
    mod = import_skin(library, tmp_path)
    body = Path(mod.path)
    container = body.parent
    target_container = container.with_name("Renamed")
    original_rename = Path.rename
    replacement = "22222222222242228222222222222222"
    foreign_marker = None
    index_before = [item.to_dict() for item in library.get_mods()]
    original_body = body.read_bytes()

    def reassign_after_body_move(path, target):
        nonlocal foreign_marker
        result = original_rename(path, target)
        if path == target_container / body.name:
            marker = target_container / ".d3dxmod"
            data = json.loads(marker.read_text(encoding="utf-8"))
            data["mod_id"] = replacement
            marker.write_text(json.dumps(data), encoding="utf-8")
            foreign_marker = marker.read_bytes()
        return result

    monkeypatch.setattr(Path, "rename", reassign_after_body_move)
    assert library.rename_mod(mod.mod_id, "Renamed") is None
    assert foreign_marker is not None
    assert body.read_bytes() == original_body
    assert (container / ".d3dxmod").read_bytes() == foreign_marker
    assert [item.to_dict() for item in library.get_mods()] == index_before
    assert not target_container.exists()


@pytest.mark.parametrize("boundary", ["body_move", "marker_write"])
def test_enabled_rename_never_redeploys_with_another_mod_identity(library, tmp_path, monkeypatch, boundary):
    from src.core import mod_manager as manager_module

    first = import_skin(library, tmp_path, name="A", character="A", value=1)
    second = import_skin(library, tmp_path, name="B", character="B", value=2)
    first = library.toggle_mod(first.mod_id)
    second = library.toggle_mod(second.mod_id)
    assert first is not None and second is not None
    library.stop_ini_sync()
    body = Path(first.path)
    target = body.parent.with_name("Renamed")
    second_source = _tree_bytes(Path(second.path).parent)
    second_deployed = _tree_bytes(Path(second.deployed_path))
    second_record = library.get_mod(second.mod_id).to_dict()
    foreign_marker = None
    deployment_calls = []
    original_rename = Path.rename
    original_write = manager_module.write_container_marker
    original_deploy = library._storage.deploy_mod

    def reassign():
        nonlocal foreign_marker
        marker = target / ".d3dxmod"
        data = json.loads(marker.read_text(encoding="utf-8"))
        data["mod_id"] = second.mod_id
        marker.write_text(json.dumps(data), encoding="utf-8")
        foreign_marker = marker.read_bytes()

    def rename_then_reassign(path, destination):
        result = original_rename(path, destination)
        if boundary == "body_move" and path == target / body.name:
            reassign()
        return result

    def write_then_reassign(*args, **kwargs):
        result = original_write(*args, **kwargs)
        if result and boundary == "marker_write":
            reassign()
        return result

    def observe_deploy(source, *args, **kwargs):
        marker = Path(source).parent / ".d3dxmod"
        deployment_calls.append(json.loads(marker.read_text(encoding="utf-8"))["mod_id"])
        return original_deploy(source, *args, **kwargs)

    monkeypatch.setattr(Path, "rename", rename_then_reassign)
    monkeypatch.setattr(manager_module, "write_container_marker", write_then_reassign)
    monkeypatch.setattr(library._storage, "deploy_mod", observe_deploy)
    result = library.rename_mod(first.mod_id, "Renamed")
    assert _tree_bytes(Path(second.deployed_path)) == second_deployed
    assert _tree_bytes(Path(second.path).parent) == second_source
    assert library.get_mod(second.mod_id).to_dict() == second_record
    assert second.mod_id not in deployment_calls
    assert result is None
    assert body.is_file()
    assert (body.parent / ".d3dxmod").read_bytes() == foreign_marker
    assert library.get_mod(first.mod_id).mod_id == first.mod_id


def test_enabled_rename_rollback_redeploys_only_its_unchanged_identity(library, tmp_path, monkeypatch):
    mod = import_skin(library, tmp_path)
    mod = library.toggle_mod(mod.mod_id)
    assert mod is not None
    library.stop_ini_sync()
    before = _tree_bytes(Path(mod.deployed_path))
    monkeypatch.setattr(library, "_write_mod_sidecar", lambda *_args, **_kwargs: False)
    assert library.rename_mod(mod.mod_id, "Renamed") is None
    assert Path(mod.path).is_file()
    assert read_source_mod_id(mod.path) == mod.mod_id
    assert _tree_bytes(Path(mod.deployed_path)) == before


@pytest.mark.parametrize("reconcile_first", [False, True])
@pytest.mark.parametrize("rename_source", [False, True])
def test_rename_and_repeated_rebuild_keep_identity_and_deployment(library, tmp_path, reconcile_first, rename_source):
    mod = import_skin(library, tmp_path)
    enabled = library.toggle_mod(mod.mod_id)
    renamed = Path(enabled.deployed_path).with_name("UserRenamed")
    Path(enabled.deployed_path).rename(renamed)
    if rename_source:
        source = Path(mod.path).parent
        source.rename(source.with_name("UserRenamedContainer"))
    if reconcile_first:
        library.reconcile_missing_deployments("game_a")
        current = library.get_mod(mod.mod_id)
        assert current.is_enabled
        assert Path(current.deployed_path) == renamed
    for _ in range(2):
        report = library.rebuild_index_from_filesystem("game_a")
        assert report.registered == 1
        (restored,) = library.get_mods()
        assert restored.mod_id == mod.mod_id
        assert restored.is_enabled
        assert Path(restored.deployed_path) == renamed
        record = library._index.get_deployment(mod.mod_id)
        assert record.mod_id == restored.mod_id
        assert Path(record.actual_path) == renamed


def test_rebuild_with_empty_index_recovers_identity_and_parked_path(library, tmp_path):
    mod = import_skin(library, tmp_path)
    enabled = library.toggle_mod(mod.mod_id)
    parked = library._storage.park_deployment(enabled.deployed_path, mod.mod_id)
    renamed = parked.with_name("DISABLED_CustomName")
    parked.rename(renamed)
    library.stop_ini_sync()
    library._index.clear_mods_for_game("game_a")
    report = library.rebuild_index_from_filesystem("game_a")
    assert report.registered == 1
    (restored,) = library.get_mods()
    assert restored.mod_id == mod.mod_id
    assert Path(restored.deployed_path) == renamed
    assert not restored.is_enabled
    assert library._index.get_deployment(mod.mod_id).state.value == "disabled"


def test_missing_source_identity_recovers_indexed_owner_without_clearing_index(library, tmp_path):
    mod = import_skin(library, tmp_path)
    set_identity(mod, None)
    before = [m.to_dict() for m in library.get_mods()]
    library.rebuild_index_from_filesystem("game_a")
    assert read_source_mod_id(mod.path) == mod.mod_id
    assert [m.to_dict() for m in library.get_mods()] == before


def test_duplicate_source_identity_rejects_rebuild_before_deleting(library, tmp_path):
    first = import_skin(library, tmp_path)
    second = import_skin(library, tmp_path, name="Other", character="OtherChar", value=2)
    set_identity(second, first.mod_id)
    before = [m.to_dict() for m in library.get_mods()]
    with pytest.raises(ValueError, match="identity|mod_id|身份"):
        library.rebuild_index_from_filesystem("game_a")
    assert [m.to_dict() for m in library.get_mods()] == before


def test_duplicate_deployment_identity_never_selects_one_copy(library, tmp_path):
    mod = import_skin(library, tmp_path)
    enabled = library.toggle_mod(mod.mod_id)
    shutil.copytree(enabled.deployed_path, tmp_path / "deploy" / "Duplicate")
    before = [m.to_dict() for m in library.get_mods()]
    with pytest.raises(ValueError, match="identity|mod_id|身份"):
        library.rebuild_index_from_filesystem("game_a")
    assert [m.to_dict() for m in library.get_mods()] == before
    assert (tmp_path / "deploy" / "Duplicate").is_dir()


def test_rebuild_insert_failure_restores_previous_index(library, tmp_path, monkeypatch):
    first = import_skin(library, tmp_path)
    import_skin(library, tmp_path, name="Other", character="OtherChar", value=2)
    library.toggle_mod(first.mod_id)
    before = [m.to_dict() for m in library.get_mods()]
    original = library._index.add_mod
    writes = 0

    def fail_second(mod, *, commit=True):
        nonlocal writes
        writes += 1
        return False if writes == 2 else original(mod, commit=commit)

    monkeypatch.setattr(library._index, "add_mod", fail_second)
    with pytest.raises(RuntimeError):
        library.rebuild_index_from_filesystem("game_a")
    assert [m.to_dict() for m in library.get_mods()] == before
    assert library._index.get_deployment(first.mod_id).mod_id == first.mod_id


def test_same_name_different_characters_have_independent_deployments(library, tmp_path):
    first = import_skin(library, tmp_path, character="First")
    second = import_skin(library, tmp_path, character="Second", value=2)
    one = library.toggle_mod(first.mod_id)
    two = library.toggle_mod(second.mod_id)
    assert one is not None and two is not None
    assert one.mod_id != two.mod_id
    assert Path(one.deployed_path) != Path(two.deployed_path)
    assert "x=1" in (Path(one.deployed_path) / "mod.ini").read_text()
    assert "x=2" in (Path(two.deployed_path) / "mod.ini").read_text()
    library.rebuild_index_from_filesystem("game_a")
    assert {m.mod_id for m in library.get_mods() if m.is_enabled} == {first.mod_id, second.mod_id}


def test_relocated_source_registers_sync_at_renamed_deployment(library, tmp_path, monkeypatch):
    mod = import_skin(library, tmp_path)
    enabled = library.toggle_mod(mod.mod_id)
    library.stop_ini_sync()
    deployed = Path(enabled.deployed_path).with_name("RenamedDeployment")
    Path(enabled.deployed_path).rename(deployed)
    source = Path(mod.path).parent
    relocated = source.with_name("RelocatedSource")
    source.rename(relocated)
    registrations = []
    monkeypatch.setattr(
        library,
        "_register_ini_sync",
        lambda identity, body, target, **kw: registrations.append((identity, Path(body), Path(target))),
    )

    assert library.claim_relocated_containers("game_a") == 1
    assert registrations == [(mod.mod_id, relocated / Path(mod.path).name, deployed)]
