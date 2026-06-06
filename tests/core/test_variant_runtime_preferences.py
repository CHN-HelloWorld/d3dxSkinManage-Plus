from pathlib import Path

import pytest

from src.core import migoto_user_ini as user_ini
from src.core.mod_package import override_path_for, write_override
from tests.core.test_named_variant_manager import enable, save
from tests.core.test_variant_preset_switch import _KEY, _write_user_ini
from tests.core.test_variant_preset_switch import env as env
from tests.helpers import apply_preset

AUTHOR_INI = (
    "[Constants]\r\nglobal persist $swapvar = 0\r\n"
    "if $swapvar == 0\r\n  $swapvar = 4\r\nendif\r\n"
    "post $swapvar = 5\r\n[KeySwap]\r\nkey = k\r\n"
)


def author_mod(env):
    assert write_override(env["mod"].path, "merged.ini", AUTHOR_INI)
    mm, mod, current = enable(env)
    paths = [Path(mod.path), override_path_for(mod.path, "merged.ini"), env["migoto"] / "Mods/AlphaSkin/merged.ini"]
    return mm, mod, current, {p: p.read_bytes() for p in paths}


def assert_originals(originals):
    assert {p: p.read_bytes() for p in originals} == originals


@pytest.mark.parametrize("selection", ["named", "default", "disabled", "preset"])
def test_application_preserves_author_code_and_writes_runtime_preferences(env, selection):
    mm, mod, current, originals = author_mod(env)
    variant = save(env)
    user_before = (f"; loader settings\r\n[Constants]\r\n{_KEY} = 9\r\n$unrelated = 8\r\n[Other]\r\nx = y\r\n").encode()
    env["user_ini"].write_bytes(user_before)
    if selection == "disabled":
        mm.toggle_mod(mod.mod_id)
        originals.pop(env["migoto"] / "Mods/AlphaSkin/merged.ini")
    if selection == "preset":
        apply_preset(mm.preset_manager, current.id)
    else:
        mm.variants.apply_variant(mod.mod_id, None if selection == "default" else variant.variant_id)
    assert_originals(originals)
    values = user_ini.read_stable(env["user_ini"]).values
    assert values.get(_KEY) == {"named": "2", "default": None, "disabled": "9", "preset": "2"}[selection]
    assert b"$unrelated = 8\r\n[Other]\r\nx = y\r\n" in env["user_ini"].read_bytes()
    if selection == "default":
        assert not mm.variants.load_prefs().state_for(mod.mod_id).confirmed_keys


@pytest.mark.parametrize("named", [False, True])
def test_absent_user_file_is_created_only_for_named_values(env, named):
    mm, mod, _, originals = author_mod(env)
    variant = save(env)
    env["user_ini"].unlink()
    mm.variants.apply_variant(mod.mod_id, variant.variant_id if named else None)
    assert env["user_ini"].exists() == named
    if named:
        assert user_ini.read_stable(env["user_ini"]).values == {_KEY: "2"}
    assert_originals(originals)


def test_duplicate_target_entries_are_all_replaced(env):
    mm, mod, _, originals = author_mod(env)
    variant = save(env)
    env["user_ini"].write_text(f"[Constants]\n{_KEY} = 3\n{_KEY.upper()} = 9\n", encoding="utf-8")
    mm.variants.apply_variant(mod.mod_id, variant.variant_id)
    snapshot = user_ini.read_stable(env["user_ini"])
    assert snapshot.values == {_KEY: "2"}
    assert not user_ini.duplicate_persisted_keys(snapshot.text)
    assert_originals(originals)


def test_runtime_missing_path_refuses_named_success(env, monkeypatch):
    mm, mod, _, originals = author_mod(env)
    variant = save(env)
    monkeypatch.setattr(mm.variants, "user_ini_path", lambda *a: None)
    with pytest.raises(ValueError, match="用户配置"):
        mm.variants.apply_variant(mod.mod_id, variant.variant_id)
    assert_originals(originals)


