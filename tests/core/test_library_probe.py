import os
import shutil
from pathlib import Path

import pytest

from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def test_unchanged_probe_apply_never_repeats_full_scan(mm, tmp_path, monkeypatch):
    from src.core.library_snapshot import ProbeToken, probe_library

    char = mm.add_character(name="Probe", node_type="character")
    _import_mod(mm, char.id, tmp_path, "unchanged")
    mm.reconcile_library("game_a", trigger="poll")
    request = mm.create_library_probe_request(ProbeToken("game_a", 1, 1))
    result = probe_library(request)

    def forbidden(*args, **kwargs):
        raise AssertionError("unchanged apply must use evidence only")

    monkeypatch.setattr(mm._index, "_row_to_mod", forbidden)
    monkeypatch.setattr(mm, "_discover_library_mods", forbidden)
    monkeypatch.setattr(mm._storage, "scan_deployments", forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)
    assert not any(step.changed for step in mm.apply_library_probe_result(result))


def test_unchanged_apply_does_not_normalize_every_source_again(mm, tmp_path, monkeypatch):
    from src.core import mod_manager
    from src.core.library_snapshot import ProbeToken, probe_library

    char = mm.add_character(name="Probe", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "no-path-work")
    result = probe_library(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)))
    original = mod_manager.path_key

    def tracked(path):
        assert path != mod.path, "unchanged source already checked by worker"
        return original(path)

    monkeypatch.setattr(mod_manager, "path_key", tracked)
    assert not any(step.changed for step in mm.apply_library_probe_result(result))


def test_probe_reuses_immutable_inputs_but_refreshes_token_and_inventory(mm, monkeypatch):
    from src.core.library_snapshot import DeploymentInventory, ProbeToken

    first = mm.create_library_probe_request(ProbeToken("game_a", 1, 1))
    inventory = DeploymentInventory(scanned_at=42)
    mm._storage.adopt_deployment_inventory(inventory)

    def repeated_materialization(*args):
        raise AssertionError("unchanged input must not rematerialize deployment records")

    monkeypatch.setattr(mm._index, "get_deployments", repeated_materialization)
    second = mm.create_library_probe_request(ProbeToken("game_a", 1, 2))
    assert second.token.request_id == 2
    assert second.records is first.records
    assert second.inventory == inventory


def test_probe_template_invalidates_after_sql_and_transaction_rollback(mm, tmp_path):
    from src.core.library_snapshot import ProbeToken

    char = mm.add_character(name="Probe", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "template")
    token = ProbeToken("game_a", 1, 1)
    first = mm.create_library_probe_request(token)
    mm._index._db().execute(
        "INSERT INTO deployment_records VALUES(?,'active','') ON CONFLICT(mod_id) DO UPDATE SET state='active'",
        (mod.mod_id,),
    )
    assert mm.create_library_probe_request(token).rows[0].enabled
    mm._index._db().rollback()
    assert mm.create_library_probe_request(token).rows == first.rows


def test_probe_template_invalidates_when_explicit_deploy_root_changes(mm, tmp_path):
    from src.core.library_snapshot import ProbeToken

    token = ProbeToken("game_a", 1, 1)
    first = mm.create_library_probe_request(token)
    mm.set_deploy_dir(str(tmp_path / "new-deploy"))
    second = mm.create_library_probe_request(token)
    assert second.deploy_roots != first.deploy_roots
    assert second.deploy_roots == (str(tmp_path / "new-deploy"),)


def test_source_restored_after_probe_is_never_deleted(mm, tmp_path):
    from src.core.library_snapshot import ProbeToken, probe_library

    char = mm.add_character(name="Probe", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "restored")
    body = Path(mod.path)
    backup = tmp_path / "container-backup"
    shutil.move(body.parent, backup)
    result = probe_library(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)))
    assert not result.error
    assert result.checks[0].state == "missing"
    shutil.move(backup, body.parent)
    list(mm.apply_library_probe_result(result))
    assert mm.get_mod(mod.mod_id) is not None
    assert body.exists()


