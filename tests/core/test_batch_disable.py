"""验证批量禁用的单次变量采集、统一索引事务及单项文件操作失败隔离。"""

from __future__ import annotations

import pytest

from tests.helpers import registered_games as registered_games


def _make_enabled_mods(tmp_path, count: int):
    from src.core.config import Config
    from src.core.mod_manager import ModManager

    Config().set("import.format", "zip")
    mm = ModManager()
    mm.set_game("game_a")
    char = mm.add_character(name="角色", node_type="character")
    deploy_root = tmp_path / "deploy"
    mm.set_deploy_dir(str(deploy_root))

    mods = []
    for i in range(count):
        source = tmp_path / f"Skin{i}"
        source.mkdir()
        (source / "mod.ini").write_text(f"[Constants]\nx={i}\n", encoding="utf-8")
        mod = mm.import_mod(source_path=str(source), node_id=char.id)
        assert mod is not None
        enabled = mm.toggle_mod(mod.mod_id)
        assert enabled is not None and enabled.is_enabled
        mods.append(enabled)
    return mm, mods, deploy_root


@pytest.mark.parametrize("change_source", [False, True])
def test_prepared_disable_rechecks_receipt_without_modal_hash(temp_data_dir, tmp_path, change_source):
    from pathlib import Path

    mm, mods, deploy_root = _make_enabled_mods(tmp_path, 1)
    mod = mods[0]
    try:
        preparation = mm.create_deployment_preparation(trigger="mod_disable", mod_ids=[mod.mod_id]).prepare()
        if change_source:
            with Path(mod.path).open("ab") as stream:
                stream.write(b"changed after background validation")
        modal_calls = []

        def modal_check(work):
            modal_calls.append(True)
            return work()

        mm.operation_check_runner = modal_check
        mm.invalidate_operation_trust()
        entries = mm.prepare_disable_batch([mod.mod_id], preparation=preparation)
        assert not modal_calls, "prepared disable must not disable the window for a second hash"
        if change_source:
            assert entries == []
            assert mm.get_mod(mod.mod_id).is_enabled
            assert (deploy_root / mod.folder_name).exists()
        else:
            assert len(entries) == 1
            results = [mm.disable_batch_step(entries[0])]
            assert mm.commit_disable_batch(results) == [mod.mod_id]
            assert not mm.get_mod(mod.mod_id).is_enabled
    finally:
        mm.stop_ini_sync()


def test_prepare_collects_variants_once_for_whole_batch(temp_data_dir, tmp_path, monkeypatch):
    from src.core import mod_variant_manager as mvm

    mm, mods, deploy_root = _make_enabled_mods(tmp_path, 3)

    triggers: list[str] = []
    monkeypatch.setattr(
        mvm.ModVariantManager, "collect", lambda self, **kw: triggers.append(kw.get("trigger")) and None
    )

    entries = mm.prepare_disable_batch([m.mod_id for m in mods])
    assert len(entries) == 3
    assert triggers == ["mod_disable"], "整批只许采集一次 d3dx_user.ini"
    # 没有别的预设引用 → 一律直接移除，不停放
    assert all(entry.park is False for entry in entries)

    results = [mm.disable_batch_step(entry) for entry in entries]
    assert all(r.ok for r in results)
    disabled = mm.commit_disable_batch(results)

    assert sorted(disabled) == sorted(m.mod_id for m in mods)
    for mod in mods:
        persisted = mm.get_mod(mod.mod_id)
        assert persisted is not None and not persisted.is_enabled
        assert not (deploy_root / mod.folder_name).exists()
    current = mm.preset_manager.get_current("game_a")
    for mod in mods:
        assert mod.mod_id not in current.enabled_mod_ids
    mm.stop_ini_sync()


def test_commit_writes_go_through_one_transaction(temp_data_dir, tmp_path, monkeypatch):
    from src.core.mod_index import ModIndex

    mm, mods, _ = _make_enabled_mods(tmp_path, 3)

    seen_commit_flags: list[bool] = []
    real_add = ModIndex.add_mod

    def _spy_add(self, mod, *, commit: bool = True):
        seen_commit_flags.append(commit)
        return real_add(self, mod, commit=commit)

    monkeypatch.setattr(ModIndex, "add_mod", _spy_add)

    entries = mm.prepare_disable_batch([m.mod_id for m in mods])
    results = [mm.disable_batch_step(entry) for entry in entries]
    assert mm.commit_disable_batch(results)

    assert seen_commit_flags, "落库路径没被走到，测试本身失效了"
    assert all(flag is False for flag in seen_commit_flags), (
        "批量禁用的每条 Mod 写入都必须留在同一个事务里（commit=False）"
    )
    mm.stop_ini_sync()


def test_failed_step_keeps_that_mod_enabled(temp_data_dir, tmp_path, monkeypatch):
    mm, mods, deploy_root = _make_enabled_mods(tmp_path, 3)
    victim = mods[1]

    real_undeploy = mm._storage.undeploy_mod

    def _flaky_undeploy(source_path, deploy_name="", **kw):
        if str(deploy_name) == victim.folder_name:
            return False  # 模拟部署副本被占用 / 磁盘不可用
        return real_undeploy(source_path, deploy_name, **kw)

    monkeypatch.setattr(mm._storage, "undeploy_mod", _flaky_undeploy)

    entries = mm.prepare_disable_batch([m.mod_id for m in mods])
    results = [mm.disable_batch_step(entry) for entry in entries]
    failed = [r for r in results if not r.ok]
    assert len(failed) == 1 and failed[0].mod_id == victim.mod_id
    assert failed[0].error

    disabled = mm.commit_disable_batch(results)
    assert victim.mod_id not in disabled
    assert len(disabled) == 2

    still_on = mm.get_mod(victim.mod_id)
    assert still_on is not None and still_on.is_enabled, "失败项必须保持启用等重试"
    assert (deploy_root / victim.folder_name).exists()
    for mod in (mods[0], mods[2]):
        assert not mm.get_mod(mod.mod_id).is_enabled
    mm.stop_ini_sync()


def test_source_body_untouched_by_batch_disable(temp_data_dir, tmp_path):
    mm, mods, _ = _make_enabled_mods(tmp_path, 2)
    before = {m.mod_id: (m.path, __import__("pathlib").Path(m.path).stat().st_size) for m in mods}

    entries = mm.prepare_disable_batch([m.mod_id for m in mods])
    results = [mm.disable_batch_step(entry) for entry in entries]
    assert mm.commit_disable_batch(results)

    from pathlib import Path

    for mod_id, (path, size) in before.items():
        assert Path(path).exists(), f"源本体被删了: {path}"
        assert Path(path).stat().st_size == size, f"源本体被改了: {path}"
        assert mm.get_mod(mod_id).path == path
    mm.stop_ini_sync()
