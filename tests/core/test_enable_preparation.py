import copy
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import get_ident

import pytest

from tests.core.test_named_variant_enable import disabled_with_selection
from tests.core.test_variant_preset_switch import _KEY, _user_keys, _write_user_ini
from tests.core.test_variant_preset_switch import env as env


def test_enable_without_persist_variables_only_reads_its_own_declarations(env, monkeypatch):
    from src.core import mod_variant_manager
    from src.core.deployment_enable import EnablePreparation
    from tests.core.test_named_variant_coordination import add_mod

    mm = env["mm"]
    assert mm.toggle_mod(env["mod"].mod_id).is_enabled
    target = add_mod(env, "Plain", "[Constants]\nglobal $bench = 10\n")
    staged = mm._storage.materialize_deployment(target.path, target.mod_id, target.folder_name, active=False)
    preparation = EnablePreparation(mm, target.mod_id, staged, stage_only=True)
    inspected = set()
    inspect = mod_variant_manager.inspect_effective_inis

    def observed(path):
        inspected.add(str(path))
        return inspect(path)

    monkeypatch.setattr(mod_variant_manager, "inspect_effective_inis", observed)
    preparation.run()
    result = mm.finalize_enable(
        target.mod_id,
        True,
        deploy_path=preparation.deploy_path,
        source_receipt=preparation.source_receipt,
        preparation=preparation,
    )
    assert result.is_enabled
    assert target.mod_id in mm.preset_manager.get_current("g1").enabled_mod_ids
    assert inspected == {target.path}, "a Mod without persistent variables must not scan other Mods"


def test_enable_preparation_runs_configuration_without_owner_index_access(env, monkeypatch):
    from src.core.deployment_enable import EnablePreparation

    mm, mod, current, variant = disabled_with_selection(env)
    staged = mm._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name, active=False)
    _write_user_ini(env["user_ini"], "9")
    preparation = EnablePreparation(mm, mod.mod_id, str(staged), stage_only=True)
    owner = get_ident()
    from src.core import mod_variant_manager

    inspect = mod_variant_manager.inspect_effective_inis

    def checked_inspect(*args, **kwargs):
        assert get_ident() != owner, "declaration files were scanned on the GUI thread"
        return inspect(*args, **kwargs)

    monkeypatch.setattr(mod_variant_manager, "inspect_effective_inis", checked_inspect)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(preparation.run).result(timeout=15)
    assert not mm.get_mod(mod.mod_id).is_enabled
    result = mm.finalize_enable(
        mod.mod_id,
        True,
        deploy_path=preparation.deploy_path,
        source_receipt=preparation.source_receipt,
        preparation=preparation,
    )
    assert result.is_enabled
    assert current.mod_variants[mod.mod_id] == variant.variant_id
    assert mod.mod_id in current.enabled_mod_ids
    assert _user_keys(env["user_ini"])[_KEY] == "2"


def test_unpublished_enable_configuration_can_be_rolled_back(env):
    from src.core.deployment_enable import EnablePreparation

    mm, mod, current, variant = disabled_with_selection(env)
    staged = mm._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name, active=False)
    _write_user_ini(env["user_ini"], "9")
    before = mm.preset_manager._presets_file.read_bytes()
    preparation = EnablePreparation(mm, mod.mod_id, str(staged), stage_only=True)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(preparation.run).result(timeout=15)
        assert _user_keys(env["user_ini"])[_KEY] == "2"
        assert pool.submit(preparation.rollback).result(timeout=15) == ()
    assert mm.preset_manager._presets_file.read_bytes() == before
    assert _user_keys(env["user_ini"])[_KEY] == "9"
    assert not mm.get_mod(mod.mod_id).is_enabled


@pytest.mark.parametrize("failure_point", ["references", "commit"])
def test_failed_owner_publication_preserves_disabled_record_and_preset_memory(env, monkeypatch, failure_point):
    from src.core.deployment_enable import EnablePreparation

    mm, mod, current, variant = disabled_with_selection(env)
    staged = mm._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name, active=False)
    before_record = mm._index.get_deployment(mod.mod_id)
    before_members = copy.deepcopy(current.character_mods)
    before_variants = dict(current.mod_variants)
    before_durable = copy.deepcopy(mm.preset_manager._durable_refs)
    before_resolved = copy.deepcopy(mm.preset_manager._resolved_refs)
    preparation = EnablePreparation(mm, mod.mod_id, str(staged), stage_only=True)
    preparation.run()
    refresh = mm.preset_manager.refresh_references

    def fail_after_refresh(*args, **kwargs):
        refresh(*args, **kwargs)
        raise sqlite3.OperationalError("simulated owner publication failure")

    if failure_point == "references":
        monkeypatch.setattr(mm.preset_manager, "refresh_references", fail_after_refresh)
    else:

        @contextmanager
        def failed_commit():
            mm._index._db().execute("BEGIN IMMEDIATE")
            yield
            raise sqlite3.OperationalError("simulated owner publication commit failure")

        monkeypatch.setattr(mm._index, "transaction", failed_commit)
    with pytest.raises(sqlite3.OperationalError, match="owner publication"):
        mm.finalize_enable(
            mod.mod_id,
            True,
            deploy_path=preparation.deploy_path,
            source_receipt=preparation.source_receipt,
            preparation=preparation,
        )
    assert not preparation.published
    assert preparation.rollback() == ()
    assert mm._storage.remove_deploy_path(preparation.deploy_path)
    assert mm._index.get_deployment(mod.mod_id) == before_record
    assert not mm.get_mod(mod.mod_id).is_enabled
    assert current.character_mods == before_members
    assert current.mod_variants == before_variants
    assert mm.preset_manager._durable_refs == before_durable
    assert mm.preset_manager._resolved_refs == before_resolved