def test_relocation_claim_precedes_missing_cleanup(mm, tmp_path):
    from src.core.library_snapshot import ProbeToken, probe_library

    char = mm.add_character(name="Probe", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "moved")
    old = Path(mod.path).parent
    moved = old.with_name("moved-container")
    old.rename(moved)
    result = probe_library(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)))
    list(mm.apply_library_probe_result(result))
    assert mm.get_mod(mod.mod_id).path == str(moved / Path(mod.path).name)


@pytest.mark.parametrize("blocked_kind", ["source", "category", "marker", "deployment"])
def test_access_error_preserves_missing_source(mm, tmp_path, monkeypatch, blocked_kind):
    from src.core.library_snapshot import ProbeToken, path_key, probe_library
    from src.utils import fs

    char = mm.add_character(name="Probe", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "denied")
    mm.set_deploy_dir(str(tmp_path / "Mods"))
    if blocked_kind != "source":
        old = Path(mod.path).parent
        moved = old.with_name("moved-container")
        old.rename(moved)
    else:
        moved = Path(mod.path).parent
    blocked = {
        "source": mod.path,
        "category": str(moved.parent),
        "marker": str(moved / ".d3dxmod"),
        "deployment": str(tmp_path / "Mods"),
    }[blocked_kind]
    original = os.stat

    def denied(path, *args, **kwargs):
        if path_key(fs.normal_path(path)) == path_key(blocked):
            raise PermissionError("inaccessible probe target")
        return original(path, *args, **kwargs)

    request = mm.create_library_probe_request(ProbeToken("game_a", 1, 1))
    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", denied)
        if blocked_kind == "marker":
            original_read = fs.read_text

            def denied_read(path, *args, **kwargs):
                if path_key(fs.normal_path(path)) == path_key(blocked):
                    raise PermissionError("inaccessible probe marker")
                return original_read(path, *args, **kwargs)

            patch.setattr(fs, "read_text", denied_read)
        result = probe_library(request)
    assert result.error
    assert list(mm.apply_library_probe_result(result)) == []
    assert mm.get_mod(mod.mod_id) is not None


def test_pure_deployment_cache_forces_marker_rescan_after_five_seconds(tmp_path):
    from src.core.library_snapshot import scan_deployment_inventory
    from src.core.mod_identity import new_mod_id, write_deployment_identity

    root = tmp_path / "Mods"
    target = root / "Skin"
    target.mkdir(parents=True)
    identity = new_mod_id()
    write_deployment_identity(target, identity, "before")
    first = scan_deployment_inventory((str(root),), now=10)
    write_deployment_identity(target, identity, "after")
    cached = scan_deployment_inventory((str(root),), first, now=14)
    assert cached is first
    refreshed = scan_deployment_inventory((str(root),), first, now=15)
    assert refreshed.locations[0].content_hash == "after"


def test_cache_cleanup_rechecks_live_references_and_inflight(mm, tmp_path):
    from src.core.library_snapshot import ProbeToken, probe_library

    char = mm.add_character(name="Probe", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "cache")
    request = mm.create_library_probe_request(ProbeToken("game_a", 1, 1))
    root = Path(request.cache_root)
    for name in (mod.mod_id, "inflight", "orphan"):
        (root / name).mkdir(parents=True)
        (root / name / "preview.png").write_bytes(b"preview")
    result = probe_library(request)
    mod.preview_images = [str(root / mod.mod_id / "preview.png")]
    mm.update_mod(mod)
    mm.register_inflight_mod_id("inflight")
    list(mm.apply_library_probe_result(result))
    assert Path(mod.preview_images[0]).exists()
    assert (root / "inflight").exists()
    assert not (root / "orphan").exists()


def test_deployment_restore_after_missing_probe_preserves_enabled_state(mm, tmp_path):
    from src.core.library_snapshot import ProbeToken, probe_library

    char = mm.add_character(name="Probe", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "deploy_restore")
    mm.set_deploy_dir(str(tmp_path / "Mods"))
    mod = mm.toggle_mod(mod.mod_id)
    target = Path(mod.deployed_path)
    backup = tmp_path / "deploy-backup"
    target.rename(backup)
    result = probe_library(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)))
    backup.rename(target)
    list(mm.apply_library_probe_result(result))
    assert mm.get_mod(mod.mod_id).is_enabled


