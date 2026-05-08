import json
import sqlite3
from contextlib import contextmanager
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.library_schema import create_schema
from src.models.deployment import DeploymentRecord, DeploymentState
from tests.core.test_library_reconcile import mm as mm
from tests.helpers import execute_preset_plan

MID = "11111111111141118111111111111111"
SECOND = "22222222222242228222222222222222"


def test_deployment_record_has_only_the_single_identity_and_runtime_state():
    assert [f.name for f in fields(DeploymentRecord)] == ["mod_id", "state", "actual_path"]


def test_planner_roundtrip_and_duplicate_name_reservations_use_mod_id(tmp_path):
    from src.core.preset_deployment_plan import PresetDeploymentPlan, build_preset_deployment_plan

    mods = [SimpleNamespace(mod_id=mid, folder_name="same", path=str(tmp_path / mid)) for mid in (MID, SECOND)]
    plan = build_preset_deployment_plan(
        game_id="g",
        from_preset_id="old",
        to_preset_id="new",
        mods=mods,
        target_mod_ids={MID, SECOND},
        deployments=[],
        deploy_root=tmp_path,
    )
    assert {action.mod_id for action in plan.actions} == {MID, SECOND}
    assert len({action.active_path for action in plan.actions}) == 2
    assert PresetDeploymentPlan.from_json(plan.to_json()) == plan
    assert "deployment_id" not in plan.to_json()


class Index:
    def __init__(self, root):
        self.conn = sqlite3.connect(":memory:")
        create_schema(self.conn)
        self.conn.execute(
            "INSERT INTO mods(mod_id,game_id,folder_name,path,imported_at) VALUES(?,?,?,?,?)",
            (MID, "g", "box", "body.7z", "2026-10-01"),
        )
        self.conn.commit()
        self.paths = SimpleNamespace(body_path=lambda mid: root / "box/body.7z")

    def _db(self):
        return self.conn

    @contextmanager
    def transaction(self):
        with self.conn:
            yield self.conn


def manager(monkeypatch, tmp_path):
    from src.core import preset_manager

    store = SimpleNamespace(set_current_preset_id=lambda *args: None)
    monkeypatch.setattr(preset_manager, "GameConfigStore", lambda: store)
    monkeypatch.setattr(preset_manager, "get_data_dir", lambda: tmp_path)
    index = Index(tmp_path)
    return preset_manager.PresetManager(mod_index=index), index


def test_commit_only_changes_deployment_and_journal_not_source(mm, tmp_path):
    from tests.core.test_library_reconcile import _import_mod

    category = mm.add_character("Projection", node_type="character")
    mod = _import_mod(mm, category.id, tmp_path, "projection")
    mm.set_deploy_dir(str(tmp_path / "deploy"))
    manager = mm.preset_manager
    target = manager.create_preset("game_a", "Enabled", snapshot={category.id: [mod.mod_id]})
    plan = manager.prepare_preset_apply(target.id)
    completed = execute_preset_plan(manager, plan)
    connection = mm._index._db()
    before = [tuple(row) for row in connection.execute("SELECT * FROM mods")]
    trace = []
    connection.set_trace_callback(trace.append)
    try:
        assert manager.commit_preset_apply(plan, completed) == [mod.mod_id]
        assert [tuple(row) for row in connection.execute("SELECT * FROM mods")] == before
        record = mm._index.get_deployment(mod.mod_id)
        assert record.state == DeploymentState.ACTIVE
        assert Path(record.actual_path).is_dir()
        assert not any(sql.lstrip().upper().startswith("UPDATE MODS") for sql in trace)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert mm._index.get_journals() == []
    finally:
        connection.set_trace_callback(None)


