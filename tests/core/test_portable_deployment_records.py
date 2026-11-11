"""验证部署持久记录随应用副本迁移，外部绝对引用和身份保护仍然有效。"""

from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.mod_identity import write_deployment_identity
from src.core.mod_index import ModIndex
from src.core.mod_package import write_container_marker
from src.core.mod_storage import ModStorage
from src.core.mod_update_paths import load_update_plan, load_update_result, store_update_plan, store_update_result
from src.core.mod_update_transaction import cleanup_update, owned_update_paths
from src.core.preset_apply_service import CompletedStep, PresetPlanExecutor
from src.core.preset_deployment_plan import DeploymentAction, PresetDeploymentPlan
from src.core.preset_manager import PresetManager
from src.models.deployment import DeploymentRecord, DeploymentState, PresetApplyJournal
from src.models.mod import Mod
from tests.helpers import make_mod, reset_game_service
from tests.helpers import registered_games as registered_games

MOD_ID = "11111111111141118111111111111111"
OTHER_ID = "22222222222242228222222222222222"
JOB_ID = "1234567890abcdef1234567890abcdef"


def _copy_application(root: Path, monkeypatch) -> Path:
    """保留原应用副本，在另一工作目录启动复制后的应用。"""
    moved = root.with_name(root.name + "-moved")
    cwd = root.with_name(root.name + "-cwd")
    shutil.copytree(root, moved)
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith("src."):
            continue
        for attribute, relative in (("get_app_dir", ""), ("get_data_dir", "data"), ("get_config_dir", "config")):
            if hasattr(module, attribute):
                monkeypatch.setattr(module, attribute, lambda rel=relative: moved / rel)
    reset_game_service()
    return moved


def _indexed_source(index: ModIndex) -> Mod:
    """创建带真实身份标记的压缩源，供部署执行器进行完整内容校验。"""
    mod = make_mod(index, "Skin", mod_id=MOD_ID, body_name="Skin.zip")
    body = Path(mod.path)
    body.parent.mkdir(parents=True)
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("Skin/mod.ini", "[Constants]\nx = 1\n")
        archive.writestr("Skin/mesh.buf", b"source")
    mod.content_hash = Mod.compute_sha1(body)
    assert write_container_marker(
        body.parent, body.name, mod.content_hash, mod_id=mod.mod_id, imported_at=mod.imported_at
    )
    assert index.add_mod(mod)
    return mod


def _active_deployment(root: Path) -> Path:
    """创建属于测试 Mod 的部署，并放置能够区分新旧副本的内容。"""
    active = root / "Skin"
    write_deployment_identity(active, MOD_ID)
    (active / "mesh.buf").write_bytes(b"original")
    return active


@pytest.mark.parametrize("external", [False, True])
def test_index_paths_and_park_follow_application_copy(temp_data_dir, monkeypatch, external):
    root = temp_data_dir
    deploy_root = root.with_name(root.name + "-external") / "Mods" if external else root / "loader/Mods"
    active = _active_deployment(deploy_root)
    index = ModIndex("Game")
    mod = _indexed_source(index)
    record = DeploymentRecord(mod.mod_id, DeploymentState.ACTIVE, str(active))
    try:
        index.upsert_deployment(record)
        assert index.get_library_rows("Game")[0].deployed_path == str(active)
        stored = index._db().execute("SELECT actual_path FROM deployment_records").fetchone()[0]
        assert stored == (str(active) if external else "loader/Mods/Skin")
        assert record.actual_path == str(active)
    finally:
        index.close()

    moved = _copy_application(root, monkeypatch)
    expected = active if external else moved / "loader/Mods/Skin"
    if not external:
        (expected / "mesh.buf").write_bytes(b"copied")
    reopened = ModIndex("Game")
    try:
        assert reopened.get_mod(MOD_ID).deployed_path == str(expected)
        assert reopened.get_deployment(MOD_ID).actual_path == str(expected)
        assert reopened.get_deployments("Game")[0].actual_path == str(expected)
        assert reopened.get_library_rows("Game")[0].deployed_path == str(expected)
        assert reopened.get_deployment_rows("Game")[0].deployed_path == str(expected)
        storage = ModStorage()
        storage.set_migoto_mods_dir(expected.parent)
        parked = storage.park_deployment(reopened.get_mod(MOD_ID).deployed_path, MOD_ID)
        assert parked is not None
        assert (parked / "mesh.buf").read_bytes() == (b"original" if external else b"copied")
        assert not expected.exists()
        if not external:
            assert (active / "mesh.buf").read_bytes() == b"original"
    finally:
        reopened.close()


