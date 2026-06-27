import json
from pathlib import Path

import pytest

from src.core.deployment_enable import EnablePreparation
from src.core.mod_package import override_path_for, write_override
from tests.core.test_named_variant_manager import enable, save
from tests.core.test_variant_preset_switch import env as env


def _change_loader_root(env, availability):
    game = env["mm"]._game_service.get_game("g1")
    if availability == "unconfigured":
        game.migoto_path = ""
    elif availability == "missing":
        game.migoto_path = str(env["migoto"].parent / "old-machine/3DMigoto")
    else:
        root = env["migoto"].parent / "separate-loader"
        root.mkdir()
        (root / "d3dx.ini").write_text("[Include]\ninclude_recursive = Mods\n", encoding="utf-8")
        (root / "d3dx_user.ini").write_text("[Constants]\n$unrelated = 7\n", encoding="utf-8")
        game.migoto_path = str(root)
    return game


def _prepare(env):
    mm, mod = env["mm"], env["mod"]
    current = mm._index.get_mod(mod.mod_id)
    target = mm._restore_parked_deployment(current)
    if target is None:
        target = mm._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name)
    return EnablePreparation(mm, mod.mod_id, str(target))


@pytest.mark.parametrize("availability", ["unconfigured", "missing", "outside"])
@pytest.mark.parametrize("explicit", [False, True])
def test_new_default_enable_without_runtime_mapping_preserves_preferences(env, availability, explicit):
    mm, mod = env["mm"], env["mod"]
    if explicit:
        assert write_override(
            mod.path, "merged.ini", "namespace = author\\skin\n[Constants]\nglobal persist $swapvar = 0\n"
        )
    store = mm.variants.load_prefs()
    store.update_recent("unrelated-mod", {r"$\unrelated\v": "9"})
    assert mm.variants.save_prefs(store)
    prefs_path = mm.variants.prefs_file()
    before_prefs = prefs_path.read_bytes()
    game = _change_loader_root(env, availability)
    runtime = Path(game.migoto_path) / "d3dx_user.ini" if game.migoto_path else None
    before_runtime = runtime.read_bytes() if runtime is not None and runtime.is_file() else None
    original = {p: p.read_bytes() for p in (Path(mod.path), override_path_for(mod.path, "merged.ini"))}

    prepared = _prepare(env).run()

    assert prepared.completed
    assert prepared.outcome.skipped == (mod.mod_id,)
    assert not prepared.outcome.applied
    assert not prepared.outcome.needs_reload_hint
    record = next(item for item in prepared.records if item["id"] == prepared.snapshot.current_id)
    assert mod.mod_id in record["character_mods"][mod.node_id]
    assert record["mod_variants"][mod.mod_id] is None
    assert prefs_path.read_bytes() == before_prefs
    override = override_path_for(mod.path, "merged.ini")
    assert (Path(prepared.deploy_path) / "merged.ini").read_bytes() == original[override]
    assert {p: p.read_bytes() for p in original} == original
    if runtime is not None:
        assert (runtime.read_bytes() if runtime.is_file() else None) == before_runtime


@pytest.mark.parametrize("saved", ["working", "confirmed", "source", "named", "preset_selection"])
def test_saved_preference_enable_without_loader_root_remains_blocked(env, saved):
    mm, mod, current = enable(env)
    variant = save(env) if saved in {"named", "preset_selection"} else None
    assert mm.toggle_mod(mod.mod_id) is not None
    if saved != "named":
        store = mm.variants.load_prefs()
        state = store.state_for(mod.mod_id)
        key = r"$\mods\alphaskin\merged.ini\swapvar"
        if saved == "working":
            store.update_recent(mod.mod_id, {key: "2"})
        elif saved == "confirmed":
            state.confirmed_keys = (key,)
        elif saved == "source":
            state.sources = {key: {"path": "old-machine/d3dx_user.ini"}}
        else:
            state.selected_variant_id = None
            state.values, state.sources, state.confirmed_keys = {}, {}, ()
            current.add_mod_for_character(mod.node_id, mod.mod_id)
            current.mod_variants[mod.mod_id] = variant.variant_id
            mm.preset_manager._save()
        assert mm.variants.save_prefs(store)
    prefs_path = mm.variants.prefs_file()
    before_prefs = prefs_path.read_bytes()
    before_source = Path(mod.path).read_bytes()
    before_presets = mm.preset_manager._presets_file.read_bytes()
    game = _change_loader_root(env, "missing")

    with pytest.raises(ValueError) as raised:
        _prepare(env).run()

    assert "3DMigoto" in str(raised.value)
    assert game.migoto_path in str(raised.value)
    assert prefs_path.read_bytes() == before_prefs
    assert Path(mod.path).read_bytes() == before_source
    assert mm.preset_manager._presets_file.read_bytes() == before_presets
    assert not mm._index.get_mod(mod.mod_id).is_enabled
    expected_selection = variant.variant_id if saved == "named" else None
    assert mm.variants.load_prefs().state_for(mod.mod_id).selected_variant_id == expected_selection


