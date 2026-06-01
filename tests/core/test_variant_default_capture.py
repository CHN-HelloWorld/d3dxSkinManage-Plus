import pytest

from src.core.mod_package import seed_ini_overrides, write_override
from src.services.archive import ArchiveService
from tests.core.test_named_variant_coordination import add_mod
from tests.core.test_named_variant_manager import enable
from tests.core.test_variant_preset_switch import env as env

KEY = r"$\mods\capturedefaults\merged.ini\swapvar"


def captured_default(env, initializer="0", captured="5"):
    ini = f"[Constants]\nglobal persist $swapvar = {initializer}\npost $swapvar = {captured}\n"
    mod = add_mod(env, "CaptureDefaults", ini)
    assert seed_ini_overrides(mod.path)
    mm, mod, _ = enable(dict(env, mod=mod))
    mm.variants.apply_variant(mod.mod_id, None)
    state = mm.variants.get_state(mod.mod_id)
    assert state["status"] == "unconfirmed" and not state["can_save"]
    env["user_ini"].write_text(f"[Constants]\n{KEY} = {captured}\n", encoding="utf-8")
    assert mm.variants.collect(trigger="simulated_loader_save").changed
    return mm, mod


@pytest.mark.parametrize("initializer,captured", [("0", "5"), ("1 + 1", "2")])
def test_complete_default_capture_is_neutral_saveable_and_needs_no_literal_read(
    env, monkeypatch, initializer, captured
):
    mm, mod = captured_default(env, initializer, captured)
    mm.variants._default_cache.clear()
    original = ArchiveService.read_all_inis
    archive_reads = []

    def read_archive(service, path):
        archive_reads.append(str(path))
        return original(service, path)

    monkeypatch.setattr(ArchiveService, "read_all_inis", read_archive)
    state = mm.variants.get_state(mod.mod_id)
    assert (state["status"], state["reason"], archive_reads) == ("captured", "", [])
    assert state["selected_variant_id"] is None
    assert state["can_save"]
    assert state["confirmed_count"] == state["variable_count"] == 1
    assert state["captured_at"]
    variant = mm.variants.save_variant(mod.mod_id, "Captured author result")
    assert variant.variables[0].value == captured


def test_named_capture_still_compares_saved_values(env):
    mm, mod = captured_default(env)
    variant = mm.variants.save_variant(mod.mod_id, "Named five")
    assert mm.variants.get_state(mod.mod_id)["status"] == "clean"
    env["user_ini"].write_text(f"[Constants]\n{KEY} = 7\n", encoding="utf-8")
    mm.variants.collect(trigger="changed_named_values")
    state = mm.variants.get_state(mod.mod_id)
    assert state["selected_variant_id"] == variant.variant_id
    assert state["status"] == "modified" and state["can_save"]


@pytest.mark.parametrize("change", ["missing_value", "changed_declarations"])
def test_default_capture_loses_save_confirmation_when_incomplete_or_stale(env, change):
    mm, mod = captured_default(env)
    if change == "missing_value":
        env["user_ini"].write_text("[Constants]\n", encoding="utf-8")
        mm.variants.collect(trigger="incomplete_loader_save")
    else:
        assert write_override(
            mod.path, "merged.ini", "[Constants]\nglobal persist $swapvar = 0\nglobal persist $new_choice = 1\n"
        )
    state = mm.variants.get_state(mod.mod_id)
    assert state["status"] == "unconfirmed" and not state["can_save"]
    assert "完整确认" in state["reason"]
    with pytest.raises(ValueError, match="未能读取完整的游戏内配置"):
        mm.variants.save_variant(mod.mod_id, "Incomplete")
