from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from tests.core.test_variant_preset_switch import env as env
from tests.helpers import apply_preset


def _make_enabled_mods(tmp_path, count):
    from src.core.config import Config
    from src.core.mod_manager import ModManager
    from tests.helpers import register_games, reset_game_service

    register_games("game_a")
    reset_game_service()
    Config().set("import.format", "zip")
    manager = ModManager()
    manager.set_game("game_a")
    node = manager.add_character("Character", node_type="character")
    root = tmp_path / "deploy"
    manager.set_deploy_dir(str(root))
    mods = []
    for i in range(count):
        source = tmp_path / f"Skin{i}"
        source.mkdir()
        (source / "mod.ini").write_text(f"[Constants]\nx={i}\n")
        item = manager.import_mod(str(source), node.id)
        assert item is not None
        item = manager.toggle_mod(item.mod_id)
        assert item.is_enabled
        mods.append(item)
    return manager, mods, root


def test_explicit_preparation_ids_do_not_materialize_other_mods(temp_data_dir, tmp_path, monkeypatch):
    from src.core.deployment_preparation import DeploymentPreparation

    mm, mods, root = _make_enabled_mods(tmp_path, 2)
    try:

        def forbidden(*args, **kwargs):
            raise AssertionError("scoped preparation read the full Mod library")

        monkeypatch.setattr(mm._index, "get_all_mods", forbidden)
        prepared = DeploymentPreparation(mm, trigger="mod_disable", mod_ids=[mods[0].mod_id])
        assert [m.mod_id for m in prepared.mods] == [mods[0].mod_id]
    finally:
        mm.stop_ini_sync()


def test_background_preparation_uses_detached_data_and_owner_commits(
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    mm, mods, root = _make_enabled_mods(tmp_path, 2)
    try:
        preparation = mm.create_deployment_preparation(trigger="mod_disable")
        # 后台线程访问所有者的 SQLite 索引时立即失败。
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(preparation.prepare).result(timeout=5)

        def unexpected(*args, **kwargs):
            raise AssertionError("file scan/variant collection repeated on GUI thread")

        monkeypatch.setattr(mm._storage, "scan_deployments", unexpected)
        monkeypatch.setattr(mm.variants, "collect", unexpected)
        entries = mm.prepare_disable_batch([m.mod_id for m in mods], preparation=preparation)
        assert len(entries) == 2
        with ThreadPoolExecutor(max_workers=1) as pool:
            results = list(pool.map(mm.disable_batch_step, entries))
        assert len(mm.commit_disable_batch(results)) == 2
        assert all(not mm.get_mod(mod.mod_id).is_enabled for mod in mods)
    finally:
        mm.stop_ini_sync()


def _prepare_and_run(mm, target):
    from src.ui.preset_deploy_worker import PresetDeployWorker

    preparation = mm.create_deployment_preparation(trigger="preset_apply_before_io")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(preparation.prepare).result(timeout=5)
    plan = mm.preset_manager.prepare_preset_apply(target.id, preparation=preparation)
    worker = PresetDeployWorker(plan, mm._storage, preparation=preparation)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(worker.run).result(timeout=5)
    assert worker.error is None
    return preparation, plan, worker.completed


def test_detached_variant_switch_and_rollback_preserve_explicit_references(env):
    from src.core.preset_apply_service import PresetApplyError
    from tests.core.test_variant_preset_switch import _KEY, _override_value, _user_keys, _write_user_ini

    mm, mod, char = env["mm"], env["mod"], env["char"]
    pm = mm.preset_manager
    p1 = pm.create_preset("g1", "First", snapshot={char.id: [mod.mod_id]})
    p2 = pm.create_preset("g1", "Second", snapshot={char.id: [mod.mod_id]})
    apply_preset(pm, p1.id)
    _write_user_ini(env["user_ini"], "2")
    first_variant = mm.variants.save_variant(mod.mod_id, "First appearance")
    assert p1.mod_variants[mod.mod_id] == first_variant.variant_id
    preparation, plan, completed = _prepare_and_run(mm, p2)
    assert _override_value(mod) == "0"
    assert _user_keys(env["user_ini"]) == {}
    affected = pm.commit_preset_apply(plan, completed, preparation=preparation)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(preparation.after_commit, plan).result(timeout=5)
    preparation.publish_ini_refs(mm, plan)
    pm.finish_prepared_apply(plan, affected, preparation)
    _write_user_ini(env["user_ini"], "3")
    second_variant = mm.variants.save_variant(mod.mod_id, "Second appearance")
    before = env["user_ini"].read_bytes()
    preparation, plan, completed = _prepare_and_run(mm, p1)
    assert _override_value(mod) == "0"
    assert _user_keys(env["user_ini"])[_KEY] == "2"
    with ThreadPoolExecutor(max_workers=1) as pool:
        errors = pool.submit(preparation.rollback, plan, completed).result(timeout=5)
    assert errors == ()
    pm.fail_preset_apply(plan, PresetApplyError("database busy", completed=completed), preparation=preparation)
    assert env["user_ini"].read_bytes() == before
    assert pm.get_current("g1").id == p2.id
    assert p2.mod_variants[mod.mod_id] == second_variant.variant_id
    assert not pm.has_pending_apply("g1")


def test_preset_preflight_and_variant_io_can_finish_before_owner_commit(
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    from src.ui.preset_deploy_worker import PresetDeployWorker

    mm, mods, root = _make_enabled_mods(tmp_path, 1)
    try:
        target = mm.preset_manager.create_preset("game_a", "Empty")
        preparation = mm.create_deployment_preparation(trigger="preset_apply_before_io")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(preparation.prepare).result(timeout=5)
        plan = mm.preset_manager.prepare_preset_apply(target.id, preparation=preparation)
        worker = PresetDeployWorker(plan, mm._storage, preparation=preparation)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(worker.run).result(timeout=5)
        assert worker.error is None
        assert mm.get_mod(mods[0].mod_id).is_enabled
        assert mm.preset_manager.has_pending_apply("game_a")
        mm.preset_manager.commit_preset_apply(plan, worker.completed, preparation=preparation)
        assert not mm.get_mod(mods[0].mod_id).is_enabled
        assert not mm.preset_manager.has_pending_apply("game_a")
        mm.preset_manager.finish_prepared_apply(plan, [mods[0].mod_id], preparation)
    finally:
        mm.stop_ini_sync()


def test_prepared_preset_reuses_captured_rows_and_reads_records_once(
    temp_data_dir,
    tmp_path,
    monkeypatch,
):
    mm, mods, root = _make_enabled_mods(tmp_path, 2)
    try:
        pm = mm.preset_manager
        target = pm.create_preset("game_a", "Empty")
        preparation = mm.create_deployment_preparation(trigger="preset_apply_before_io")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(preparation.prepare).result(timeout=5)

        def unexpected(*args, **kwargs):
            raise AssertionError("prepared operation refetched the full library projection")

        monkeypatch.setattr(mm._index, "get_library_rows", unexpected)
        monkeypatch.setattr(pm, "_read_plan_mods", unexpected)
        statements = []
        mm._index._db().set_trace_callback(statements.append)
        try:
            plan = pm.prepare_preset_apply(target.id, preparation=preparation)
        finally:
            mm._index._db().set_trace_callback(None)
        assert len(plan.actions) == 2
        assert {action.action for action in plan.actions} == {"park"}
        assert sum("SELECT d.* FROM deployment_records" in sql for sql in statements) == 1
        mm._index.delete_journal(plan.operation_id)
    finally:
        mm.stop_ini_sync()