def test_orphan_tree_cleanup_uses_worker_plan_and_yields_between_files(mm, monkeypatch):
    from src.core.library_snapshot import ProbeToken, probe_library

    request = mm.create_library_probe_request(ProbeToken("game_a", 1, 1))
    orphan = Path(request.cache_root) / "orphan"
    (orphan / "nested").mkdir(parents=True)
    for number in range(5):
        (orphan / "nested" / f"{number}.png").write_bytes(b"preview")
    result = probe_library(request)

    def recursive_cleanup_forbidden(*args, **kwargs):
        raise AssertionError("main thread must not recursively rescan/delete cache trees")

    monkeypatch.setattr(mm, "_remove_managed_path", recursive_cleanup_forbidden)
    steps = list(mm.apply_library_probe_result(result))
    assert len(steps) >= 7
    assert not orphan.exists()


@pytest.mark.parametrize("unreadable", [False, True])
def test_renamed_category_preserves_source_identity_deployment_and_preview(mm, tmp_path, monkeypatch, unreadable):
    from src.core.library_snapshot import ProbeToken, path_key, probe_library
    from src.utils import fs

    char = mm.add_character(name="Before", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "category_move")
    preview = Path(mod.path).parent / ".d3dxmeta" / "preview.png"
    preview.parent.mkdir(exist_ok=True)
    preview.write_bytes(b"preview")
    mod.preview_images = [str(preview)]
    mm.update_mod(mod)
    mm.set_deploy_dir(str(tmp_path / "Mods"))
    mod = mm.toggle_mod(mod.mod_id)
    old = Path(mod.path).parent.parent
    moved = old.with_name("After")
    old.rename(moved)
    listing = os.scandir

    def denied(path):
        if unreadable and path_key(fs.normal_path(path)) == path_key(str(moved)):
            raise PermissionError("fallback category inaccessible")
        return listing(path)

    request = mm.create_library_probe_request(ProbeToken("game_a", 1, 1))
    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", denied)
        result = probe_library(request)
    if unreadable:
        assert result.error
    else:
        assert not result.error
        assert len(result.relocated) == 1
    list(mm.apply_library_probe_result(result))
    current = mm.get_mod(mod.mod_id)
    assert current is not None
    assert current.mod_id == mod.mod_id
    assert current.is_enabled
    assert Path(current.deployed_path).exists()
    if not unreadable:
        assert Path(current.path).exists()
        assert Path(current.preview_images[0]).exists()


def test_orphan_cleanup_removes_readonly_preview(mm):
    import stat

    from src.core.library_snapshot import ProbeToken, probe_library

    request = mm.create_library_probe_request(ProbeToken("game_a", 1, 1))
    orphan = Path(request.cache_root) / "readonly"
    orphan.mkdir(parents=True)
    image = orphan / "preview.png"
    image.write_bytes(b"preview")
    image.chmod(stat.S_IREAD)
    try:
        result = probe_library(request)
        list(mm.apply_library_probe_result(result))
        assert not orphan.exists()
    finally:
        if image.exists():
            image.chmod(stat.S_IWRITE)


def test_source_restored_at_new_path_after_probe_invalidates_absence_proof(mm, tmp_path):
    from src.core.library_snapshot import ProbeToken, probe_library

    char = mm.add_character(name="Probe", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "restore_elsewhere")
    container = Path(mod.path).parent
    backup = tmp_path / "outside-library"
    container.rename(backup)
    result = probe_library(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)))
    assert not result.error
    assert result.checks[0].state == "missing"
    restored = container.with_name("restored-elsewhere")
    backup.rename(restored)
    list(mm.apply_library_probe_result(result))
    assert mm.get_mod(mod.mod_id) is not None
    fresh = probe_library(mm.create_library_probe_request(ProbeToken("game_a", 2, 2)))
    list(mm.apply_library_probe_result(fresh))
    assert mm.get_mod(mod.mod_id).path == str(restored / Path(mod.path).name)