def test_reference_save_retains_unresolved_id_without_identity_mapping(monkeypatch, tmp_path):
    from src.models.preset import Preset

    pm, index = manager(monkeypatch, tmp_path)
    preset = Preset(id="p", name="Preset", game_id="g", character_mods={"node": [MID, SECOND]})
    pm._presets[preset.id] = preset
    pm._save()
    saved = json.loads((tmp_path / "presets.json").read_text(encoding="utf-8"))
    assert saved["presets"][0]["character_mods"] == {"node": [MID, SECOND]}
    projection = pm._read_plan_mods("g")[0]
    assert projection.mod_id == MID
    assert projection.path == str(tmp_path / "box/body.7z")


def test_executor_rolls_back_first_park_when_later_step_fails(mm, tmp_path, monkeypatch):
    from src.core.preset_apply_service import PresetApplyError
    from tests.core.test_library_reconcile import _import_mod

    category = mm.add_character("Rollback", node_type="character")
    first = _import_mod(mm, category.id, tmp_path, "first")
    second = _import_mod(mm, category.id, tmp_path, "second")
    mm.set_deploy_dir(str(tmp_path / "deploy"))
    assert mm.toggle_mod(first.mod_id) is not None
    active = Path(mm.get_mod(first.mod_id).deployed_path)
    manager = mm.preset_manager
    target = manager.create_preset("game_a", "Second", snapshot={category.id: [second.mod_id]})
    plan = manager.prepare_preset_apply(target.id)
    assert plan.actions[0].action == "park"

    def fail_materialize(*args, **kwargs):
        raise OSError("injected extraction failure")

    monkeypatch.setattr(mm._storage, "materialize_deployment", fail_materialize)
    with pytest.raises(PresetApplyError, match="injected extraction failure") as failure:
        execute_preset_plan(manager, plan)
    manager.fail_preset_apply(plan, failure.value)
    assert active.is_dir()
    assert mm.get_mod(first.mod_id).is_enabled
    assert not mm.get_mod(second.mod_id).is_enabled
    assert not manager.has_pending_apply("game_a")


def test_reconcile_persists_only_deployment_records(monkeypatch, tmp_path):
    from src.core.deployment_reconcile import reconcile_deployment_records

    pm, index = manager(monkeypatch, tmp_path)
    mod = SimpleNamespace(mod_id=MID, path="body", is_enabled=True, deployed_path="old")
    mod.disable = lambda: setattr(mod, "is_enabled", False)
    mod.enable = lambda: setattr(mod, "is_enabled", True)
    index.game_id = "g"
    index.get_mod = lambda mid: mod
    updates = []
    index.upsert_deployment = lambda record, commit: updates.append(record)
    row = SimpleNamespace(mod_id=MID, path="body", enabled=True, deployed_path="old")
    result = reconcile_deployment_records(
        index,
        SimpleNamespace(get_deploy_dir=lambda: tmp_path),
        "g",
        rows=[row],
        locations={},
        records={MID: DeploymentRecord(MID, DeploymentState.ACTIVE, "old")},
    )
    assert result.disabled_ids == (MID,)
    assert updates == [DeploymentRecord(MID, DeploymentState.ABSENT, "")]


def test_failed_deployment_cleanup_preserves_joinable_source_and_record(monkeypatch, tmp_path):
    pm, index = manager(monkeypatch, tmp_path)
    deployed = tmp_path / "parked"
    deployed.mkdir()
    (deployed / ".d3dxmod").write_text(
        json.dumps({"kind": "d3dxSkinManage-Plus.deployment", "mod_id": MID, "content_hash": ""})
    )
    index.conn.execute("INSERT INTO deployment_records VALUES(?,?,?)", (MID, "disabled", str(deployed)))
    index.conn.commit()
    index.get_journals = lambda game: []
    index.get_deployments = lambda game: [DeploymentRecord(MID, DeploymentState.DISABLED, str(deployed))]
    pm._mod_storage = SimpleNamespace(get_deploy_dir=lambda: tmp_path, remove_deploy_path=lambda path: False)
    pm._refresh_deployments = lambda game: None
    assert pm.garbage_collect_deployments("g") == []
    assert index.conn.execute(
        "SELECT m.game_id,d.state FROM deployment_records d JOIN mods m ON m.mod_id=d.mod_id"
    ).fetchall() == [("g", "disabled")]
