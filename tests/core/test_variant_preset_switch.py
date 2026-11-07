from __future__ import annotations

import json
import uuid
import zipfile
from pathlib import Path

import pytest

from src.core import migoto_user_ini as mui
from src.core.game_config_store import GameConfigStore
from src.core.mod_package import read_override
from src.models.mod import Mod
from tests.helpers import apply_preset, register_games, reset_game_service

_MOD_INI = "[Constants]\nglobal persist $swapvar = 0\n"
_KEY = r"$\mods\alphaskin\merged.ini\swapvar"


@pytest.fixture
def env(temp_data_dir, tmp_path, monkeypatch):
    """真实 3DMigoto 目录 + 部署目录在其下（隐式 namespace 才能推导）+ 一个归档容器 Mod。"""
    from src.core.mod_manager import ModManager

    migoto = tmp_path / "3DMigoto"
    (migoto / "Mods").mkdir(parents=True)
    (migoto / "d3dx.ini").write_text(
        "[Include]\ninclude_recursive = Mods\nexclude_recursive = DISABLED*\n", encoding="utf-8"
    )

    register_games("g1")
    store = GameConfigStore()
    games = store.get_games()
    games[0].update({"migoto_path": str(migoto), "mods_path": str(migoto / "Mods")})
    store.set_games(games)
    reset_game_service()

    mm = ModManager()
    mm._game_service.reload_games()
    mm.set_game("g1")
    mm.set_deploy_dir(str(migoto / "Mods"))

    char = mm.add_character(name="示例角色甲", node_type="character")
    container = mm._get_character_library(char.id) / "AlphaSkin"
    container.mkdir(parents=True)
    body = container / "AlphaSkin.zip"
    with zipfile.ZipFile(body, "w") as zf:
        zf.writestr("AlphaSkin/merged.ini", _MOD_INI)
    (container / ".d3dxmod").write_text(
        json.dumps(
            {
                "schema": 1,
                "kind": "d3dxSkinManage-Plus.mod-container",
                "body": body.name,
                "body_type": "archive",
                "content_hash": Mod.compute_sha1(str(body)),
                "mod_id": uuid.uuid4().hex,
                "imported_at": "2026-01-01T00:00:00",
            }
        ),
        encoding="utf-8",
    )
    (container / ".d3dxmeta").mkdir()
    (container / ".d3dxmeta" / "info.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "kind": "d3dxSkinManage-Plus.mod-share",
                "name": "AlphaSkin",
                "info": {},
                "previews": [],
            }
        ),
        encoding="utf-8",
    )

    mm.rebuild_index_from_filesystem("g1")
    mod = mm.get_mods(node_id=char.id)[0]
    # 使用正常纳管写入的完整本地元数据。
    from src.core.mod_package import build_local_manifest, seed_ini_overrides, write_sidecar

    assert write_sidecar(mod.path, build_local_manifest(mod.to_dict()))
    assert seed_ini_overrides(mod.path)
    yield {"mm": mm, "mod": mod, "char": char, "migoto": migoto, "user_ini": migoto / "d3dx_user.ini"}
    mm.stop_ini_sync()
    mm.variants.stop_watch()  # 关闭文件监控线程，避免影响其他用例。
    reset_game_service()


def _write_user_ini(path: Path, value: str) -> None:
    """模拟 3DMigoto 在 F10 / 游戏退出时整文件重写 d3dx_user.ini。"""
    path.write_text(f"; AUTOMATICALLY GENERATED FILE - DO NOT EDIT\n[Constants]\n{_KEY} = {value}\n", encoding="utf-8")


def _override_value(mod) -> str:
    ov = read_override(mod.path, "merged.ini") or ""
    for line in ov.splitlines():
        if "swapvar" in line:
            return line.split("=", 1)[1].strip()
    return ""


def _user_keys(path: Path) -> dict:
    if not path.is_file():
        return {}
    return mui.parse_persisted_values(path.read_text(encoding="mbcs", errors="surrogateescape"))


@pytest.mark.parametrize("named_selection", [False, True])
def test_async_enable_uses_private_selection_or_original_default(env, named_selection):
    mm, mod = env["mm"], env["mod"]
    current = mm.preset_manager.get_current("g1")
    current.add_mod_for_character(env["char"].id, mod.mod_id)
    mm.preset_manager._save()
    apply_preset(mm.preset_manager, current.id)
    if named_selection:
        _write_user_ini(env["user_ini"], "3")
        mm.variants.save_variant(mod.mod_id, "三号外观")
    assert mm.toggle_mod(mod.mod_id) is not None
    store = mm.variants.load_prefs()
    store.update_recent(mod.mod_id, {_KEY: "2"})
    assert mm.variants.save_prefs(store)
    _write_user_ini(env["user_ini"], "9")

    target = mm._restore_parked_deployment(mm._index.get_mod(mod.mod_id))
    if target is None:
        target = mm._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name)
    enabled = mm.finalize_enable(mod.mod_id, deploy_ok=True)

    expected = "3" if named_selection else None
    assert enabled is not None and enabled.is_enabled
    assert _override_value(mod) == "0"
    assert (target / "merged.ini").read_bytes() == _MOD_INI.encode()
    assert _user_keys(env["user_ini"]).get(_KEY) == expected


