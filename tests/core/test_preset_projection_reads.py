import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm
from tests.helpers import registered_games as registered_games


@pytest.mark.parametrize("damage", ["not_database", "malformed"])
def test_saved_presets_do_not_block_corrupt_index_startup(temp_data_dir, damage):
    from src.core.mod_manager import ModManager

    identity = "123456781234423482341234567890ab"
    presets_file = temp_data_dir / "data" / "presets.json"
    presets_file.write_text(
        json.dumps(
            {
                "version": 2,
                "presets": [
                    {
                        "id": "saved",
                        "name": "Saved",
                        "game_id": "game_a",
                        "character_mods": {"character": [identity]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    original_presets = presets_file.read_bytes()
    db = temp_data_dir / "data" / "mod_index.db"
    damaged = b"deliberately damaged SQLite database"
    if damage == "malformed":
        with sqlite3.connect(db) as connection:
            connection.execute("CREATE TABLE broken (id INTEGER)")
        connection.close()
        damaged = bytearray(db.read_bytes())
        damaged[100] = 255
    db.write_bytes(damaged)

    manager = ModManager()
    try:
        pm = manager.preset_manager
        assert manager.index_needs_recovery() == "corrupt"
        assert pm.get_preset("saved").enabled_mod_ids == []
        assert pm._durable_refs["saved"] == {"character": [identity]}
        assert db.read_bytes() == damaged
        assert presets_file.read_bytes() == original_presets
        pm.update_preset_meta("saved", description="Awaiting index recovery")
        saved = json.loads(presets_file.read_text(encoding="utf-8"))
        assert saved["presets"][0]["character_mods"] == {"character": [identity]}
        assert db.read_bytes() == damaged
    finally:
        manager._index.close()


def test_save_with_unavailable_index_retains_resolved_and_unresolved_refs(mm, tmp_path):
    char = mm.add_character(name="References", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "reference")
    pm = mm.preset_manager
    preset = pm.create_preset("game_a", "Saved", {char.id: [mod.mod_id]})
    unresolved = "123456781234423482341234567890ab"
    pm._durable_refs[preset.id][char.id].append(unresolved)
    mm._index.close()

    pm.update_preset_meta(preset.id, description="Still retained")

    saved = json.loads(pm._presets_file.read_text(encoding="utf-8"))
    record = next(item for item in saved["presets"] if item["id"] == preset.id)
    assert record["description"] == "Still retained"
    assert set(record["character_mods"]) == {char.id}
    assert sorted(record["character_mods"][char.id]) == sorted([unresolved, mod.mod_id])


def test_capture_references_reuses_explicit_snapshot(mm, tmp_path, monkeypatch):
    char = mm.add_character(name="References", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "reference")
    pm = mm.preset_manager
    preset = pm.create_preset("game_a", "Snapshot", {char.id: [mod.mod_id]})
    before = mod.to_dict()

    def forbidden(*args):
        raise AssertionError("snapshot capture must not refetch full Mods")

    monkeypatch.setattr(mm._index, "get_all_mods", forbidden)
    pm._capture_references(preset, [mod])
    assert pm._durable_refs[preset.id] == {char.id: [mod.mod_id]}
    assert mod.to_dict() == before


def test_rebuild_refresh_reuses_projection_and_preserves_unresolved_refs(mm, tmp_path, monkeypatch):
    char = mm.add_character(name="Before", node_type="character")
    moved = mm.add_character(name="After", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "remap")
    pm = mm.preset_manager
    preset = pm.create_preset("game_a", "Saved", {char.id: [mod.mod_id]})
    unresolved = "123456781234423482341234567890ab"
    pm._durable_refs[preset.id][char.id].append(unresolved)
    mm._index.clear_mods_for_game("game_a")
    mod.node_id = moved.id
    mod.path = str(mm._index.paths.node_path(moved.id) / mod.folder_name / __import__("pathlib").Path(mod.path).name)
    assert mm._index.add_mod(mod)
    statements = []
    mm._index._db().set_trace_callback(statements.append)

    def forbidden(*args):
        raise AssertionError("preset rebuilding must not decode full Mod objects")

    monkeypatch.setattr(mm._index, "_row_to_mod", forbidden)
    try:
        assert pm.refresh_after_rebuild("game_a") == 1
    finally:
        mm._index._db().set_trace_callback(None)
    assert preset.character_mods == {moved.id: [mod.mod_id]}
    assert pm._durable_refs[preset.id] == {char.id: [unresolved], moved.id: [mod.mod_id]}
    assert sum("SELECT m.mod_id,m.node_id,m.folder_name" in sql for sql in statements) == 1


def test_post_commit_decodes_only_active_action_representatives(mm, tmp_path, monkeypatch):
    char = mm.add_character(name="References", node_type="character")
    active = _import_mod(mm, char.id, tmp_path, "active")
    parked = _import_mod(mm, char.id, tmp_path, "parked")
    _import_mod(mm, char.id, tmp_path, "unrelated")
    calls, registrations = [], []
    original = mm._index._row_to_mod

    def decode(row, *args):
        calls.append(row["mod_id"])
        return original(row, *args)

    monkeypatch.setattr(mm._index, "_row_to_mod", decode)
    mm._variants = SimpleNamespace(on_preset_apply_after_commit=lambda plan: None)
    monkeypatch.setattr(mm, "_register_ini_sync", lambda *args: registrations.append(args))
    plan = SimpleNamespace(
        game_id="game_a",
        actions=[
            SimpleNamespace(action="restore", mod_id=active.mod_id, active_path="deployed"),
            SimpleNamespace(action="park", mod_id=parked.mod_id),
        ],
    )
    mm._after_preset_deployment_commit(plan)
    assert calls == [active.mod_id]
    assert registrations == [(active.mod_id, active.path, "deployed", (active.mod_id,))]


def test_save_reuses_scoped_identity_maps_and_retains_unresolved_refs(mm, tmp_path, monkeypatch):
    char = mm.add_character(name="References", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "reference")
    pm = mm.preset_manager
    preset = pm.create_preset("game_a", "First", {char.id: [mod.mod_id]})
    pm.create_preset("game_a", "Second", {char.id: [mod.mod_id]})
    unresolved = "a" * 32
    pm._durable_refs[preset.id][char.id].append(unresolved)
    statements = []
    mm._index._db().set_trace_callback(statements.append)

    def forbidden(*args):
        raise AssertionError("saving preset references must not materialize full Mods")

    monkeypatch.setattr(mm._index, "get_all_mods", forbidden)
    try:
        pm._save()
        assert set(pm._durable_refs[preset.id][char.id]) == {mod.mod_id, unresolved}
        assert not any("SELECT" in sql for sql in statements)
    finally:
        mm._index._db().set_trace_callback(None)


def test_prepare_and_variant_members_do_not_decode_unrelated_mods(mm, tmp_path, monkeypatch):
    char = mm.add_character(name="Planning", node_type="character")
    target = _import_mod(mm, char.id, tmp_path, "target")
    _import_mod(mm, char.id, tmp_path, "unrelated")
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    mm.set_deploy_dir(str(deploy))
    pm = mm.preset_manager
    preset = pm.create_preset("game_a", "Target", {char.id: [target.mod_id]})

    def forbidden(*args):
        raise AssertionError("preset planning and membership need no full Mod objects")

    monkeypatch.setattr(mm._index, "get_all_mods", forbidden)
    plan = pm.prepare_preset_apply(preset.id)
    assert len(plan.actions) == 1
    assert plan.actions[0].source_path == target.path
    assert plan.actions[0].mod_id == target.mod_id
    assert mm.variants._preset_members("game_a", preset.id) == {target.mod_id}
    assert mm.variants._preset_members("game_b", preset.id) == set()


@pytest.mark.parametrize("invalid", ["identity", "folder", "parked_name"])
def test_unused_plan_sources_still_validate_identity_and_names(tmp_path, invalid):
    from src.core.preset_deployment_plan import build_preset_deployment_plan
    from src.models.deployment import DeploymentRecord, DeploymentState
    from src.models.mod import Mod

    identity = "123456781234423482341234567890ab"
    mod = Mod(mod_id=identity, path=str(tmp_path / "source"), folder_name="Valid")
    mods, records = [mod], []
    if invalid == "identity":
        mods.append(Mod(mod_id=identity, path="other", folder_name="Other"))
    elif invalid == "folder":
        mod.folder_name = "../bad"
    else:
        records = [DeploymentRecord(identity, state=DeploymentState.DISABLED, actual_path=str(tmp_path / "DISABLED_"))]
    with pytest.raises(ValueError):
        build_preset_deployment_plan(
            game_id="game_a",
            from_preset_id="a",
            to_preset_id="b",
            mods=mods,
            target_mod_ids=set(),
            deployments=records,
            deploy_root=tmp_path,
        )


def test_unselected_absent_records_do_not_construct_deployment_paths(tmp_path, monkeypatch):
    from pathlib import Path

    from src.core.preset_deployment_plan import build_preset_deployment_plan
    from src.models.deployment import DeploymentRecord, DeploymentState
    from src.models.mod import Mod

    identity = "123456781234423482341234567890ab"
    mod = Mod(mod_id=identity, path="source", folder_name="Unused")
    record = DeploymentRecord(identity, state=DeploymentState.ABSENT)
    joins = []
    original = Path.__truediv__

    def join(path, name):
        if path == tmp_path:
            joins.append(name)
        return original(path, name)

    monkeypatch.setattr(Path, "__truediv__", join)
    plan = build_preset_deployment_plan(
        game_id="game_a",
        from_preset_id="a",
        to_preset_id="b",
        mods=[mod],
        target_mod_ids=set(),
        deployments=[record],
        deploy_root=tmp_path,
    )
    assert plan.actions == ()
    assert joins == []
