import json
import shutil
from pathlib import Path

from src.core.mod_identity import write_deployment_identity
from src.core.watcher import FileChange
from src.utils.constants import CATEGORY_MARKER_FILE
from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def event(mm, kind, source, target="", *, deployment=False):
    root = Path(source).parent if deployment else mm._index.paths.roots["game_a"]
    return FileChange(str(root), 1, kind, str(source), str(target), True)


def publish_proven_delete(mm, source, mid):
    """构造发布契约输入；内核证据另有独立测试。"""
    from src.core.library_publication import _service

    root = mm._index.paths.roots["game_a"]
    info = root.stat()
    identity = (info.st_dev, info.st_ino)
    _service(mm, 1).missing_grace = 0
    return mm.apply_local_source_events(
        [
            FileChange(
                str(root),
                1,
                "deleted",
                str(source),
                native_evidence="delete_pending",
                evidence_mod_id=mid,
                batch_id=1,
                root_identity=identity,
            ),
            FileChange(str(root), 1, "batch_end", str(root), batch_id=1, root_identity=identity),
        ]
    )


def test_completed_native_batch_requests_immediate_confirmation_without_purge(mm, tmp_path, monkeypatch):
    from src.core.library_publication import _service

    mod = _import_mod(mm, mm.add_character("Immediate").id, tmp_path, "proof")
    root = mm._index.paths.roots["game_a"]
    source = mm._index.paths.container_path(mod.mod_id)
    info = root.stat()
    identity = (info.st_dev, info.st_ino)
    service = _service(mm, 1)
    service.clock = lambda: 100.0
    service.missing_grace = 60
    shutil.rmtree(source)
    monkeypatch.setattr(
        mm,
        "_purge_missing_source_mod",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("source publication must not purge synchronously")
        ),
    )
    result = mm.apply_local_source_events(
        [
            FileChange(
                str(root),
                1,
                "deleted",
                str(source),
                native_evidence="delete_pending",
                evidence_mod_id=mod.mod_id,
                batch_id=1,
                root_identity=identity,
            )
        ]
    )
    assert result.retry_after_ms is None
    result = mm.apply_local_source_events(
        [FileChange(str(root), 1, "batch_end", str(root), batch_id=1, root_identity=identity)]
    )
    assert result.retry_after_ms == 0
    assert not result.removed_ids and mm.get_mod(mod.mod_id) is not None
    assert service._missing[mod.mod_id][0] == 160.0


def test_confirmation_limits_purge_and_failure_does_not_starve_following_items(mm, tmp_path, monkeypatch):
    mods = [_import_mod(mm, mm.add_character(f"Bounded{i}").id, tmp_path, f"bounded{i}") for i in range(3)]
    for mod in mods:
        source = mm._index.paths.container_path(mod.mod_id)
        shutil.rmtree(source)
        publish_proven_delete(mm, source, mod.mod_id)
    service = mm._local_source_events
    clock = [100.0]
    service.clock = lambda: clock[0]
    calls = []
    real_purge = mm._purge_missing_source_mod

    def purge(mod, *args, **kwargs):
        calls.append(mod.mod_id)
        return False if mod.mod_id == mods[0].mod_id else real_purge(mod, *args, **kwargs)

    monkeypatch.setattr(mm, "_purge_missing_source_mod", purge)
    first = mm.confirm_local_missing()
    assert calls == [mods[0].mod_id]
    assert first.retry_after_ms == 0
    assert first.pending_missing_ids == {mod.mod_id for mod in mods}
    second = mm.confirm_local_missing()
    assert calls == [mods[0].mod_id, mods[1].mod_id]
    assert second.removed_ids == {mods[1].mod_id} and second.retry_after_ms == 0
    third = mm.confirm_local_missing()
    assert calls == [mod.mod_id for mod in mods]
    assert third.removed_ids == {mods[2].mod_id} and third.retry_after_ms == 500
    assert third.pending_missing_ids == {mods[0].mod_id}
    blocked = mm.confirm_local_missing()
    assert len(calls) == 3 and blocked.retry_after_ms == 500
    clock[0] += 0.5
    assert mm.confirm_local_missing().retry_after_ms == 500
    assert calls[-1] == mods[0].mod_id and len(calls) == 4