@pytest.mark.parametrize("cached", [False, True])
def test_enable_repairs_legacy_empty_ini_seed_without_reimport(env, cached):
    """新解压与已有停放副本均补齐旧配置目录，不误判为外部修改。"""
    from src.core.deployment_enable import EnablePreparation
    from src.core.mod_package import override_path_for

    mm, mod = env["mm"], env["mod"]
    parked = None
    if cached:
        parked = mm._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name, active=False)
    override = override_path_for(mod.path, "merged.ini")
    override.unlink()
    (override.parent / ".seeded").write_text("1", encoding="utf-8")
    if parked is None:
        parked = mm._storage.materialize_deployment(mod.path, mod.mod_id, mod.folder_name, active=False)
    preparation = EnablePreparation(mm, mod.mod_id, parked, stage_only=True).run()
    enabled = mm.finalize_enable(
        mod.mod_id,
        True,
        deploy_path=preparation.deploy_path,
        source_receipt=preparation.source_receipt,
        preparation=preparation,
    )
    assert enabled is not None and enabled.is_enabled
    assert override.read_bytes() == _MOD_INI.encode()
    assert (Path(preparation.deploy_path) / "merged.ini").read_bytes() == _MOD_INI.encode()


def test_two_presets_switch_variant_without_redeploying(env):
    mm, mod, char, user_ini = env["mm"], env["mod"], env["char"], env["user_ini"]
    pm = mm.preset_manager
    p1 = pm.create_preset("g1", "P1", snapshot={char.id: [mod.mod_id]})
    p2 = pm.create_preset("g1", "P2", snapshot={char.id: [mod.mod_id]})

    apply_preset(pm, p1.id)
    deployed = env["migoto"] / "Mods" / "AlphaSkin"
    identity = deployed.stat().st_ino

    _write_user_ini(user_ini, "1")
    assert mm.variants.collect(trigger="test_p1").changed
    assert p1.mod_variants.get(mod.mod_id) is None
    first = mm.variants.save_variant(mod.mod_id, "白色")
    assert p1.mod_variants[mod.mod_id] == first.variant_id

    # 切到 P2：planner 判 noop（不重复部署），但运行时状态照旧要处理
    assert apply_preset(pm, p2.id) == []
    assert deployed.stat().st_ino == identity  # 验收 3：没有物理 park/materialize
    assert _override_value(mod) == "0"  # P2 无引用，使用本体默认
    assert _user_keys(user_ini) == {}  # 键被删，初始值才能生效

    # 玩家在 P2 里把变体切到 3
    _write_user_ini(user_ini, "3")
    assert mm.variants.collect(trigger="test_p2").changed
    second = mm.variants.save_variant(mod.mod_id, "黑色")
    assert p2.mod_variants[mod.mod_id] == second.variant_id
    assert p1.mod_variants[mod.mod_id] == first.variant_id

    # 来回切换各自恢复（验收 2）
    apply_preset(pm, p1.id)
    assert _override_value(mod) == "0"
    assert _user_keys(user_ini)[_KEY] == "1"
    apply_preset(pm, p2.id)
    assert _override_value(mod) == "0"
    assert _user_keys(user_ini)[_KEY] == "3"
    assert not pm.has_pending_apply("g1")


def test_variant_uses_user_preferences_after_redeployment(env):
    mm, mod, char, user_ini = env["mm"], env["mod"], env["char"], env["user_ini"]
    pm = mm.preset_manager
    p1 = pm.create_preset("g1", "P1", snapshot={char.id: [mod.mod_id]})
    apply_preset(pm, p1.id)
    _write_user_ini(user_ini, "2")
    mm.variants.save_variant(mod.mod_id, "副本外观")

    empty = pm.create_preset("g1", "Empty", snapshot={})
    apply_preset(pm, empty.id)
    apply_preset(pm, p1.id)

    deployed_ini = env["migoto"] / "Mods" / "AlphaSkin" / "merged.ini"
    assert deployed_ini.read_bytes() == _MOD_INI.encode()
    assert _user_keys(user_ini)[_KEY] == "2"


def test_prefs_file_lives_outside_any_mod_container(env):
    mm, mod = env["mm"], env["mod"]
    current = mm.preset_manager.get_current("g1")
    current.add_mod_for_character(env["char"].id, mod.mod_id)
    mm.preset_manager._save()
    apply_preset(mm.preset_manager, current.id)
    _write_user_ini(env["user_ini"], "2")
    mm.variants.collect(trigger="test")
    pf = mm.variants.prefs_file()
    assert pf is not None and pf.is_file()
    container = Path(mod.path).parent
    assert not any(p.name == pf.name for p in container.rglob("*"))
    assert "mods" not in pf.parent.name


