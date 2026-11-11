"""验证旧 Mod 命名、加载器排除规则和外观偏好之间的兼容性。"""

from dataclasses import replace
from pathlib import Path

import pytest

from src.core import migoto_user_ini as user_ini
from src.core.deployment_enable import EnablePreparation
from src.core.mod_package import override_path_for, write_override
from src.core.mod_variant_manager import ModVariantManager
from src.core.persist_vars import (
    ModIniSource,
    build_attribution_index,
    declaration_fingerprint,
    resolve_variant_values,
    snapshot_variables,
)
from src.core.preset_deployment_plan import build_preset_deployment_plan
from src.models.mod_variant import ModVariant
from tests.core.test_enable_without_runtime_namespace import _prepare
from tests.core.test_named_variant_manager import enable, save
from tests.core.test_variant_preset_switch import env as env

_ABSOLUTE_INI = "namespace = C:\\Users\\Author\\Mod Launcher\\Mods\\Sample\n[Constants]\nglobal persist $swapvar = 0\n"
_ABSOLUTE_KEY = r"$\c:\users\author\mod launcher\mods\sample\swapvar"


def test_absolute_namespace_can_roundtrip_values_and_saved_variant():
    index = build_attribution_index([ModIniSource("sample", (("mod.ini", _ABSOLUTE_INI),))])
    values = {_ABSOLUTE_KEY: "2"}
    variant = ModVariant(
        "saved",
        "Saved",
        snapshot_variables(index, "sample", values),
        declaration_fingerprint=declaration_fingerprint(index, "sample"),
    )

    restored = ModVariant.from_dict(variant.to_dict())

    assert restored.variables[0].namespace == r"c:\users\author\mod launcher\mods\sample"
    assert resolve_variant_values(index, "sample", restored) == values
    assert user_ini.parse_persisted_values(user_ini.apply_values("[Constants]\n", values)) == values


@pytest.mark.parametrize("background", [False, True])
def test_absolute_namespace_enables_without_rewriting_mod(env, background):
    manager, mod = env["mm"], env["mod"]
    assert write_override(mod.path, "merged.ini", _ABSOLUTE_INI)
    source = Path(mod.path).read_bytes()
    override = override_path_for(mod.path, "merged.ini")
    before = override.read_bytes()

    if background:
        prepared = _prepare(env).run()
        assert prepared.completed
        target = Path(prepared.deploy_path)
    else:
        enabled = manager.toggle_mod(mod.mod_id)
        assert enabled is not None and enabled.is_enabled
        target = Path(enabled.deployed_path)

    assert (target / "merged.ini").read_bytes() == before
    assert override.read_bytes() == before
    assert Path(mod.path).read_bytes() == source


@pytest.mark.parametrize("existing", [False, True])
def test_legacy_disabled_name_is_usable_when_enabled(env, existing):
    manager, mod = env["mm"], env["mod"]
    storage = manager._storage
    target = storage.materialize_deployment(mod.path, mod.mod_id, "disabled-legacy")
    if existing:
        legacy = target.parent / f"DISABLED_{mod.mod_id[:12]}_disabled-legacy"
        assert legacy.parent == target.parent
        target.rename(legacy)
        target = legacy

    prepared = EnablePreparation(manager, mod.mod_id, str(target), stage_only=existing).run()

    assert prepared.completed
    assert not Path(prepared.deploy_path).name.casefold().startswith("disabled")
    assert (Path(prepared.deploy_path) / "merged.ini").is_file()
    assert mod.folder_name == "AlphaSkin"


def test_preset_plan_avoids_disabled_source_name(env):
    mod = replace(env["mod"], folder_name="disabled-legacy")
    plan = build_preset_deployment_plan(
        game_id="g1",
        from_preset_id="before",
        to_preset_id="after",
        mods=[mod],
        target_mod_ids={mod.mod_id},
        deployments=[],
        deploy_root=env["migoto"] / "Mods",
    )

    assert len(plan.actions) == 1
    assert not Path(plan.actions[0].active_path).name.casefold().startswith("disabled")


@pytest.mark.parametrize(
    ("pattern", "backup"),
    [("DISABLED*", "DISABLED_BACKUP_123.ini"), ("BACKUP*", "BACKUP old/mod.ini")],
)
def test_excluded_backup_does_not_block_enable_and_is_preserved(env, pattern, backup):
    mod = env["mod"]
    config = env["migoto"] / "d3dx.ini"
    config.write_text(f"[Include]\ninclude_recursive = Mods\nexclude_recursive = {pattern}\n", encoding="utf-8")
    backup_text = "namespace =\n[Constants]\nglobal persist $swapvar = 9\n"
    assert write_override(mod.path, backup, backup_text)
    source = Path(mod.path).read_bytes()

    prepared = _prepare(env).run()

    assert prepared.completed
    assert prepared.outcome.applied == (mod.mod_id,)
    assert (Path(prepared.deploy_path) / backup).read_text(encoding="utf-8") == backup_text
    assert override_path_for(mod.path, backup).read_text(encoding="utf-8") == backup_text
    assert Path(mod.path).read_bytes() == source
    index = prepared.variants.build_index(mods=[prepared.mod])
    assert len(index.declarations) == 1
    assert index.declarations[0].rel == "merged.ini"