def test_source_marker_does_not_cascade_but_move_uses_old_node(mm, tmp_path, monkeypatch):
    first, second = mm.add_character("First"), mm.add_character("Second")
    mod = _import_mod(mm, first.id, tmp_path, "local")
    source = mm._index.paths.container_path(mod.mod_id)
    calls = []
    batches = []
    load = mm._index.get_mods_by_ids

    def batch(ids):
        batches.append(set(ids))
        return load(ids)

    monkeypatch.setattr(mm._index, "get_mods_by_ids", batch)
    monkeypatch.setattr(mm, "get_mod", lambda *_: (_ for _ in ()).throw(AssertionError("per-Mod query")))
    monkeypatch.setattr(mm.preset_manager, "cascade_move_mod", lambda *args: calls.append(args))
    mm.apply_local_source_events([event(mm, "modified", source / ".d3dxmod")])
    assert calls == []
    target = mm._index.paths.node_path(second.id) / source.name
    source.rename(target)
    result = mm.apply_local_source_events([event(mm, "moved", source, target)])
    assert result.changed_ids == {mod.mod_id}
    assert calls == [(mod.mod_id, first.id, second.id, "game_a")]
    assert mm._index.get_mod(mod.mod_id).node_id == second.id
    assert batches == [{mod.mod_id}, {mod.mod_id}]


def test_node_move_publishes_descendants_and_new_node_registers_locally(mm, tmp_path):
    node = mm.add_character("Before")
    child = mm.add_character("Child", parent_id=node.id)
    mod = _import_mod(mm, child.id, tmp_path, "nested")
    source = mm._index.paths.node_path(node.id)
    target = source.with_name("After")
    source.rename(target)
    result = mm.apply_local_source_events([event(mm, "moved", source, target)])
    assert result.changed_ids == {mod.mod_id}
    assert result.moved_node_ids == {node.id}
    assert Path(mm.get_mod(mod.mod_id).path).exists()
    added = target.parent / "New"
    added.mkdir()
    (added / CATEGORY_MARKER_FILE).write_text(json.dumps({"id": "new-node", "name": "New"}))
    result = mm.apply_local_source_events([event(mm, "created", added)])
    assert "new-node" in result.moved_node_ids
    assert mm._index.paths.node_path("new-node") == added


def test_node_move_skips_descendant_materialization_and_repairs_active_once(mm, tmp_path, monkeypatch):
    from src.models.deployment import DeploymentRecord, DeploymentState

    node = mm.add_character("Node")
    mods = [_import_mod(mm, node.id, tmp_path, f"item{i}") for i in range(3)]
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    write_deployment_identity(deployed, mods[0].mod_id, mods[0].content_hash)
    mm._index.upsert_deployment(DeploymentRecord(mods[0].mod_id, DeploymentState.ACTIVE, str(deployed)))
    source = mm._index.paths.node_path(node.id)
    target = source.with_name("Renamed")
    source.rename(target)

    def forbidden(*args, **kwargs):
        raise AssertionError("node-only move must not materialize descendant Mods")

    monkeypatch.setattr(mm, "get_mod", forbidden)
    monkeypatch.setattr(mm._index, "get_mods_by_ids", forbidden)
    associations = []
    monkeypatch.setattr(mm, "_register_ini_sync", lambda *args, **kwargs: associations.append(args))
    statements = []
    mm._index._db().set_trace_callback(statements.append)
    try:
        result = mm.apply_local_source_events([event(mm, "moved", source, target)])
    finally:
        mm._index._db().set_trace_callback(None)
    assert result.changed_ids == {mod.mod_id for mod in mods}
    assert len(associations) == 1 and associations[0][0] == mods[0].mod_id
    assert not any("UPDATE MODS " in statement.upper() for statement in statements)