def test_legacy_absolute_record_is_not_rebased_without_evidence(temp_data_dir, monkeypatch):
    active = _active_deployment(temp_data_dir / "loader/Mods")
    index = ModIndex("Game")
    _indexed_source(index)
    index._db().execute(
        "INSERT INTO deployment_records(mod_id,state,actual_path) VALUES(?,?,?)", (MOD_ID, "active", str(active))
    )
    index._db().commit()
    index.close()
    moved = _copy_application(temp_data_dir, monkeypatch)
    reopened = ModIndex("Game")
    try:
        assert reopened.get_deployment(MOD_ID).actual_path == str(active)
        assert reopened.get_mod(MOD_ID).deployed_path == str(active)
        assert reopened.get_library_rows("Game")[0].deployed_path == str(active)
        assert reopened.get_deployment_rows("Game")[0].deployed_path == str(active)
        assert (moved / "loader/Mods/Skin/mesh.buf").read_bytes() == b"original"
    finally:
        reopened.close()


@pytest.mark.parametrize("external", [False, True])
def test_preset_plan_executes_only_at_restored_paths(temp_data_dir, monkeypatch, external):
    root = temp_data_dir
    index = ModIndex("Game")
    mod = _indexed_source(index)
    index.close()
    deploy_root = root.with_name(root.name + "-external") / "Mods" if external else root / "loader/Mods"
    active = _active_deployment(deploy_root)
    disabled = active.with_name("DISABLED_" + MOD_ID[:12] + "_Skin")
    partial = disabled.with_name(disabled.name + ".partial")
    plan = PresetDeploymentPlan(
        "operation",
        "Game",
        "before",
        "after",
        (DeploymentAction(MOD_ID, mod.path, str(active), str(disabled), str(partial), "park"),),
    )
    serialized = plan.to_json()
    moved = _copy_application(root, monkeypatch)
    restored = PresetDeploymentPlan.from_json(serialized)
    expected_root = deploy_root if external else moved / "loader/Mods"
    action = restored.actions[0]
    assert action.source_path == str(moved / "data/Game/mods/Skin/Skin.zip")
    assert action.active_path == str(expected_root / active.name)
    assert action.disabled_path == str(expected_root / disabled.name)
    assert action.partial_path == str(expected_root / partial.name)
    storage = ModStorage()
    storage.set_migoto_mods_dir(expected_root)
    completed = PresetPlanExecutor(storage).execute(restored)
    assert (expected_root / disabled.name / "mesh.buf").read_bytes() == b"original"
    assert not (expected_root / active.name).exists()
    if not external:
        assert (active / "mesh.buf").read_bytes() == b"original"
    reopened = ModIndex("Game")
    try:
        manager = PresetManager(mod_index=reopened, mod_storage=storage)
        manager.commit_preset_apply(restored, completed)
        assert reopened.get_deployment(MOD_ID).state == DeploymentState.DISABLED
        stored = reopened._db().execute("SELECT actual_path FROM deployment_records").fetchone()[0]
        assert stored == (str(disabled) if external else "loader/Mods/" + disabled.name)
    finally:
        reopened.close()