def test_unavailable_namespace_can_enable_without_overwriting_preferences(env):
    manager, mod = env["mm"], env["mod"]
    body = "namespace =\n[Constants]\nglobal persist $swapvar = 0\n"
    assert write_override(mod.path, "merged.ini", body)
    env["user_ini"].write_text("[Constants]\n$unrelated = 7\n", encoding="utf-8")
    before_runtime = env["user_ini"].read_bytes()
    prefs_path = manager.variants.prefs_file()
    before_prefs = prefs_path.read_bytes() if prefs_path.is_file() else None

    prepared = _prepare(env).run()

    assert prepared.completed
    assert prepared.outcome.skipped == (mod.mod_id,)
    assert not prepared.outcome.applied
    assert env["user_ini"].read_bytes() == before_runtime
    assert (prefs_path.read_bytes() if prefs_path.is_file() else None) == before_prefs
    assert (Path(prepared.deploy_path) / "merged.ini").read_text(encoding="utf-8") == body


def test_unavailable_namespace_preserves_existing_preferences_on_enable(env):
    manager, mod = env["mm"], env["mod"]
    assert write_override(mod.path, "merged.ini", "namespace =\n[Constants]\nglobal persist $swapvar = 0\n")
    store = manager.variants.load_prefs()
    store.update_recent(mod.mod_id, {r"$\mods\alphaskin\merged.ini\swapvar": "2"})
    assert manager.variants.save_prefs(store)
    before = manager.variants.prefs_file().read_bytes()
    prepared = _prepare(env)

    prepared.run()

    assert prepared.completed
    assert prepared.outcome.skipped == (mod.mod_id,)
    assert not prepared.outcome.applied
    assert manager.variants.prefs_file().read_bytes() == before
    assert not manager.get_mod(mod.mod_id).is_enabled


@pytest.mark.parametrize("unresolved", [False, True])
def test_real_duplicate_declarations_still_block_enable(env, unresolved):
    manager, mod = env["mm"], env["mod"]
    text = "namespace = shared\n[Constants]\nglobal persist $swapvar = 0\n"
    assert write_override(mod.path, "merged.ini", text)
    assert write_override(mod.path, "DISABLED_BACKUP.ini", text)
    (env["migoto"] / "d3dx.ini").write_text("[Include]\ninclude_recursive = Mods\n", encoding="utf-8")
    if unresolved:
        assert write_override(mod.path, "empty.ini", "namespace =\n[Constants]\nglobal persist $other = 0\n")

    with pytest.raises(ValueError):
        _prepare(env).run()

    assert not manager.get_mod(mod.mod_id).is_enabled


@pytest.mark.parametrize("included_by", ["loader", "mod"])
def test_explicit_include_overrides_recursive_exclusion(env, included_by):
    mod = env["mod"]
    assert write_override(
        mod.path, "DISABLED_DETAIL.ini", "namespace = detail\n[Constants]\nglobal persist $color = 2\n"
    )
    if included_by == "loader":
        config = env["migoto"] / "d3dx.ini"
        config.write_text(
            config.read_text(encoding="utf-8") + "include = Mods/AlphaSkin/DISABLED_DETAIL.ini\n", encoding="utf-8"
        )
    else:
        assert write_override(
            mod.path,
            "merged.ini",
            "[Constants]\nglobal persist $swapvar = 0\n[Include]\ninclude = DISABLED_DETAIL.ini\n",
        )

    prepared = _prepare(env).run()

    index = prepared.variants.build_index(mods=[prepared.mod])
    assert {decl.rel for decl in index.declarations} == {"merged.ini", "DISABLED_DETAIL.ini"}


def test_excluded_deployment_addition_does_not_invalidate_loaded_inventory(env, monkeypatch):
    prepared = _prepare(env)
    execute = prepared.variants._execute
    backup = Path(prepared.deploy_path) / "DISABLED_EXTERNAL.ini"

    def add_backup(*args, **kwargs):
        """在提交前模拟外部工具保存一个不参与加载的备份文件。"""
        backup.write_text("namespace =\n[Constants]\nglobal persist $old = 0\n", encoding="utf-8")
        return execute(*args, **kwargs)

    monkeypatch.setattr(prepared.variants, "_execute", add_backup)

    assert prepared.run().completed
    assert backup.is_file()


