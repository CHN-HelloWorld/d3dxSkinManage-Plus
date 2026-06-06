from pathlib import Path

import pytest

from src.core.library_protection import LibraryProtection
from tests.core.test_named_variant_manager import enable, save
from tests.core.test_variant_preset_switch import env as env


def test_variant_reservation_is_atomic_with_existing_worker_protection(tmp_path):
    registry = LibraryProtection()
    registry.protect("update", mod_ids=("mod",))
    with pytest.raises(RuntimeError, match="正在|占用"):
        with registry.variant_operation("variant", mod_ids=("mod",)):
            pytest.fail("overlapping update admitted")
    registry.release("update")
    with registry.variant_operation("variant", mod_ids=("mod",)):
        with pytest.raises(RuntimeError, match="正在|占用"):
            registry.protect("new-update", mod_ids=("mod",))
    registry.protect("new-update", mod_ids=("mod",))


def test_ancestor_move_conflicts_but_unrelated_mod_can_continue(tmp_path):
    registry = LibraryProtection()
    with registry.variant_operation("variant", paths=(tmp_path / "mods" / "A",), mod_ids=("a",)):
        with pytest.raises(RuntimeError):
            registry.protect("move-category", paths=(tmp_path / "mods",))
        registry.protect("other", paths=(tmp_path / "other",), mod_ids=("b",))
    assert registry.snapshot()[1] == {"b"}


def test_exclusive_reservation_does_not_inherit_command_authority():
    registry = LibraryProtection()
    with registry.operation("update", mod_ids=("mod",)):
        with pytest.raises(RuntimeError, match="正在|占用"):
            registry.protect("preset", mod_ids=("mod",), exclusive=True)
    assert registry.snapshot()[1] == set()


def test_delete_during_variant_command_leaves_deployment_and_source_intact(env):
    mm, mod, _ = enable(env)
    deployment = Path(mm._index.get_mod(mod.mod_id).deployed_path)
    with mm.variants._command([mod.mod_id]):
        assert not mm.delete_mod(mod.mod_id)
        assert deployment.is_dir()
        assert Path(mod.path).is_file()
    assert mm._index.get_mod(mod.mod_id).is_enabled


def test_variant_reservation_precedes_reentrant_source_check(env):
    mm, mod, _ = enable(env)
    attempted = []

    def preflight(check):
        attempted.append(mm.delete_mod(mod.mod_id))
        return check()

    mm.operation_check_runner = preflight
    variant = save(env)
    assert attempted == [False]
    assert variant.name == "白色"
    assert Path(mod.path).is_file()


def test_mod_operations_keep_keyword_argument_api(env):
    mm, mod, _ = enable(env)
    renamed = mm.rename_mod(mod_id=mod.mod_id, new_display_name="新名称", sync_folder=False)
    assert renamed.display_name == "新名称"
