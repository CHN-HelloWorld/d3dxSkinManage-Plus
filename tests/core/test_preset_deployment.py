from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from src.core.mod_identity import write_deployment_identity
from src.core.mod_index import ModIndex
from src.core.mod_package import build_local_manifest, write_container_marker, write_sidecar
from src.core.mod_storage import ModStorage
from src.core.preset_apply_service import PresetApplyError, PresetPlanExecutor
from src.core.preset_deployment_plan import (
    DeploymentAction,
    PresetDeploymentPlan,
    build_preset_deployment_plan,
)
from src.core.preset_manager import PresetManager
from src.models.deployment import DeploymentRecord, DeploymentState, PresetApplyJournal
from src.models.mod import Mod
from tests.helpers import apply_preset, make_mod
from tests.helpers import registered_games as registered_games

ID_A = "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"
ID_B = "bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb"


def _storage(root: Path) -> ModStorage:
    root.mkdir()
    storage = ModStorage()
    storage.set_migoto_mods_dir(root)
    return storage


def _archive_source(root: Path, name: str, members: dict[str, bytes], identity=ID_A) -> Path:
    """创建带有单个顶层目录的压缩本体，与归档服务生成的成员布局一致。"""
    container = root / name
    container.mkdir()
    body = container / f"{name}.zip"
    with zipfile.ZipFile(body, "w") as zf:
        for rel, data in members.items():
            zf.writestr(f"{name}/{rel}", data)
    write_container_marker(
        container, body.name, Mod.compute_sha1(body), mod_id=identity, imported_at="2026-10-05T10:00:00"
    )
    assert write_sidecar(body, build_local_manifest({"name": name, "info": {}}, {"mod_id": identity}))
    return body


def test_deployment_crud_and_transaction_rollback(temp_data_dir, tmp_path):
    index = ModIndex("g")
    mod = make_mod(index, mod_id=ID_A)
    assert index.add_mod(mod)
    record = DeploymentRecord(ID_A)
    index.upsert_deployment(record)
    assert index.get_deployment(ID_A) == record
    journal = PresetApplyJournal("op", "g", "a", "b", "planned", "{}")
    index.save_journal(journal)
    with pytest.raises(RuntimeError):
        with index.transaction() as conn:
            conn.execute("UPDATE deployment_records SET state='active' WHERE mod_id=?", (ID_A,))
            raise RuntimeError("inject")
    assert not index.get_mod(ID_A).is_enabled
    assert index.get_journals("g")[0].operation_id == "op"


def test_same_named_deployments_have_no_redundant_unique_name_column(temp_data_dir):
    index = ModIndex("g")
    for name, identity in [("One", ID_A), ("Two", ID_B)]:
        assert index.add_mod(make_mod(index, name, mod_id=identity))
        index.upsert_deployment(DeploymentRecord(identity))
    assert len(index.get_deployments("g")) == 2
    assert {r[1] for r in index._db().execute("PRAGMA table_info(deployment_records)")} == {
        "mod_id",
        "state",
        "actual_path",
    }


def _indexed_source(index, source, identity, node_id, name):
    import shutil

    index._db().execute(
        "INSERT INTO nodes(id,game_id,directory_name,node_type) VALUES(?,?,?,'character')",
        (node_id, index.game_id, node_id),
    )
    index._db().commit()
    destination = index.paths.node_path(node_id) / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source.parent, destination)
    return make_mod(
        index,
        name,
        mod_id=identity,
        node_id=node_id,
        body_name=source.name,
        content_hash=Mod.compute_sha1(destination / source.name),
    )


def test_strict_park_refuses_existing_disabled(tmp_path):
    root = tmp_path / "Mods"
    storage = _storage(root)
    active, disabled, _ = storage.deployment_paths(ID_A, "M")
    active.mkdir()
    disabled.mkdir()
    (active / "active.txt").write_text("a")
    (disabled / "disabled.txt").write_text("b")
    write_deployment_identity(active, ID_A)
    write_deployment_identity(disabled, ID_A)
    assert storage.park_deployment(active, ID_A) is None
    assert (active / "active.txt").exists()
    assert (disabled / "disabled.txt").exists()


def test_materialize_uses_partial_then_commits(tmp_path):
    source = _archive_source(tmp_path, "source", {"file.bin": b"payload"})
    storage = _storage(tmp_path / "Mods")
    active, disabled, partial = storage.deployment_paths(ID_A, "M")
    result = storage.materialize_deployment(source, ID_A, "M")
    assert result == active
    assert (active / "file.bin").read_bytes() == b"payload"
    assert not disabled.exists()
    assert not partial.exists()


def test_planner_rejects_duplicate_identity_and_separates_same_names(tmp_path):
    root = tmp_path / "Mods"
    a = Mod(path=str(tmp_path / "a"), folder_name="Same", mod_id=ID_A)
    duplicate = Mod(path=str(tmp_path / "a"), folder_name="Same", mod_id=ID_A)
    with pytest.raises(ValueError, match="identity|mod_id|身份"):
        build_preset_deployment_plan(
            game_id="g",
            from_preset_id="p1",
            to_preset_id="p2",
            mods=[a, duplicate],
            target_mod_ids={ID_A},
            deployments=[],
            deploy_root=root,
        )
    other = Mod(path=str(tmp_path / "b"), folder_name="Same", mod_id=ID_B)
    plan = build_preset_deployment_plan(
        game_id="g",
        from_preset_id="p1",
        to_preset_id="p2",
        mods=[a, other],
        target_mod_ids={ID_A, ID_B},
        deployments=[],
        deploy_root=root,
    )
    assert {action.mod_id for action in plan.actions} == {ID_A, ID_B}
    assert len({action.active_path for action in plan.actions}) == 2