@pytest.mark.parametrize("record_failure", [False, True])
def test_preset_recovery_rolls_back_only_the_copied_deployment(temp_data_dir, monkeypatch, record_failure):
    root = temp_data_dir
    index = ModIndex("Game")
    mod = _indexed_source(index)
    storage = ModStorage()
    storage.set_migoto_mods_dir(root / "loader/Mods")
    active = _active_deployment(root / "loader/Mods")
    disabled = storage.park_deployment(active, MOD_ID)
    assert disabled is not None
    partial = disabled.with_name(disabled.name + ".partial")
    plan = PresetDeploymentPlan(
        "operation",
        "Game",
        "before",
        "after",
        (DeploymentAction(MOD_ID, mod.path, str(active), str(disabled), str(partial), "park"),),
    )
    manager = PresetManager(mod_index=index, mod_storage=storage)
    index.save_journal(PresetApplyJournal("operation", "Game", "before", "after", "executing", plan.to_json()))
    steps = (CompletedStep(MOD_ID, "park", str(active), str(disabled)),)
    if record_failure:
        manager.fail_preset_apply(plan, SimpleNamespace(completed=steps, rollback_errors=("interrupted",)))
    else:
        manager.record_preset_apply_steps(plan.operation_id, steps)
    index.close()

    moved = _copy_application(root, monkeypatch)
    reopened = ModIndex("Game")
    storage = ModStorage()
    storage.set_migoto_mods_dir(moved / "loader/Mods")
    recovery = PresetManager(mod_index=reopened, mod_storage=storage)
    try:
        assert recovery.recover_incomplete_applies("Game") == []
        assert (moved / "loader/Mods/Skin/mesh.buf").read_bytes() == b"original"
        assert not (moved / "loader/Mods" / disabled.name).exists()
        assert (disabled / "mesh.buf").read_bytes() == b"original"
        assert not active.exists()
        assert reopened.get_journals("Game") == []
    finally:
        reopened.close()


@pytest.mark.parametrize("external", [False, True])
def test_update_protocol_preserves_owned_paths_after_copy(temp_data_dir, monkeypatch, external):
    root = temp_data_dir
    container = root / "data/Game/mods/Skin"
    work = container / ".d3dxmeta/update_work" / JOB_ID
    deploy_root = root.with_name(root.name + "-external") / "Mods" if external else root / "loader/Mods"
    active = _active_deployment(deploy_root)
    deploy_work = deploy_root / f"DISABLED_.update_{JOB_ID}.partial"
    body = work / "container/Skin.zip"
    staged = deploy_work / "unpacked"
    for path in (body, staged / "mesh.buf"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"prepared")
    plan = {
        "job_id": JOB_ID,
        "old_path": str(container / "Skin.zip"),
        "container": str(container),
        "archive": str(root / "data/downloads/new.zip"),
        "work_root": str(work),
        "deploy_target": str(active),
        "deploy_work": str(deploy_work),
        "old_deployed_path": str(active),
    }
    data = json.loads(json.dumps(store_update_plan(plan)))
    result = json.loads(json.dumps(store_update_result({"body": str(body), "deployment": str(staged)}, work)))
    moved = _copy_application(root, monkeypatch)
    current_work = moved / "data/Game/mods/Skin/.d3dxmeta/update_work" / JOB_ID
    current_deploy_root = deploy_root if external else moved / "loader/Mods"
    loaded = load_update_plan(data, current_work)
    prepared = load_update_result(result, current_work)
    assert loaded["old_path"] == str(moved / "data/Game/mods/Skin/Skin.zip")
    assert loaded["container"] == str(moved / "data/Game/mods/Skin")
    assert loaded["archive"] == str(moved / "data/downloads/new.zip")
    assert loaded["work_root"] == str(current_work)
    assert loaded["deploy_target"] == str(current_deploy_root / "Skin")
    assert loaded["old_deployed_path"] == str(current_deploy_root / "Skin")
    expected_deploy_work = current_deploy_root / deploy_work.name
    assert owned_update_paths(loaded) == [current_work, expected_deploy_work]
    assert Path(prepared["body"]).read_bytes() == b"prepared"
    assert prepared["deployment"] == str(expected_deploy_work / "unpacked")
    assert Path(prepared["deployment"]).joinpath("mesh.buf").read_bytes() == b"prepared"
    cleanup_update(loaded)
    assert not current_work.exists()
    assert not expected_deploy_work.exists()
    assert body.read_bytes() == b"prepared"
    assert (active / "mesh.buf").read_bytes() == b"original"
    if not external:
        assert (staged / "mesh.buf").read_bytes() == b"prepared"