def test_missing_requires_successful_local_recovery_and_avoids_global_reads(mm, tmp_path, monkeypatch):
    node = mm.add_character("Local")
    mod = _import_mod(mm, node.id, tmp_path, "deleted")
    source = mm._index.paths.container_path(mod.mod_id)
    shutil.rmtree(source)
    publish_proven_delete(mm, source, mod.mod_id)
    service = mm._local_source_events
    service.missing_grace = 0
    original = service.recover_missing
    monkeypatch.setattr(service, "recover_missing", lambda mid: False)
    assert not mm.confirm_local_missing().removed_ids
    assert mm.get_mod(mod.mod_id)
    monkeypatch.setattr(service, "recover_missing", original)
    retry_at = service._cleanup_retry_after[mod.mod_id]
    service.clock = lambda: retry_at

    def forbidden(*args, **kwargs):
        raise AssertionError("ordinary deletion cannot enumerate the library or deployment roots")

    monkeypatch.setattr(mm._index, "get_all_mods", forbidden)
    monkeypatch.setattr(mm, "_known_deploy_paths", forbidden)
    result = mm.confirm_local_missing()
    assert result.removed_ids == {mod.mod_id}
    assert mm.get_mod(mod.mod_id) is None


def test_protected_pending_does_not_starve_following_ready_target(mm, tmp_path):
    mods = [_import_mod(mm, mm.add_character(f"Protection{i}").id, tmp_path, f"protected{i}") for i in range(2)]
    for mod in mods:
        source = mm._index.paths.container_path(mod.mod_id)
        shutil.rmtree(source)
        publish_proven_delete(mm, source, mod.mod_id)
    service = mm._local_source_events
    protected = str(mm._index.paths.container_path(mods[0].mod_id))
    service.protected = lambda path: path == protected
    result = mm.confirm_local_missing()
    assert result.removed_ids == {mods[1].mod_id}
    assert result.pending_missing_ids == {mods[0].mod_id}
    assert 0 < result.retry_after_ms <= 500


def test_pending_delete_keeps_row_when_parent_review_denied(mm, tmp_path, monkeypatch):
    import src.core.library_events as events

    node = mm.add_character("Local")
    mod = _import_mod(mm, node.id, tmp_path, "denied")
    source = mm._index.paths.container_path(mod.mod_id)
    shutil.rmtree(source)
    mm.apply_local_source_events([event(mm, "deleted", source)])
    mm._local_source_events.missing_grace = 0

    def denied(path):
        raise PermissionError("parent offline")

    monkeypatch.setattr(events.os, "scandir", denied)
    assert not mm.confirm_local_missing().removed_ids
    assert mm.get_mod(mod.mod_id)


def test_deployment_duplicate_and_inflight_do_not_replace_record(mm, tmp_path):
    from src.models.deployment import DeploymentRecord, DeploymentState

    mod = _import_mod(mm, mm.add_character("Local").id, tmp_path, "deployed")
    first, duplicate = tmp_path / "Mods" / "First", tmp_path / "Mods" / "Duplicate"
    for path in (first, duplicate):
        path.mkdir(parents=True)
        write_deployment_identity(path, mod.mod_id, mod.content_hash)
    mm._index.upsert_deployment(DeploymentRecord(mod.mod_id, DeploymentState.ACTIVE, str(first)))
    result = mm.apply_local_deployment_events([event(mm, "created", duplicate, deployment=True)])
    assert result.review_paths and not result.changed_ids
    assert mm._index.get_deployment(mod.mod_id).actual_path == str(first)
    mm.register_inflight_mod_id(mod.mod_id)
    shutil.rmtree(first)
    result = mm.apply_local_deployment_events([event(mm, "deleted", first, deployment=True)])
    assert not result.changed_ids
    assert mm._index.get_deployment(mod.mod_id).actual_path == str(first)


def test_missing_source_cleans_verified_deployment_without_inventory_scan(mm, tmp_path, monkeypatch):
    mod = _import_mod(mm, mm.add_character("Local").id, tmp_path, "cleanup")
    mm.set_deploy_dir(str(tmp_path / "Mods"))
    mod = mm.toggle_mod(mod.mod_id)
    target = Path(mod.deployed_path)
    source = mm._index.paths.container_path(mod.mod_id)
    shutil.rmtree(source)
    publish_proven_delete(mm, source, mod.mod_id)
    mm._local_source_events.missing_grace = 0
    monkeypatch.setattr(
        mm,
        "_known_deploy_paths",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("exact deployment cleanup must not scan roots")),
    )
    assert mm.confirm_local_missing().removed_ids == {mod.mod_id}
    assert not target.exists()


