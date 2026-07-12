from pathlib import Path

import pytest

from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


@pytest.mark.parametrize("old_copy", [False, True])
def test_deleted_source_cannot_restore_or_publish(qapp, mm, tmp_path, old_copy):
    from src.ui.deploy_worker import DeployWorker

    mod = _import_mod(mm, mm.add_character("Preflight").id, tmp_path, "gone")
    mm.set_deploy_dir(str(tmp_path / "Mods"))
    parked = None
    if old_copy:
        active = mm._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name)
        parked = mm._storage.park_deployment(active, mod.mod_id)
    Path(mod.path).unlink()
    worker = DeployWorker(mod.mod_id, mod.path, mm._storage, restore_existing=True, remembered_path=str(parked or ""))
    worker.run()
    assert not worker.result[0]
    assert not mm.get_mod(mod.mod_id).is_enabled
    if parked:
        assert parked.exists()


def test_finalization_rejects_source_deleted_after_worker(mm, tmp_path):
    mod = _import_mod(mm, mm.add_character("Preflight").id, tmp_path, "late")
    mm.set_deploy_dir(str(tmp_path / "Mods"))
    active = mm._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name)
    Path(mod.path).unlink()
    assert mm.finalize_enable(mod.mod_id, True, deploy_path=str(active)) is None
    assert not mm.get_mod(mod.mod_id).is_enabled


@pytest.mark.parametrize("operation", ["update", "rename", "preview", "move"])
def test_missing_source_edit_does_not_change_record_or_container(mm, tmp_path, operation):
    node = mm.add_character("Preflight")
    other = mm.add_character("Other")
    mod = _import_mod(mm, node.id, tmp_path, "edit")
    preview = tmp_path / "preview.png"
    preview.write_bytes(b"example")
    before = mm.get_mod(mod.mod_id).to_dict()
    container = Path(mod.path).parent
    Path(mod.path).unlink()
    files = {str(p.relative_to(container)): p.read_bytes() for p in container.rglob("*") if p.is_file()}
    if operation == "update":
        mod.name = "changed"
        result = mm.update_mod(mod)
    elif operation == "rename":
        result = mm.rename_mod(mod.mod_id, "changed")
    elif operation == "preview":
        result = mm.save_preview(mod.mod_id, str(preview))
    else:
        result = mm.move_mod(mod.mod_id, other.id)
    assert not result
    assert mm.get_mod(mod.mod_id).to_dict() == before
    assert container.exists()
    assert {str(p.relative_to(container)): p.read_bytes() for p in container.rglob("*") if p.is_file()} == files


def test_missing_metadata_is_not_recreated_by_edit(mm, tmp_path):
    mod = _import_mod(mm, mm.add_character("Preflight").id, tmp_path, "metadata")
    metadata = Path(mod.path).parent / ".d3dxmeta" / "info.json"
    metadata.unlink()
    mod.name = "changed"
    assert not mm.update_mod(mod)
    assert not metadata.exists()
    assert mm.get_mod(mod.mod_id).name != "changed"


def test_changed_archive_is_rejected_without_rewriting_marker(mm, tmp_path):
    import zipfile

    mod = _import_mod(mm, mm.add_character("Preflight").id, tmp_path, "changed")
    marker = Path(mod.path).parent / ".d3dxmod"
    before = marker.read_bytes()
    with zipfile.ZipFile(mod.path, "w") as archive:
        archive.writestr("mod.ini", "[Constants]\nx=another")
    mod.name = "new title"
    assert not mm.update_mod(mod)
    assert marker.read_bytes() == before
    assert mm.get_mod(mod.mod_id).name == "changed"


