import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def request(mm, **changes):
    from src.core.library_snapshot import ProbeToken

    return replace(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)), mode="full", **changes)


def enabled(mm, tmp_path, name="archive"):
    node = mm.add_character(name=name, node_type="character")
    mod = _import_mod(mm, node.id, tmp_path, name)
    mm.set_deploy_dir(str(tmp_path / "deploy"))
    assert mm.toggle_mod(mod.mod_id)
    mm.preset_manager.create_preset("game_a", name, snapshot={node.id: [mod.mod_id]})
    return mm.get_mod(mod.mod_id)


@pytest.mark.parametrize("warm", [False, True])
def test_deleted_archive_cascades_container_deployment_record_and_presets(mm, tmp_path, warm):
    from src.core.library_snapshot import probe_library

    mod = enabled(mm, tmp_path)
    previous = probe_library(request(mm)).source_inventory if warm else None
    Path(mod.path).unlink()
    result = probe_library(request(mm, source_inventory=previous))
    assert not result.error
    list(mm.apply_library_probe_result(result))
    assert mm.get_mod(mod.mod_id) is None
    assert mm._index.get_deployment(mod.mod_id) is None
    assert not Path(mod.path).parent.exists()
    assert not Path(mod.deployed_path).exists()
    assert all(
        mod.mod_id not in ids
        for preset in mm.preset_manager.get_all_presets("game_a")
        for ids in preset.character_mods.values()
    )


def test_deleted_archive_cleanup_failure_retries_without_losing_record(mm, tmp_path, monkeypatch):
    from src.core.library_snapshot import probe_library

    mod = enabled(mm, tmp_path)
    Path(mod.path).unlink()
    with monkeypatch.context() as patch:
        patch.setattr(mm._storage, "undeploy_mod", lambda *args, **kwargs: False)
        result = probe_library(request(mm))
        assert not result.error
        list(mm.apply_library_probe_result(result))
    assert mm.get_mod(mod.mod_id)
    assert Path(mod.path).parent.exists()
    assert Path(mod.deployed_path).exists()
    assert mm.has_pending_source_cleanup()
    list(mm.apply_library_probe_result(probe_library(request(mm))))
    assert mm.get_mod(mod.mod_id) is None


@pytest.mark.parametrize("change", ["identity", "rename", "unmanaged"])
def test_new_container_changes_after_probe_preserve_deployment_and_record(mm, tmp_path, change):
    from src.core.library_snapshot import probe_library
    from src.core.mod_identity import new_mod_id

    mod = enabled(mm, tmp_path)
    body = Path(mod.path)
    saved = body.read_bytes()
    body.unlink()
    result = probe_library(request(mm))
    assert not result.error
    if change == "identity":
        marker = body.parent / ".d3dxmod"
        data = json.loads(marker.read_text(encoding="utf8"))
        data["mod_id"] = new_mod_id()
        marker.write_text(json.dumps(data), encoding="utf8")
    elif change == "rename":
        body.with_name("renamed.zip").write_bytes(saved)
    else:
        (body.parent / "user.txt").write_text("keep", encoding="utf8")
    list(mm.apply_library_probe_result(result))
    assert mm.get_mod(mod.mod_id)
    assert body.parent.exists()
    assert Path(mod.deployed_path).exists()


def test_empty_old_container_does_not_hide_same_identity_moved_elsewhere(mm, tmp_path):
    from src.core.library_snapshot import probe_library

    mod = enabled(mm, tmp_path)
    body = Path(mod.path)
    moved = body.parent.with_name("moved")
    shutil.copytree(body.parent, moved)
    body.unlink()
    result = probe_library(request(mm))
    assert not result.error
    list(mm.apply_library_probe_result(result))
    assert mm.get_mod(mod.mod_id).path == str(moved / body.name)
    assert Path(mod.deployed_path).exists()


def test_cleanup_rechecks_replaced_identity_even_without_probe(mm, tmp_path):
    from src.core.mod_identity import new_mod_id

    mod = enabled(mm, tmp_path)
    body = Path(mod.path)
    body.unlink()
    marker = body.parent / ".d3dxmod"
    data = json.loads(marker.read_text(encoding="utf8"))
    data["mod_id"] = new_mod_id()
    marker.write_text(json.dumps(data), encoding="utf8")
    assert not mm._cleanup_missing_source_artifacts(mod, "game_a")
    assert body.parent.exists()
    assert Path(mod.deployed_path).exists()


def test_direct_cleanup_preserves_mod_when_library_root_is_offline(mm, tmp_path):
    mod = enabled(mm, tmp_path)
    root = mm._get_library_root("game_a")
    root.rename(root.with_name("offline"))
    assert not mm._cleanup_missing_source_artifacts(mod, "game_a")
    assert mm.get_mod(mod.mod_id)
    assert Path(mod.deployed_path).exists()


def test_new_identity_during_deployment_cleanup_preserves_container(mm, tmp_path, monkeypatch):
    from src.core.mod_identity import new_mod_id

    mod = enabled(mm, tmp_path)
    body = Path(mod.path)
    body.unlink()
    original = mm._remove_known_deployments

    def replace_after_cleanup(*args, **kwargs):
        outcome = original(*args, **kwargs)
        marker = body.parent / ".d3dxmod"
        data = json.loads(marker.read_text(encoding="utf8"))
        data["mod_id"] = new_mod_id()
        marker.write_text(json.dumps(data), encoding="utf8")
        return outcome

    monkeypatch.setattr(mm, "_remove_known_deployments", replace_after_cleanup)
    assert not mm._purge_missing_source_mod(mod, "game_a")
    assert mm.get_mod(mod.mod_id)
    assert body.parent.exists()


def test_multiple_empty_containers_finish_through_existing_pending_retry(mm, tmp_path):
    from src.core.library_snapshot import probe_library

    node = mm.add_character(name="multiple", node_type="character")
    mods = [_import_mod(mm, node.id, tmp_path, name) for name in ("first", "second")]
    for mod in mods:
        Path(mod.path).unlink()
    first = probe_library(request(mm))
    assert not first.error
    list(mm.apply_library_probe_result(first))
    assert mm.has_pending_source_cleanup()
    second = probe_library(request(mm))
    assert not second.error
    list(mm.apply_library_probe_result(second))
    assert all(mm.get_mod(mod.mod_id) is None for mod in mods)
    assert all(not Path(mod.path).parent.exists() for mod in mods)
    assert not mm.has_pending_source_cleanup()


def test_marker_changed_after_its_read_invalidates_complete_scan(mm, tmp_path, monkeypatch):
    from src.core import library_inventory
    from src.core.library_snapshot import probe_library

    mod = enabled(mm, tmp_path)
    marker = Path(mod.path).parent / ".d3dxmod"
    original = library_inventory.source_directory_signature

    def change_at_scan_end(paths):
        if not isinstance(paths, tuple):
            data = json.loads(marker.read_text(encoding="utf8"))
            data["content_hash"] = "b" * 40
            marker.write_text(json.dumps(data), encoding="utf8")
        return original(paths)

    monkeypatch.setattr(library_inventory, "source_directory_signature", change_at_scan_end)
    result = probe_library(request(mm))
    assert result.error
    assert list(mm.apply_library_probe_result(result)) == []
    assert mm.get_mod(mod.mod_id)