@pytest.mark.parametrize("keeps_changing", [False, True])
def test_enable_inventory_retry_rebuilds_once_and_preserves_external_changes(env, monkeypatch, keeps_changing):
    manager, mod = env["mm"], env["mod"]
    prepared = _prepare(env)
    execute = prepared.variants._execute
    original_presets = manager.preset_manager._presets_file.read_bytes()
    calls = []

    def add_ini(*args, **kwargs):
        """在冻结后添加源与部署都可见的 INI，区分一次变化和持续变化。"""
        calls.append(None)
        if len(calls) == 1 or keeps_changing:
            name = f"extra{len(calls)}.ini"
            body = f"namespace = extra{len(calls)}\n[Constants]\nglobal persist $value = 3\n"
            assert write_override(mod.path, name, body)
            (Path(prepared.deploy_path) / name).write_bytes(body.encode("utf-8"))
        return execute(*args, **kwargs)

    monkeypatch.setattr(prepared.variants, "_execute", add_ini)

    if keeps_changing:
        with pytest.raises(RuntimeError):
            prepared.run()
        assert not prepared.completed
        assert manager.preset_manager._presets_file.read_bytes() == original_presets
    else:
        assert prepared.run().completed
        index = prepared.variants.build_index(mods=[prepared.mod])
        assert {decl.rel for decl in index.declarations} == {"merged.ini", "extra1.ini"}
    assert len(calls) == 2
    assert (
        override_path_for(mod.path, "extra1.ini").read_bytes()
        == (Path(prepared.deploy_path) / "extra1.ini").read_bytes()
    )


def test_inventory_retry_does_not_overwrite_a_concurrent_preset_selection(env, monkeypatch):
    import json

    prepared = _prepare(env)
    execute = prepared.variants._execute
    preset_path = env["mm"].preset_manager._presets_file
    written = []

    def change_selection(*args, **kwargs):
        """同时改变 INI 清单和磁盘上的预设选择，验证不会沿用旧选择重试。"""
        body = "namespace = extra\n[Constants]\nglobal persist $value = 3\n"
        assert write_override(env["mod"].path, "extra.ini", body)
        (Path(prepared.deploy_path) / "extra.ini").write_bytes(body.encode("utf-8"))
        data = json.loads(preset_path.read_text(encoding="utf-8"))
        data["presets"][0].setdefault("mod_variants", {})[env["mod"].mod_id] = "external-selection"
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        preset_path.write_bytes(raw)
        written.append(raw)
        return execute(*args, **kwargs)

    monkeypatch.setattr(prepared.variants, "_execute", change_selection)

    with pytest.raises(RuntimeError):
        prepared.run()

    assert len(written) == 1
    assert preset_path.read_bytes() == written[0]
    assert not prepared.completed


@pytest.mark.parametrize("previous", [{}, {r"$\known\value": "1"}])
def test_unavailable_namespace_does_not_capture_partial_preferences(previous):
    from src.core.mod_runtime_prefs import RuntimePrefsStore
    from src.core.mod_variant_service import collect

    index = build_attribution_index(
        [
            ModIniSource(
                "sample",
                (
                    ("known.ini", "namespace = known\n[Constants]\nglobal persist $value = 0\n"),
                    ("unknown.ini", "namespace =\n[Constants]\nglobal persist $other = 0\n"),
                ),
            )
        ]
    )
    store = RuntimePrefsStore()
    store.update_recent("sample", previous)
    values = {r"$\known\value": "3"}
    snapshot = user_ini.UserIniSnapshot(Path("d3dx_user.ini"), "[Constants]\n", "", "utf-8", values)

    report = collect(snapshot, index, store)

    assert not report.attributed
    assert store.snapshot_for("sample") == previous


def test_unavailable_namespace_preserves_previous_capture_evidence_after_restart(env):
    """不完整声明的 Mod 不得被重新标记成新会话已采集的状态。"""
    manager, mod, _ = enable(env)
    save(env)
    before = manager.variants.load_prefs().state_for(mod.mod_id).to_dict()
    text = "namespace =\n[Constants]\nglobal persist $other = 0\n"
    assert write_override(mod.path, "unresolved.ini", text)
    deployed = Path(manager.get_mod(mod.mod_id).deployed_path)
    (deployed / "unresolved.ini").write_text(text, encoding="utf-8")
    env["user_ini"].write_text("[Constants]\n$\\mods\\alphaskin\\merged.ini\\swapvar = 3\n", encoding="utf-8")
    restarted = ModVariantManager(manager)

    restarted.collect(trigger="startup")

    assert restarted.load_prefs().state_for(mod.mod_id).to_dict() == before


@pytest.mark.parametrize("excluded", [False, True])
def test_loaded_inventory_rules_survive_transaction_recovery(env, tmp_path, excluded):
    from src.core.variant_operation import IniInventoryChanged, VariantOperation

    manager = env["mm"]
    prepared = _prepare(env)
    mod = replace(env["mod"], deployed_path=prepared.deploy_path)
    mod.enable()
    index = manager.variants.build_index(mods=[mod])
    operation = VariantOperation(tmp_path / "operation", "g1", [mod.mod_id], index.source_checks)
    operation.prepare()
    restored = VariantOperation.load(operation.journal_path, allowed_roots=manager.variants._allowed_roots("g1"))
    added = Path(prepared.deploy_path) / ("DISABLED_added.ini" if excluded else "added.ini")
    added.write_bytes(b"[Constants]\nglobal persist $value = 3\n")

    if excluded:
        restored.apply()
        restored.commit()
    else:
        with pytest.raises(IniInventoryChanged):
            restored.apply()

    assert added.read_bytes() == b"[Constants]\nglobal persist $value = 3\n"