def test_open_draft_rejects_external_metadata_change(mm, tmp_path):
    import json

    from src.core.operation_preflight import OperationPreflightError

    mod = _import_mod(mm, mm.add_character("Preflight").id, tmp_path, "draft")
    draft = mm.get_tag_service().begin_edit(mod)
    path = Path(mod.path).parent / ".d3dxmeta" / "info.json"
    external = json.loads(path.read_text(encoding="utf-8"))
    external["external_change"] = True
    path.write_text(json.dumps(external), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(OperationPreflightError):
        mm.commit_tag_edit(draft, {"name": "changed"})
    assert path.read_bytes() == before
    assert mm.get_mod(mod.mod_id).name == mod.name


def test_batch_checks_all_targets_before_disabling_any(mm, tmp_path):
    node = mm.add_character("Preflight")
    mods = [_import_mod(mm, node.id, tmp_path, name) for name in ("one", "two")]
    mm.set_deploy_dir(str(tmp_path / "Mods"))
    for mod in mods:
        assert mm.toggle_mod(mod.mod_id).is_enabled
    Path(mods[1].path).unlink()
    assert mm.prepare_disable_batch([m.mod_id for m in mods]) == []
    for mod in mods:
        saved = mm.get_mod(mod.mod_id)
        assert saved.is_enabled and Path(saved.deployed_path).exists()


def test_missing_body_reference_is_not_recovered_during_edit(mm, tmp_path):
    import json

    mod = _import_mod(mm, mm.add_character("Preflight").id, tmp_path, "reference")
    path = Path(mod.path).parent / ".d3dxmod"
    marker = json.loads(path.read_text(encoding="utf-8"))
    del marker["body"]
    path.write_text(json.dumps(marker), encoding="utf-8")
    before = path.read_bytes()
    assert not mm.rename_mod(mod.mod_id, "recovered")
    assert path.read_bytes() == before
    assert mm.get_mod(mod.mod_id).name == mod.name


def test_preset_executor_checks_missing_restore_before_parking_old(mm, tmp_path):
    from src.core.preset_apply_service import PresetApplyError, PresetPlanExecutor
    from src.core.preset_deployment_plan import DeploymentAction, PresetDeploymentPlan

    node = mm.add_character("Preflight")
    old, new = [_import_mod(mm, node.id, tmp_path, name) for name in ("old", "new")]
    mm.set_deploy_dir(str(tmp_path / "Mods"))
    active = mm._storage.materialize_deployment(old.path, old.mod_id, old.folder_name)
    parked = mm._storage.materialize_deployment(new.path, new.mod_id, new.folder_name, active=False)
    old_parked = active.with_name(mm._storage._parked_deploy_name(old.mod_id, active.name))
    new_active = mm._storage.active_deployment_path(new.mod_id, new.folder_name)
    plan = PresetDeploymentPlan(
        "test",
        "game_a",
        "",
        "",
        (
            DeploymentAction(old.mod_id, old.path, str(active), str(old_parked), str(active) + ".partial", "park"),
            DeploymentAction(new.mod_id, new.path, str(new_active), str(parked), str(parked) + ".partial", "restore"),
        ),
    )
    Path(new.path).unlink()
    with pytest.raises(PresetApplyError):
        PresetPlanExecutor(mm._storage).execute(plan)
    assert active.exists() and parked.exists()
    assert not old_parked.exists() and not new_active.exists()


def test_explicit_delete_cascades_missing_source_and_deployment(mm, tmp_path):
    mod = _import_mod(mm, mm.add_character("Preflight").id, tmp_path, "delete")
    mm.set_deploy_dir(str(tmp_path / "Mods"))
    current = mm.toggle_mod(mod.mod_id)
    deployed = Path(current.deployed_path)
    container = Path(mod.path).parent
    Path(mod.path).unlink()
    assert mm.delete_mod(mod.mod_id)
    assert not container.exists() and not deployed.exists()
    assert mm.get_mod(mod.mod_id) is None
    assert mod.mod_id not in mm.preset_manager.get_current(mm.current_game_id).enabled_mod_ids


def test_explicit_delete_does_not_remove_replacement_identity(mm, tmp_path):
    import json
    import uuid

    mod = _import_mod(mm, mm.add_character("Preflight").id, tmp_path, "replaced")
    marker = Path(mod.path).parent / ".d3dxmod"
    value = json.loads(marker.read_text(encoding="utf-8"))
    value["mod_id"] = uuid.uuid4().hex
    marker.write_text(json.dumps(value), encoding="utf-8")
    assert not mm.delete_mod(mod.mod_id)
    assert Path(mod.path).exists() and marker.exists()
    assert mm.get_mod(mod.mod_id) is not None


def test_finalization_matches_worker_receipt_even_when_marker_unchanged(mm, tmp_path):
    from src.core.operation_preflight import confirm_source

    mod = _import_mod(mm, mm.add_character("Preflight").id, tmp_path, "receipt")
    mm.set_deploy_dir(str(tmp_path / "Mods"))
    receipt = confirm_source(mod.path, mod.mod_id, mod.content_hash)
    target = mm._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name)
    Path(mod.path).write_bytes(b"changed after preparation")
    assert mm.finalize_enable(mod.mod_id, True, deploy_path=str(target), source_receipt=receipt) is None
    assert not mm.get_mod(mod.mod_id).is_enabled


def test_disable_completion_rechecks_worker_source(mm, tmp_path):
    from dataclasses import replace

    mod = _import_mod(mm, mm.add_character("Preflight").id, tmp_path, "disable")
    mm.set_deploy_dir(str(tmp_path / "Mods"))
    active = mm.toggle_mod(mod.mod_id).deployed_path
    (entry,) = mm.prepare_disable_batch([mod.mod_id])
    result = mm.disable_batch_step(replace(entry, park=True))
    assert result.ok
    Path(mod.path).write_bytes(b"changed before commit")
    assert mm.commit_disable_batch([result]) == []
    assert result.preflight_error is not None
    assert Path(active).exists()
    assert mm.get_mod(mod.mod_id).is_enabled