def test_rollback_restores_override_and_user_ini(env):
    mm, mod, char, user_ini = env["mm"], env["mod"], env["char"], env["user_ini"]
    pm = mm.preset_manager
    p1 = pm.create_preset("g1", "P1", snapshot={char.id: [mod.mod_id]})
    apply_preset(pm, p1.id)
    _write_user_ini(user_ini, "2")
    mm.variants.save_variant(mod.mod_id, "回滚目标")

    _write_user_ini(user_ini, "9")
    # 重新进入一次切换事务，让运行时补丁落 journal
    plan = pm.prepare_preset_apply(p1.id)
    before_user = user_ini.read_text(encoding="mbcs", errors="surrogateescape")
    outcome = mm.variants.on_preset_apply_before_commit(plan)
    assert outcome.touched
    assert _override_value(mod) == "0"
    assert _user_keys(user_ini)[_KEY] == "2"

    problems = mm.variants.on_preset_apply_rollback(plan)
    assert problems == ()
    # 改写前没有 override → 还原成**本体原文**（不能删 override：已播种归档的
    # iter_effective_inis 只返回 override，删了这个 ini 就从有效集合里消失了）
    from src.core.mod_package import resolve_effective_ini_text

    assert _override_value(mod) == "0"
    assert resolve_effective_ini_text(mod.path, "merged.ini") == _MOD_INI
    assert user_ini.read_text(encoding="mbcs", errors="surrogateescape") == before_user
    pm.fail_preset_apply(plan, RuntimeError("probe"))


def test_rollback_refuses_when_user_ini_changed_externally(env):
    mm, mod, char, user_ini = env["mm"], env["mod"], env["char"], env["user_ini"]
    pm = mm.preset_manager
    p1 = pm.create_preset("g1", "P1", snapshot={char.id: [mod.mod_id]})
    apply_preset(pm, p1.id)
    _write_user_ini(user_ini, "2")
    mm.variants.collect(trigger="test")

    plan = pm.prepare_preset_apply(p1.id)
    assert mm.variants.on_preset_apply_before_commit(plan).touched

    # 模拟 3DMigoto 在管理器写入后再次重写整个配置文件。
    _write_user_ini(user_ini, "7")
    external = user_ini.read_text(encoding="mbcs", errors="surrogateescape")

    problems = mm.variants.on_preset_apply_rollback(plan)
    assert any("外部修改" in p for p in problems)
    assert user_ini.read_text(encoding="mbcs", errors="surrogateescape") == external
    pm.fail_preset_apply(plan, RuntimeError("probe"))


# 监控事件触发变量采集。


def test_watch_collect_ignores_our_own_write_but_sees_the_players(env):
    mm, mod, char, user_ini = env["mm"], env["mod"], env["char"], env["user_ini"]
    pm = mm.preset_manager
    p1 = pm.create_preset("g1", "P1", snapshot={char.id: [mod.mod_id]})
    apply_preset(pm, p1.id)

    _write_user_ini(user_ini, "2")
    assert mm.variants.collect(trigger="test").changed  # 玩家这次的改动
    p2 = pm.create_preset("g1", "P2", snapshot={char.id: [mod.mod_id]})
    apply_preset(pm, p2.id)  # 管理器写回时移除目标变量。

    # 自身写入触发文件事件时，监听器应根据已记录的内容摘要跳过采集。
    assert mm.variants.collect(trigger="user_ini_watch", only_if_changed=True) is None

    # 用户再次切换变量并持久化后，监听器应采集新值。
    _write_user_ini(user_ini, "5")
    report = mm.variants.collect(trigger="user_ini_watch", only_if_changed=True)
    assert report is not None and report.changed
    assert report.attributed[mod.mod_id] == {_KEY: "5"}


def test_watch_collect_survives_file_deleted_by_ctrl_alt_f10(env):
    mm, mod, char, user_ini = env["mm"], env["mod"], env["char"], env["user_ini"]
    pm = mm.preset_manager
    p1 = pm.create_preset("g1", "P1", snapshot={char.id: [mod.mod_id]})
    apply_preset(pm, p1.id)
    _write_user_ini(user_ini, "4")
    mm.variants.collect(trigger="test")
    before = mm.variants.load_prefs().snapshot_for(mod.mod_id)

    user_ini.unlink()
    assert mm.variants.collect(trigger="user_ini_watch", only_if_changed=True) is None
    assert mm.variants.load_prefs().snapshot_for(mod.mod_id) == before


def test_refresh_watch_points_at_current_game_user_ini(env, monkeypatch):
    """游戏或加载器路径变化后重新绑定监控，未配置时撤销监听。

    使用可控监控器隔离后台线程调度。
    """
    import src.core.user_ini_watcher as uiw

    real = uiw.UserIniWatcher
    monkeypatch.setattr(uiw, "UserIniWatcher", lambda cb, **kw: real(cb, **{**kw, "enable_watch": False}))

    mm = env["mm"]
    mm.variants.set_change_notify(lambda event: None)
    mm.variants.refresh_watch()
    assert mm.variants._watcher.target == env["user_ini"]

    mm.set_game("")  # 取消激活 → 定位不到文件
    assert mm.variants.refresh_watch() is False
    assert mm.variants._watcher.target is None
    mm.variants.stop_watch()