@pytest.mark.parametrize("changed", ["source", "deployment", "inventory", "prefs", "new_prefs"])
def test_default_enable_without_loader_keeps_read_guards(env, monkeypatch, changed):
    mm, mod = env["mm"], env["mod"]
    prefs_path = mm.variants.prefs_file()
    if changed != "new_prefs":
        store = mm.variants.load_prefs()
        store.update_recent("unrelated-mod", {r"$\unrelated\v": "9"})
        assert mm.variants.save_prefs(store)
    _change_loader_root(env, "missing")
    prepared = _prepare(env)
    original_presets = mm.preset_manager._presets_file.read_bytes()
    execute = prepared.variants._execute
    edited = {}

    def edit_dependency(*args, **kwargs):
        if changed == "source":
            path = override_path_for(mod.path, "merged.ini")
            path.write_bytes(b"[Constants]\nglobal persist $changed = 3\n")
        elif changed == "deployment":
            path = Path(prepared.deploy_path) / "merged.ini"
            temporary = path.with_suffix(".pending")
            temporary.write_bytes(b"[Constants]\nglobal persist $deployed = 4\n")
            temporary.replace(path)
        elif changed == "inventory":
            path = Path(prepared.deploy_path) / "new.ini"
            path.write_bytes(b"[Constants]\nglobal persist $extra = 5\n")
        else:
            path = prefs_path
            path.write_text(
                json.dumps(
                    {"schema": 2, "mods": {mod.mod_id: {"values": {r"$\mods\alphaskin\merged.ini\swapvar": "7"}}}}
                ),
                encoding="utf-8",
            )
        edited[path] = path.read_bytes()
        return execute(*args, **kwargs)

    monkeypatch.setattr(prepared.variants, "_execute", edit_dependency)

    with pytest.raises(RuntimeError, match="外部修改"):
        prepared.run()

    assert edited
    assert {p: p.read_bytes() for p in edited} == edited
    assert mm.preset_manager._presets_file.read_bytes() == original_presets
    assert not mm._index.get_mod(mod.mod_id).is_enabled


@pytest.mark.parametrize("operation", ["apply_default", "restore", "preset"])
def test_explicit_preference_operations_do_not_skip_unavailable_namespace(env, operation):
    mm, mod, current = enable(env)
    _change_loader_root(env, "missing")
    originals = {
        path: path.read_bytes()
        for path in (
            mm.variants.prefs_file(),
            mm.preset_manager._presets_file,
            Path(mod.path),
            override_path_for(mod.path, "merged.ini"),
        )
    }

    with pytest.raises(ValueError):
        if operation == "apply_default":
            mm.variants.apply_variant(mod.mod_id, None)
        elif operation == "restore":
            mm.variants.restore([mod.mod_id])
        else:
            mm.preset_manager.prepare_preset_apply(current.id)

    assert {path: path.read_bytes() for path in originals} == originals


@pytest.mark.parametrize("changed_at", ["before_run", "during_index"])
def test_default_enable_preserves_external_preset_selection(env, monkeypatch, changed_at):
    mm, mod = env["mm"], env["mod"]
    _change_loader_root(env, "missing")
    prepared = _prepare(env)
    preset_path = mm.preset_manager._presets_file
    prefs_path = mm.variants.prefs_file()
    before_prefs = prefs_path.read_bytes() if prefs_path.is_file() else None
    source_path = override_path_for(mod.path, "merged.ini")
    original_source = source_path.read_bytes()
    edited = []

    def select_external_variant():
        data = json.loads(preset_path.read_text(encoding="utf-8"))
        record = next(item for item in data["presets"] if item["id"] == prepared.snapshot.current_id)
        record["character_mods"].setdefault(mod.node_id, []).append(mod.mod_id)
        record.setdefault("mod_variants", {})[mod.mod_id] = "external-variant"
        data["external_note"] = "must be preserved byte for byte"
        raw = json.dumps(data, ensure_ascii=False, indent=4).encode("utf-8")
        preset_path.write_bytes(raw)
        edited.append(raw)

    if changed_at == "before_run":
        select_external_variant()
    else:
        build_index = prepared.variants.build_index

        def edit_while_reading(*args, **kwargs):
            select_external_variant()
            return build_index(*args, **kwargs)

        monkeypatch.setattr(prepared.variants, "build_index", edit_while_reading)

    with pytest.raises((ValueError, RuntimeError)):
        prepared.run()

    assert edited
    assert preset_path.read_bytes() == edited[-1]
    assert (prefs_path.read_bytes() if prefs_path.is_file() else None) == before_prefs
    assert source_path.read_bytes() == original_source
    assert not prepared.completed
    assert not mm._index.get_mod(mod.mod_id).is_enabled