def test_executor_rolls_back_completed_park_when_next_step_fails(tmp_path):
    root = tmp_path / "Mods"
    storage = _storage(root)
    active_a, disabled_a, partial_a = storage.deployment_paths(ID_A, "A")
    active_a.mkdir()
    write_deployment_identity(active_a, ID_A)
    active_b, disabled_b, partial_b = storage.deployment_paths(ID_B, "B")
    # 缺失 B 的停放目录，使 A 停放后的恢复步骤失败。
    plan = PresetDeploymentPlan(
        "op",
        "g",
        "p1",
        "p2",
        (
            DeploymentAction(ID_A, "", str(active_a), str(disabled_a), str(partial_a), "park"),
            DeploymentAction(ID_B, "", str(active_b), str(disabled_b), str(partial_b), "restore"),
        ),
    )
    with pytest.raises(PresetApplyError) as caught:
        PresetPlanExecutor(storage).execute(plan)
    assert not caught.value.rollback_errors
    assert active_a.is_dir()
    assert not disabled_a.exists()


def test_preset_switch_materializes_once_then_only_renames(temp_data_dir, tmp_path):
    source = _archive_source(tmp_path, "source", {"mesh.buf": b"mesh"})
    storage = _storage(tmp_path / "Mods")
    index = ModIndex("g")
    mod = _indexed_source(index, source, ID_A, "c", "M")
    assert index.add_mod(mod)
    manager = PresetManager(mod_index=index, mod_storage=storage)
    enabled = manager.create_preset("g", "enabled", {"c": [ID_A]})
    empty = manager.create_preset("g", "empty", {})

    assert apply_preset(manager, enabled.id) == [ID_A]
    active, disabled, partial = storage.deployment_paths(mod.mod_id, "M")
    identity = active.stat().st_ino
    assert active.is_dir() and not partial.exists()

    assert apply_preset(manager, empty.id) == [ID_A]
    assert disabled.stat().st_ino == identity
    assert not active.exists()

    assert apply_preset(manager, enabled.id) == [ID_A]
    assert active.stat().st_ino == identity
    assert not disabled.exists()
    assert index.get_deployment(mod.mod_id).state == DeploymentState.ACTIVE
    assert index.get_journals("g") == []


def test_preset_switch_allows_same_name_for_different_deployments(
    temp_data_dir,
    tmp_path,
):
    source_a = _archive_source(tmp_path, "source-a", {"a.bin": b"a"})
    source_b = _archive_source(tmp_path, "source-b", {"b.bin": b"b"}, ID_B)
    storage = _storage(tmp_path / "Mods")
    index = ModIndex("g")
    mod_a = _indexed_source(index, source_a, ID_A, "char-a", "Same")
    mod_b = _indexed_source(index, source_b, ID_B, "char-b", "Same")
    assert index.add_mod(mod_a)
    assert index.add_mod(mod_b)
    manager = PresetManager(mod_index=index, mod_storage=storage)
    preset_a = manager.create_preset("g", "A", {"char-a": [ID_A]})
    preset_b = manager.create_preset("g", "B", {"char-b": [ID_B]})

    assert apply_preset(manager, preset_a.id) == [ID_A]
    assert apply_preset(manager, preset_b.id) == [ID_A, ID_B]

    disabled_a = Path(index.get_deployment(ID_A).actual_path)
    active_b = Path(index.get_deployment(ID_B).actual_path)
    assert disabled_a.is_dir()
    assert active_b.is_dir()
    # 两个身份共用活动目录名；切换后该路径属于 B，A 保留在自身的停放目录。
    assert (active_b / "b.bin").read_bytes() == b"b"
    assert index.get_deployment(ID_A).state == DeploymentState.DISABLED
    assert index.get_deployment(ID_B).state == DeploymentState.ACTIVE
    assert apply_preset(manager, preset_a.id) == [ID_B, ID_A]
    assert (Path(index.get_deployment(ID_A).actual_path) / "a.bin").read_bytes() == b"a"
    assert index.get_deployment(ID_B).state == DeploymentState.DISABLED


def test_missing_disabled_deployment_blocks_switch_until_background_review(
    temp_data_dir,
    tmp_path,
):
    source = _archive_source(tmp_path, "source", {"mesh.buf": b"mesh"})
    storage = _storage(tmp_path / "Mods")
    index = ModIndex("g")
    mod = _indexed_source(index, source, ID_A, "c", "M")
    assert index.add_mod(mod)
    manager = PresetManager(mod_index=index, mod_storage=storage)
    enabled_a = manager.create_preset("g", "enabled-a", {"c": [ID_A]})
    enabled_b = manager.create_preset("g", "enabled-b", {"c": [ID_A]})
    empty = manager.create_preset("g", "empty", {})

    assert apply_preset(manager, enabled_a.id) == [ID_A]
    assert apply_preset(manager, empty.id) == [ID_A]
    _active, disabled, _partial = storage.deployment_paths(mod.mod_id, "M")
    assert storage.remove_deploy_path(disabled)

    assert apply_preset(manager, enabled_b.id) == []
    assert enabled_a.enabled_mod_ids == enabled_b.enabled_mod_ids == [ID_A]
    assert empty.enabled_mod_ids == []
    persisted = index.get_mod(ID_A)
    assert persisted is not None and not persisted.is_enabled
    assert not Path(persisted.deployed_path).exists()
    record = index.get_deployment(mod.mod_id)
    assert record is not None and record.state == DeploymentState.DISABLED
    assert record.actual_path == persisted.deployed_path
    assert index.get_current_preset_id("g") == empty.id