def test_tag_edit_commits_real_sidecar_and_database_together(mm, tmp_path):
    import json

    mod = _import_mod(mm, mm.add_character("Preflight").id, tmp_path, "tags")
    draft = mm.get_tag_service().begin_edit(mod)
    result = mm.commit_tag_edit(draft, {"name": "new title", "author": "new author"})
    assert not result.sidecar_failed_ids
    saved = mm.get_mod(mod.mod_id)
    data = json.loads((Path(mod.path).parent / ".d3dxmeta/info.json").read_text(encoding="utf-8"))
    assert data["name"] == saved.name == "new title"
    assert data["info"]["author"] == saved.info.author == "new author"


def test_existing_external_metadata_change_blocks_an_edit(mm, tmp_path):
    import json

    mod = _import_mod(mm, mm.add_character("Preflight").id, tmp_path, "external")
    path = Path(mod.path).parent / ".d3dxmeta/info.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["name"] = "external title"
    path.write_text(json.dumps(data), encoding="utf-8")
    mod.info.author = "new author"
    assert not mm.update_mod(mod)
    assert json.loads(path.read_text(encoding="utf-8")) == data
    assert mm.get_mod(mod.mod_id).info.author != "new author"


def test_edit_does_not_supplement_removed_metadata_fields(mm, tmp_path):
    import json

    mod = _import_mod(mm, mm.add_character("Preflight").id, tmp_path, "incomplete")
    path = Path(mod.path).parent / ".d3dxmeta/info.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["info"]["author"]
    path.write_text(json.dumps(data), encoding="utf-8")
    mod.info.description = "new description"
    assert not mm.update_mod(mod)
    assert json.loads(path.read_text(encoding="utf-8")) == data


def test_key_edit_uses_owned_deployment_and_rejects_missing_source(mm, tmp_path):
    source = tmp_path / "keys"
    source.mkdir()
    (source / "mod.ini").write_text("[KeySwap]\nkey = VK_A\n", encoding="utf-8")
    mod = mm.import_mod(str(source), node_id=mm.add_character("Preflight").id)
    mm.set_deploy_dir(str(tmp_path / "Mods"))
    active = mm.toggle_mod(mod.mod_id)
    binding = mm.parse_mod_key_bindings(mod)[0]
    assert mm.edit_mod_key(mod.mod_id, binding.rel, binding.key_line_no, "key", "VK_B")
    assert "VK_B" in (Path(active.deployed_path) / binding.rel).read_text(encoding="utf-8")
    Path(mod.path).unlink()
    assert not mm.edit_mod_key(mod.mod_id, binding.rel, binding.key_line_no, "key", "VK_C")
    assert "VK_B" in (Path(active.deployed_path) / binding.rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("existing", [False, True])
def test_preview_save_restores_files_when_metadata_commit_fails(mm, tmp_path, monkeypatch, existing):
    mod = _import_mod(mm, mm.add_character("Preflight").id, tmp_path, "preview")
    image = tmp_path / "preview.png"
    image.write_bytes(b"original preview")
    if existing:
        assert mm.save_preview(mod.mod_id, str(image))
        image.write_bytes(b"replacement preview")
    container = Path(mod.path).parent
    files = {str(p.relative_to(container)): p.read_bytes() for p in container.rglob("*") if p.is_file()}
    before = mm.get_mod(mod.mod_id).to_dict()
    monkeypatch.setattr(mm._index, "update_mod", lambda updated: False)
    assert not mm.save_preview(mod.mod_id, str(image))
    assert mm.get_mod(mod.mod_id).to_dict() == before
    assert {str(p.relative_to(container)): p.read_bytes() for p in container.rglob("*") if p.is_file()} == files


def test_move_does_not_recreate_missing_destination(mm, tmp_path):
    node = mm.add_character("Preflight")
    destination = mm.add_character("Destination")
    mod = _import_mod(mm, node.id, tmp_path, "move")
    target = mm._index.paths.node_path(destination.id)
    target.rename(target.with_name("externally-moved"))
    assert not mm.move_mod(mod.mod_id, destination.id)
    assert not target.exists()
    assert Path(mod.path).exists()
    assert mm.get_mod(mod.mod_id).node_id == node.id


def test_delete_category_rejects_replacement_identity_before_deleting_children(mm, tmp_path):
    import json
    import uuid

    from src.core.mod_manager import CATEGORY_MARKER_FILE

    node = mm.add_character("Preflight")
    mod = _import_mod(mm, node.id, tmp_path, "protected")
    path = mm._index.paths.node_path(node.id) / CATEGORY_MARKER_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    data["id"] = uuid.uuid4().hex
    path.write_text(json.dumps(data), encoding="utf-8")
    assert not mm.delete_character(node.id)
    assert Path(mod.path).exists()
    assert mm.get_mod(mod.mod_id) is not None
    assert mm.get_character(node.id) is not None