@pytest.mark.parametrize("legacy", [False, True])
def test_deployment_deletion_matches_relative_and_legacy_absolute_rows(temp_data_dir, legacy):
    from src.core.library_publication import deployment_events
    from src.core.watcher import FileChange

    index = ModIndex("Game")
    _indexed_source(index)
    deploy_root = temp_data_dir / "loader/Mods"
    deploy_root.mkdir(parents=True)
    missing = deploy_root / "Skin"
    record = DeploymentRecord(MOD_ID, DeploymentState.ACTIVE, str(missing))
    index.upsert_deployment(record)
    if legacy:
        index._db().execute("UPDATE deployment_records SET actual_path=?", (str(missing),))
        index._db().commit()
    manager = SimpleNamespace(_index=index, current_game_id="Game", inflight_mod_ids=lambda: set())
    try:
        event = FileChange(str(deploy_root), 1, "deleted", str(missing))
        publication = deployment_events(manager, [event])
        assert publication.changed_ids == {MOD_ID}
        assert index.get_deployment(MOD_ID) == DeploymentRecord(MOD_ID)
    finally:
        index.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_deployment_path_claim_keeps_existing_identity_guard(temp_data_dir, legacy):
    from src.core.library_publication import deployment_events
    from src.core.watcher import FileChange

    index = ModIndex("Game")
    _indexed_source(index)
    assert index.add_mod(make_mod(index, "Other", mod_id=OTHER_ID))
    active = _active_deployment(temp_data_dir / "loader/Mods")
    index.upsert_deployment(DeploymentRecord(OTHER_ID, DeploymentState.ACTIVE, str(active)))
    if legacy:
        index._db().execute("UPDATE deployment_records SET actual_path=?", (str(active),))
        index._db().commit()
    manager = SimpleNamespace(
        _index=index,
        current_game_id="Game",
        inflight_mod_ids=lambda: set(),
        _register_ini_sync=lambda *args: None,
    )
    try:
        event = FileChange(str(active.parent), 1, "created", str(active))
        publication = deployment_events(manager, [event])
        assert not publication.changed_ids
        assert publication.review_paths == {str(active)}
        assert index.get_deployment(MOD_ID) is None
        assert index.get_deployment(OTHER_ID).actual_path == str(active)
    finally:
        index.close()


def test_copied_pending_plan_protects_its_current_deployment_ancestors(temp_data_dir, monkeypatch):
    from src.core.library_protection import path_protected, protection_snapshot

    index = ModIndex("Game")
    mod = _indexed_source(index)
    active = temp_data_dir / "loader/Mods/Skin"
    disabled = active.with_name("DISABLED_" + MOD_ID[:12] + "_Skin")
    plan = PresetDeploymentPlan(
        "operation",
        "Game",
        "before",
        "after",
        (DeploymentAction(MOD_ID, mod.path, str(active), str(disabled), str(disabled) + ".partial", "park"),),
    )
    index.save_journal(PresetApplyJournal("operation", "Game", "before", "after", "planned", plan.to_json()))
    index.close()
    moved = _copy_application(temp_data_dir, monkeypatch)
    reopened = ModIndex("Game")
    try:
        manager = SimpleNamespace(_index=reopened, inflight_mod_ids=lambda: set())
        snapshot = protection_snapshot(manager)
        assert path_protected(reopened.paths, moved / "loader", snapshot)
        assert not path_protected(reopened.paths, temp_data_dir / "loader", snapshot)
    finally:
        reopened.close()


@pytest.mark.parametrize("marker", ["matching", "foreign", "unmarked"])
def test_offline_old_root_rebinds_only_verified_current_deployments(temp_data_dir, marker):
    """只按当前根中的身份恢复部署，不把旧目录记录或当前同名文件当作已启用副本。"""
    from src.core.mod_manager import ModManager

    manager = ModManager()
    manager.set_game("Game")
    mod = _indexed_source(manager._index)
    old_path = temp_data_dir / "offline/Mods/Skin"
    manager._index._db().execute(
        "INSERT INTO deployment_records(mod_id,state,actual_path) VALUES(?,?,?)", (MOD_ID, "active", str(old_path))
    )
    manager._index._db().commit()
    root = temp_data_dir / "loader/Mods"
    current = root / ("RenamedSkin" if marker == "matching" else "Skin")
    current.mkdir(parents=True)
    (current / "mesh.buf").write_bytes(b"current")
    if marker != "unmarked":
        write_deployment_identity(current, MOD_ID if marker == "matching" else OTHER_ID, mod.content_hash)
    try:
        manager.set_deploy_dir(str(root))
        record = manager._index.get_deployment(MOD_ID)
        assert record.actual_path == (str(current) if marker == "matching" else "")
        assert record.state == (DeploymentState.ACTIVE if marker == "matching" else DeploymentState.ABSENT)
        assert (current / "mesh.buf").read_bytes() == b"current"
    finally:
        manager.stop_ini_sync()
        manager._index.close()
