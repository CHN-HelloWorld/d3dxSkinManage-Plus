from pathlib import Path

from src.core.mod_variant_store import ModVariantStore
from tests.core.test_named_variant_manager import enable, save
from tests.core.test_variant_preset_switch import _KEY, _override_value, _user_keys, _write_user_ini
from tests.core.test_variant_preset_switch import env as env


def disabled_with_selection(env):
    mm, mod, current = enable(env)
    variant = save(env)
    assert mm.toggle_mod(mod.mod_id) is not None
    return mm, mm._index.get_mod(mod.mod_id), current, variant


def test_enable_uses_private_selection_in_current_preset(env):
    mm, mod, current, variant = disabled_with_selection(env)
    _write_user_ini(env["user_ini"], "9")
    result = mm.toggle_mod(mod.mod_id)
    assert result is not None and result.is_enabled
    assert current.mod_variants[mod.mod_id] == variant.variant_id
    assert _override_value(mod) == "0"
    assert _user_keys(env["user_ini"])[_KEY] == "2"


def test_finalize_enable_can_use_its_existing_deployment_reservation(env):
    mm, mod, current, variant = disabled_with_selection(env)
    target = mm._restore_parked_deployment(mod)
    if target is None:
        target = mm._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name)
    token = "deploy:" + mod.mod_id
    mm.protect_library_operation(token, mod_ids=(mod.mod_id,), paths=(mod.path, target))
    try:
        result = mm.finalize_enable(mod.mod_id, True, deploy_path=str(target))
        assert result is not None and result.is_enabled
        assert current.mod_variants[mod.mod_id] == variant.variant_id
        assert _override_value(mod) == "0"
        assert _user_keys(env["user_ini"])[_KEY] == "2"
    finally:
        mm.release_library_operation(token)


def test_corrupt_private_selection_blocks_enable_and_preserves_source(env):
    mm, mod, current, variant = disabled_with_selection(env)
    ModVariantStore(mod.path).path_for(variant.variant_id).write_text("broken", encoding="utf-8")
    result = mm.toggle_mod(mod.mod_id)
    assert result is None
    assert not mm._index.get_mod(mod.mod_id).is_enabled
    assert mod.mod_id not in current.enabled_mod_ids
    assert Path(mod.path).is_file()
