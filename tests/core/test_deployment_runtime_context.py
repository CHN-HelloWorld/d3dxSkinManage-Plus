"""验证部署环境决定外观配置来源，旧启动路径不能参与读写或监控。"""

from pathlib import Path

import pytest

from src.core.game_paths import resolve_deployment_migoto_root
from tests.core.test_enable_without_runtime_namespace import _prepare
from tests.core.test_named_variant_manager import enable, save
from tests.core.test_variant_preset_switch import _KEY, _user_keys, _write_user_ini
from tests.core.test_variant_preset_switch import env as env


def _configure_stale_loader(env, availability):
    """保留真实部署环境，只把启动路径指向缺失、空白或另一套有效加载器。"""
    game = env["mm"]._game_service.get_game("g1")
    stale = env["migoto"].parent / "previous-loader"
    if availability == "unrelated":
        stale.mkdir()
        (stale / "d3dx.ini").write_text("[Include]\ninclude_recursive = Mods\n", encoding="utf-8")
        _write_user_ini(stale / "d3dx_user.ini", "8")
    game.migoto_path = "" if availability == "unconfigured" else str(stale)
    return stale


@pytest.mark.parametrize("availability", ["missing", "unconfigured", "unrelated"])
@pytest.mark.parametrize("background", [False, True])
def test_enable_restores_saved_variant_using_current_deployment(env, availability, background):
    mm, mod, current = enable(env)
    variant = save(env, value="4")
    assert mm.toggle_mod(mod.mod_id) is not None
    stale = _configure_stale_loader(env, availability)
    stale_files = {path: path.read_bytes() for path in stale.glob("*.ini")}
    _write_user_ini(env["user_ini"], "9")
    events = []
    mm.add_event_listener(lambda event, **payload: events.append(event))

    if background:
        prepared = _prepare(env).run()
        assert prepared.completed and not prepared.outcome.skipped
        assert prepared.outcome.applied == (mod.mod_id,)
        enabled = mm.finalize_enable(
            mod.mod_id,
            True,
            deploy_path=prepared.deploy_path,
            source_receipt=prepared.source_receipt,
            preparation=prepared,
        )
    else:
        enabled = mm.toggle_mod(mod.mod_id)

    assert enabled is not None and enabled.is_enabled
    assert _user_keys(env["user_ini"])[_KEY] == "4"
    assert current.mod_variants[mod.mod_id] == variant.variant_id
    assert "variant_restore_skipped" not in events
    assert {path: path.read_bytes() for path in stale.glob("*.ini")} == stale_files


def test_capture_and_watch_use_current_deployment_even_when_old_loader_exists(env):
    mm, mod, _ = enable(env)
    save(env)
    stale = _configure_stale_loader(env, "unrelated")
    before = (stale / "d3dx_user.ini").read_bytes()
    _write_user_ini(env["user_ini"], "6")

    report = mm.variants.collect(trigger="deployment_environment")

    assert report is not None and report.changed
    assert mm.variants.load_prefs().state_for(mod.mod_id).values[_KEY] == "6"
    assert mm.variants.get_state(mod.mod_id)["can_save"]
    assert mm.variants.refresh_watch()
    assert mm.variants._watcher.target == env["user_ini"]
    assert (stale / "d3dx_user.ini").read_bytes() == before


def test_switching_deployment_rebinds_custom_runtime_file_and_drops_queued_capture(env):
    mm, mod, _ = enable(env)
    save(env)
    assert mm.variants.refresh_watch()
    events = []
    mm.variants.set_change_notify(events.append)
    mm.variants._on_user_ini_changed()
    old_event = events[-1]
    old_runtime = env["user_ini"].read_bytes()
    new_root = env["migoto"].parent / "current-loader"
    deployment = new_root / "profiles" / "Mods"
    deployment.mkdir(parents=True)
    runtime = new_root / "settings" / "appearance.ini"
    runtime.parent.mkdir()
    runtime.write_text("[Constants]\n$unrelated = 7\n", encoding="utf-8")
    (new_root / "d3dx.ini").write_text(
        "[Include]\ninclude_recursive = profiles\\Mods\nuser_config = settings\\appearance.ini\n",
        encoding="utf-8",
    )
    game = mm._game_service.get_game("g1")
    game.mods_path = str(deployment)

    mm.set_deploy_dir(str(deployment), defer_activation=True)

    assert mm.variants.user_ini_path() == runtime
    assert mm.variants._watcher.target == runtime
    assert mm.variants.collect(trigger="queued_old_event", event=old_event) is None
    assert env["user_ini"].read_bytes() == old_runtime
    assert Path(game.migoto_path) == env["migoto"]


def test_nearer_loader_configuration_blocks_fallback_to_enclosing_loader(tmp_path):
    old_root = tmp_path / "previous-loader"
    current = old_root / "Mods" / "current-loader"
    deployment = current / "Mods"
    deployment.mkdir(parents=True)
    (old_root / "d3dx.ini").write_text("[Include]\ninclude_recursive = Mods\n", encoding="utf-8")
    (current / "d3dx.ini").write_text("[Include]\ninclude_recursive = OtherMods\n", encoding="utf-8")

    assert resolve_deployment_migoto_root(deployment) is None


def test_named_include_section_identifies_current_runtime(env):
    (env["migoto"] / "d3dx.ini").write_text(
        "[Include]\nexclude_recursive = DISABLED*\n[IncludeMods]\ninclude_recursive = Mods\n", encoding="utf-8"
    )
    _write_user_ini(env["user_ini"], "9")

    prepared = _prepare(env).run()

    assert prepared.outcome.applied == (env["mod"].mod_id,)
    assert not prepared.outcome.skipped
    assert _KEY not in _user_keys(env["user_ini"])


def test_deployment_at_loader_root_uses_relative_ini_namespace(env):
    root = env["migoto"]
    (root / "d3dx.ini").write_text("[Include]\ninclude_recursive = .\n", encoding="utf-8")
    key = r"$\alphaskin\merged.ini\swapvar"
    env["user_ini"].write_text("[Constants]\n" + key + " = 9\n", encoding="utf-8")
    env["mm"]._game_service.get_game("g1").mods_path = str(root)
    env["mm"].set_deploy_dir(str(root))

    prepared = _prepare(env).run()

    assert prepared.outcome.applied == (env["mod"].mod_id,)
    assert not prepared.outcome.skipped
    assert key not in _user_keys(env["user_ini"])