@pytest.mark.parametrize("named", [False, True])
def test_declarations_changed_after_planning_reject_runtime_write(env, monkeypatch, named):
    mm, mod, _, _ = author_mod(env)
    variant = save(env)
    _write_user_ini(env["user_ini"], "9")
    before = env["user_ini"].read_bytes()
    execute = mm.variants._execute

    def changed(*args, **kwargs):
        write_override(mod.path, "merged.ini", "namespace = changed\n" + AUTHOR_INI)
        return execute(*args, **kwargs)

    monkeypatch.setattr(mm.variants, "_execute", changed)
    with pytest.raises(RuntimeError, match="外部修改"):
        mm.variants.apply_variant(mod.mod_id, variant.variant_id if named else None)
    assert env["user_ini"].read_bytes() == before


@pytest.mark.parametrize(
    "encoding,bom",
    [("utf-8", b""), ("utf-8-sig", b""), ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff"), ("mbcs", b"")],
)
def test_custom_user_config_preserves_encoding_and_unrelated_bytes(env, encoding, bom):
    mm, mod, _, originals = author_mod(env)
    variant = save(env)
    env["user_ini"].unlink()
    custom = env["migoto"] / "preferences.ini"
    (env["migoto"] / "d3dx.ini").write_text("[Include]\nuser_config = preferences.ini\n", encoding="utf-8")
    text = f"[Constants]\r\n{_KEY} = 9\r\n$other = 8\n[Other]\r\n; unrelated 中文\nkey = y"
    custom.write_bytes(bom + text.encode(encoding))
    mm.variants.apply_variant(mod.mod_id, variant.variant_id)
    assert custom.read_bytes() == bom + text.replace(f"{_KEY} = 9", f"{_KEY} = 2").encode(encoding)
    mm.variants.apply_variant(mod.mod_id, None)
    assert custom.read_bytes() == bom + text.replace(f"{_KEY} = 9\r\n", "").encode(encoding)
    assert not env["user_ini"].exists()
    assert_originals(originals)


def test_mixed_named_and_default_preset_uses_one_runtime_write(env, monkeypatch):
    from src.core import variant_operation
    from tests.core.test_named_variant_coordination import add_mod

    mm, mod, current, originals = author_mod(env)
    variant = save(env)
    second = add_mod(env, "Second", AUTHOR_INI)
    from src.core.mod_package import seed_ini_overrides

    assert seed_ini_overrides(second.path)
    current.add_mod_for_character(env["char"].id, second.mod_id)
    mm.preset_manager._save()
    apply_preset(mm.preset_manager, current.id)
    second = mm._index.get_mod(second.mod_id)
    for path in (
        Path(second.path),
        override_path_for(second.path, "merged.ini"),
        Path(second.deployed_path) / "merged.ini",
    ):
        originals[path] = path.read_bytes()
    second_key = r"$\mods\second\merged.ini\swapvar"
    env["user_ini"].write_text(f"[Constants]\n{_KEY} = 9\n{second_key} = 8\n$unrelated = 7\n", encoding="utf-8")
    written = []
    write = variant_operation.atomic_write

    def record(path, payload):
        if path == env["user_ini"]:
            written.append(payload)
        return write(path, payload)

    monkeypatch.setattr(variant_operation, "atomic_write", record)
    apply_preset(mm.preset_manager, current.id)
    assert len(written) == 1
    assert user_ini.read_stable(env["user_ini"]).values == {_KEY: "2", "$unrelated": "7"}
    assert current.mod_variants[mod.mod_id] == variant.variant_id
    assert not mm.variants.load_prefs().state_for(second.mod_id).confirmed_keys
    assert_originals(originals)


def test_disabled_selection_then_enable_preserves_author_code(env):
    mm, mod, _, originals = author_mod(env)
    variant = save(env)
    mm.toggle_mod(mod.mod_id)
    _write_user_ini(env["user_ini"], "9")
    before = env["user_ini"].read_bytes()
    mm.variants.apply_variant(mod.mod_id, variant.variant_id)
    assert env["user_ini"].read_bytes() == before
    assert mm.toggle_mod(mod.mod_id).is_enabled
    assert user_ini.read_stable(env["user_ini"]).values == {_KEY: "2"}
    assert_originals(originals)


@pytest.mark.parametrize("selection", ["named", "default"])
def test_runtime_changed_after_planning_preserves_loader_edit(env, monkeypatch, selection):
    mm, mod, _, originals = author_mod(env)
    variant = save(env)
    execute = mm.variants._execute

    def loader_write(*args, **kwargs):
        _write_user_ini(env["user_ini"], "99")
        return execute(*args, **kwargs)

    monkeypatch.setattr(mm.variants, "_execute", loader_write)
    with pytest.raises(RuntimeError, match="外部修改"):
        mm.variants.apply_variant(mod.mod_id, variant.variant_id if selection == "named" else None)
    assert user_ini.read_stable(env["user_ini"]).values == {_KEY: "99"}
    assert_originals(originals)


def test_failed_private_state_write_rolls_runtime_back(env, monkeypatch):
    from src.core import variant_operation

    mm, mod, _, originals = author_mod(env)
    variant = save(env)
    _write_user_ini(env["user_ini"], "9")
    mm.variants.collect(trigger="test")
    before = env["user_ini"].read_bytes()
    write = variant_operation.atomic_write

    def fail(path, payload):
        if path == mm.variants.prefs_file():
            raise OSError("private state write failed")
        return write(path, payload)

    monkeypatch.setattr(variant_operation, "atomic_write", fail)
    with pytest.raises(OSError, match="private state"):
        mm.variants.apply_variant(mod.mod_id, variant.variant_id)
    assert env["user_ini"].read_bytes() == before
    assert_originals(originals)


@pytest.mark.parametrize("selection", ["named", "default"])
def test_unknown_user_file_state_is_not_treated_as_absent(env, monkeypatch, selection):
    mm, mod, _, originals = author_mod(env)
    variant = save(env)
    before = env["user_ini"].read_bytes()
    original = Path.stat

    def denied(path, *args, **kwargs):
        if path == env["user_ini"]:
            raise PermissionError("user file locked")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", denied)
        with pytest.raises((OSError, RuntimeError), match="locked"):
            mm.variants.apply_variant(mod.mod_id, variant.variant_id if selection == "named" else None)
    assert env["user_ini"].read_bytes() == before
    assert_originals(originals)


@pytest.mark.parametrize("selection", ["named", "default"])
def test_source_guard_uses_bytes_read_for_index(env, monkeypatch, selection):
    import src.core.mod_variant_manager as manager_module

    mm, mod, _, _ = author_mod(env)
    variant = save(env)
    _write_user_ini(env["user_ini"], "9")
    before = env["user_ini"].read_bytes()
    plan = manager_module.configuration_changes

    def change_before_planner(*args, **kwargs):
        write_override(mod.path, "merged.ini", AUTHOR_INI.replace("key = k", "key = z"))
        return plan(*args, **kwargs)

    monkeypatch.setattr(manager_module, "configuration_changes", change_before_planner)
    with pytest.raises(RuntimeError, match="外部修改"):
        mm.variants.apply_variant(mod.mod_id, variant.variant_id if selection == "named" else None)
    assert env["user_ini"].read_bytes() == before


@pytest.mark.parametrize("selection", ["named", "default"])
def test_changed_deployed_namespace_after_planning_rejects_write(env, monkeypatch, selection):
    mm, mod, _, _ = author_mod(env)
    variant = save(env)
    _write_user_ini(env["user_ini"], "9")
    before = env["user_ini"].read_bytes()
    execute = mm.variants._execute
    deployed = env["migoto"] / "Mods/AlphaSkin/merged.ini"

    def edit_deployed(*args, **kwargs):
        temporary = deployed.with_suffix(".copy")
        temporary.write_text("namespace = different\n" + AUTHOR_INI, encoding="utf-8")
        temporary.replace(deployed)
        return execute(*args, **kwargs)

    monkeypatch.setattr(mm.variants, "_execute", edit_deployed)
    with pytest.raises(RuntimeError, match="外部修改"):
        mm.variants.apply_variant(mod.mod_id, variant.variant_id if selection == "named" else None)
    assert env["user_ini"].read_bytes() == before


def test_encoding_alias_entries_are_replaced_once(env):
    mm, mod = env["mm"], env["mod"]
    assert write_override(mod.path, "merged.ini", "namespace = 页\n" + AUTHOR_INI)
    enable(env)
    key = "$\\页\\swapvar"
    env["user_ini"].write_text(f"[Constants]\n{key} = 2\n", encoding="utf-8")
    variant = mm.variants.save_variant(mod.mod_id, "Alias")
    original = override_path_for(mod.path, "merged.ini").read_bytes()
    env["user_ini"].write_text(f"[Constants]\n{key} = 9\n$\\ҳ\\swapvar = 8\n", encoding="utf-8")
    mm.variants.apply_variant(mod.mod_id, variant.variant_id)
    snapshot = user_ini.read_stable(env["user_ini"])
    assert snapshot.values == {key: "2"}
    assert override_path_for(mod.path, "merged.ini").read_bytes() == original


def test_unreadable_loader_configuration_does_not_guess_user_path(env, monkeypatch):
    from src.utils import fs

    mm, mod, _, originals = author_mod(env)
    variant = save(env)
    read = fs.read_bytes

    def denied(path):
        if Path(path) == env["migoto"] / "d3dx.ini":
            raise PermissionError("loader configuration locked")
        return read(path)

    monkeypatch.setattr(fs, "read_bytes", denied)
    with pytest.raises(PermissionError, match="configuration locked"):
        mm.variants.apply_variant(mod.mod_id, variant.variant_id)
    assert_originals(originals)


@pytest.mark.parametrize("selection", ["named", "default"])
@pytest.mark.parametrize("location", ["override", "deployment", "both"])
def test_round1_added_ini_after_planning_rejects_stale_attribution(env, monkeypatch, selection, location):
    mm, mod, _, _ = author_mod(env)
    variant = save(env)
    _write_user_ini(env["user_ini"], "9")
    before = env["user_ini"].read_bytes()
    execute = mm.variants._execute
    added = []

    def add_declaration(*args, **kwargs):
        roots = {
            "override": override_path_for(mod.path, "merged.ini").parent,
            "deployment": env["migoto"] / "Mods/AlphaSkin",
        }
        for place, root in roots.items():
            if location in (place, "both"):
                path = root / "new-conflict.ini"
                path.write_text(
                    "namespace = mods\\alphaskin\\merged.ini\n[Constants]\nglobal persist $swapvar = 123\n",
                    encoding="utf-8",
                )
                added.append(path)
        return execute(*args, **kwargs)

    monkeypatch.setattr(mm.variants, "_execute", add_declaration)
    with pytest.raises(RuntimeError, match="INI|外部修改"):
        mm.variants.apply_variant(mod.mod_id, variant.variant_id if selection == "named" else None)
    assert env["user_ini"].read_bytes() == before
    assert all(path.is_file() for path in added)
    assert not list((mm.variants.prefs_file().parent / "variant_operations").glob("*.json"))


@pytest.mark.parametrize(
    "lines",
    [
        "$\\ҳ\\swapvar = 9\n$\\页\\swapvar = 8\n",
        "$\\ҳ\\swapvar = 2\n$\\页\\swapvar = 8\n",
        "$\\ҳ\\swapvar = 2\n",
        "$\\页\\swapvar = 8\n$\\ҳ\\swapvar = 9\n",
    ],
)
def test_round1_alias_order_cannot_remove_literal_loader_target(env, lines):
    mm, mod = env["mm"], env["mod"]
    assert write_override(mod.path, "merged.ini", "namespace = 页\n" + AUTHOR_INI)
    enable(env)
    key = "$\\页\\swapvar"
    env["user_ini"].write_text(f"[Constants]\n{key} = 2\n", encoding="utf-8")
    variant = mm.variants.save_variant(mod.mod_id, "Literal key")
    unrelated = "$other = 7\r\n[Other]\r\n; untouched\n"
    env["user_ini"].write_bytes(("[Constants]\r\n" + lines + unrelated).encode("utf-8"))
    mm.variants.apply_variant(mod.mod_id, variant.variant_id)
    output = env["user_ini"].read_bytes().decode("utf-8")
    assert output.count(key + " = 2") == 1
    assert "$\\ҳ\\swapvar" not in output
    assert output.endswith(unrelated)