def test_delete_then_local_recovery_claims_rename_before_cleanup(mm, tmp_path):
    mod = _import_mod(mm, mm.add_character("Local").id, tmp_path, "recovered")
    source = mm._index.paths.container_path(mod.mod_id)
    target = source.with_name("Recovered")
    source.rename(target)
    mm.apply_local_source_events([event(mm, "deleted", source)])
    mm._local_source_events.missing_grace = 0
    result = mm.confirm_local_missing()
    assert not result.removed_ids and mod.mod_id in result.pending_missing_ids
    # Windows 跨类别移动也会报告删除动作，目标证据独立到达并认领已有身份。
    result = mm.apply_local_source_events([event(mm, "created", target)])
    assert mod.mod_id in result.changed_ids
    assert mm._index.paths.container_path(mod.mod_id) == target


def test_real_event_and_metadata_refresh_support_paths_over_275_characters(mm, tmp_path):
    import os

    from src.ui.library_metadata import capture_request, publish_metadata, read_metadata
    from src.utils import fs

    mod = _import_mod(mm, mm.add_character("Short").id, tmp_path, "long_path")
    source = mm._index.paths.container_path(mod.mod_id)
    root = mm._index.paths.roots["game_a"]
    first, second = "a" * 90, "b" * 90
    conn = mm._index._db()
    conn.execute("INSERT INTO nodes(id,game_id,directory_name) VALUES(?,?,?)", ("long-a", "game_a", first))
    conn.execute(
        "INSERT INTO nodes(id,game_id,parent_id,directory_name) VALUES(?,?,?,?)",
        ("long-b", "game_a", "long-a", second),
    )
    conn.commit()
    destination = root / first / second / source.name
    fs.ensure_dir(destination.parent)
    os.replace(fs.native_path(source), fs.native_path(destination))
    assert len(str(destination / Path(mod.path).name)) > 275
    result = mm.apply_local_source_events([event(mm, "moved", source, destination)])
    assert result.changed_ids == {mod.mod_id}
    assert not result.review_paths
    current = mm.get_mod(mod.mod_id)
    assert current.node_id == "long-b" and not current.path.startswith("\\\\?\\")
    # 未变化的投影返回 False；先修改实际数据，以验证长路径发布。
    sidecar = destination / ".d3dxmeta" / "info.json"
    metadata = json.loads(fs.read_text(sidecar, encoding="utf-8"))
    metadata["name"] = "Updated at a long path"
    fs.write_text(sidecar, json.dumps(metadata), encoding="utf-8")
    result = read_metadata(capture_request(mm, mod.mod_id), mm._find_previews)
    assert publish_metadata(mm, result)
    assert mm.get_mod(mod.mod_id).name == "Updated at a long path"
    assert not publish_metadata(mm, read_metadata(capture_request(mm, mod.mod_id), mm._find_previews))


def test_confirmed_copy_discovery_plans_external_and_consumes_whole_container(mm, tmp_path, qtbot):
    from src.core.mod_intake import ModIntakeService
    from src.ui.library_discovery import LibraryDiscoveryController
    from src.ui.library_reconcile_worker import LibraryReconcileController

    mod = _import_mod(mm, mm.add_character("Copies").id, tmp_path, "original")
    source = mm._index.paths.container_path(mod.mod_id)
    copy = source.with_name("Copy")
    shutil.copytree(source, copy)
    delta = mm.apply_local_source_events([event(mm, "created", copy)])
    assert delta.external_copy_paths == delta.discoveries == {str(copy)}
    reports = []
    probe = LibraryReconcileController(mm, busy=lambda: False)
    controller = LibraryDiscoveryController(
        mm, probe, busy=lambda: False, consume=lambda report, options: reports.append(report)
    )
    try:
        controller.schedule(
            "game_a", trigger="watcher", changed_paths=delta.discoveries, external_copy_paths=delta.external_copy_paths
        )
        qtbot.waitUntil(lambda: bool(reports), timeout=5000)
        (request,) = reports[0].intake_requests
        assert request.external_copy and request.context == "external" and request.move_source
        assert request.source_path == copy
        service = ModIntakeService(mm)
        prepared = service.prepare(request)
        result = service.commit(prepared)
        assert result.mod_id != mod.mod_id and result.content_hash == mod.content_hash
        assert not copy.exists() and source.exists()
    finally:
        controller.stop()
        probe.stop()
        qtbot.waitUntil(lambda: controller.worker is None and probe.worker is None)
