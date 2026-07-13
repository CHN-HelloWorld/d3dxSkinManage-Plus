from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.core.config import Config
from src.core.mod_manager import ModManager
from tests.helpers import register_games, reset_game_service


@pytest.fixture
def mm(temp_data_dir):
    """先注册两个游戏，再创建管理器，保证各游戏资源目录独立。"""
    from src.core import game_service as game_service_mod

    register_games("game_a", "game_b")
    cfg = Config()
    cfg.set("import.format", "zip")
    cfg.set("import.strict_format", False)
    manager = ModManager()
    manager.set_game("game_a")
    yield manager
    manager.stop_ini_sync()
    # 释放本夹具注册的游戏，避免单例状态影响其他用例。
    game_service_mod.GameService._instance = None
    reset_game_service()


def _drop_bare_mod(mm: ModManager, char_id: str, name: str, game_id: str = "game_a") -> Path:
    """模拟外部 Mod 目录进入角色资源库。"""
    lib = mm._get_character_library(char_id, game_id)
    lib.mkdir(parents=True, exist_ok=True)
    dropped = lib / name
    dropped.mkdir()
    # 内容按名字区分：库内按 content_hash 判重，同内容会被当重复项拒收。
    (dropped / "mod.ini").write_text(f"[Constants]\nx={name}\n", encoding="utf-8")
    return dropped


def _import_mod(mm: ModManager, char_id: str, tmp_path: Path, name: str):
    source = tmp_path / f"src_{name}"
    source.mkdir()
    (source / "mod.ini").write_text(f"[Constants]\nx={name}\n", encoding="utf-8")
    mod = mm.import_mod(source_path=str(source), node_id=char_id, name=name)
    assert mod is not None
    return mod


def _commit_requests(mm: ModManager, requests) -> list:
    """按界面工作线程的流程逐项提交计划，返回成功入库的 Mod。"""
    from src.core.mod_intake import IntakeError, ModIntakeService

    service = ModIntakeService(mm)
    committed = []
    for request in requests:
        try:
            committed.append(service.commit(service.prepare(request)))
        except IntakeError:
            continue
    return committed


# 模拟启动时的资源库对账。


def test_startup_intakes_content_added_while_closed(mm, tmp_path):
    char = mm.add_character(name="Character", node_type="character")
    dropped = _drop_bare_mod(mm, char.id, "OfflineDrop")

    report = mm.reconcile_library("game_a", trigger="startup")

    assert [Path(r.source_path) for r in report.intake_requests] == [dropped]
    mods = _commit_requests(mm, report.intake_requests)
    assert len(mods) == 1
    body = Path(mods[0].path)
    assert (body.parent / ".d3dxmod").is_file()  # 受管路径必须落在容器内

    # 幂等：同一批内容不会被第二次对账重复登记
    assert mm.reconcile_library("game_a", trigger="startup").intake_requests == []


def test_startup_skips_hashing_registered_containers(mm, tmp_path):
    from src.models.mod import Mod

    char = mm.add_character(name="Character", node_type="character")
    _import_mod(mm, char.id, tmp_path, "Registered")

    calls: list[str] = []
    original = Mod.compute_sha1

    def _spy(self, *args, **kwargs):
        calls.append(self.mod_id)
        return original(self, *args, **kwargs)

    Mod.compute_sha1 = _spy
    try:
        report = mm.reconcile_library("game_a", trigger="startup")
    finally:
        Mod.compute_sha1 = original

    assert report.intake_requests == []
    assert calls == []


# 模拟游戏切换时的资源库对账。


def test_game_switch_scopes_to_target_game(mm, tmp_path):
    game_a_char = mm.add_character(name="ExampleGameA Char", node_type="character")
    game_a_mod = _import_mod(mm, game_a_char.id, tmp_path, "ExampleGameAMod")
    _drop_bare_mod(mm, game_a_char.id, "ExampleGameADrop")

    mm.set_game("game_b")
    secondary_char = mm.add_character(name="Secondary Character", node_type="character")
    star_drop = _drop_bare_mod(mm, secondary_char.id, "StarDrop", game_id="game_b")

    report = mm.reconcile_library("game_b", trigger="game_switch")

    assert [Path(r.source_path) for r in report.intake_requests] == [star_drop]
    assert report.removed_sources == 0
    # 上一个游戏的记录完全没被碰
    assert mm._index.get_mod(game_a_mod.mod_id) is not None


# 模拟文件监听器触发的对账。


def test_watcher_scans_only_changed_paths(mm, tmp_path):
    from src.utils import fs as fs_mod

    first = mm.add_character(name="First", node_type="character")
    second = mm.add_character(name="Second", node_type="character")
    watched_drop = _drop_bare_mod(mm, first.id, "WatchedDrop")
    _drop_bare_mod(mm, second.id, "UntouchedDrop")
    second_lib = mm._get_character_library(second.id)

    listed: list[str] = []
    original = fs_mod.list_children

    def _spy(path, *args, **kwargs):
        listed.append(str(path))
        return original(path, *args, **kwargs)

    fs_mod.list_children = _spy
    try:
        report = mm.reconcile_library(
            "game_a",
            trigger="watcher",
            changed_paths={watched_drop},
        )
    finally:
        fs_mod.list_children = original

    assert [Path(r.source_path) for r in report.intake_requests] == [watched_drop]
    assert not any(Path(p) == second_lib for p in listed)


def test_watcher_without_changed_paths_does_not_discover(mm):
    char = mm.add_character(name="Character", node_type="character")
    _drop_bare_mod(mm, char.id, "Ignored")

    report = mm.reconcile_library("game_a", trigger="watcher", changed_paths=set())

    assert report.intake_requests == []


# 模拟手动刷新。


def test_manual_converges_new_missing_source_and_missing_deploy(mm, tmp_path):
    char = mm.add_character(name="Character", node_type="character")
    doomed = _import_mod(mm, char.id, tmp_path, "Doomed")
    deployed = _import_mod(mm, char.id, tmp_path, "Deployed")

    deploy_root = tmp_path / "deploy"
    mm.set_deploy_dir(str(deploy_root))
    assert mm.toggle_mod(deployed.mod_id).is_enabled
    deploy_target = deploy_root / deployed.folder_name
    assert deploy_target.exists()

    dropped = _drop_bare_mod(mm, char.id, "FreshDrop")
    shutil.rmtree(Path(doomed.path).parent)  # 源容器被外部删除
    shutil.rmtree(deploy_target)  # 仅部署副本被外部删除

    report = mm.reconcile_library("game_a", trigger="manual")

    assert [Path(r.source_path) for r in report.intake_requests] == [dropped]
    assert report.removed_sources == 1
    assert report.disabled_deployments == [deployed.mod_id]
    assert mm._index.get_mod(doomed.mod_id) is None
    assert mm._index.get_mod(deployed.mod_id).is_enabled is False


def test_manual_never_relocates_by_folder_name(mm, tmp_path):
    char = mm.add_character(name="Character", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "Renamed")
    container = Path(mod.path).parent
    old_path = mod.path
    shutil.rmtree(container)
    # 用户随后手动放了个同名文件夹（内容未知）——它是新输入，不是旧记录的新位置
    revived = _drop_bare_mod(mm, char.id, container.name)

    report = mm.reconcile_library("game_a", trigger="manual")

    kept = mm._index.get_mod(mod.mod_id)
    assert kept is not None and kept.path == old_path
    assert not Path(kept.path).exists()
    assert report.claimed == 0
    assert report.removed_sources == 0
    assert report.pending_cleanup
    assert [Path(r.source_path) for r in report.intake_requests] == [revived]
    assert (revived / "mod.ini").is_file()


# 按持久身份认领改名或移动的容器，保留启用状态和预设引用。


def test_claims_renamed_container_by_content_hash(mm, tmp_path):
    """手动改容器目录名：按 marker 的 content_hash 精确认领，同一条记录换路径。

    名称永不构成身份（见 test_manual_never_relocates_by_folder_name）；容器 marker 里的
    content_hash 才是。认领必须保住 mod id、启用态、预设引用与部署名。
    """
    char = mm.add_character(name="Character", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "Claimed")
    deploy_root = tmp_path / "deploy"
    mm.set_deploy_dir(str(deploy_root))
    assert mm.toggle_mod(mod.mod_id).is_enabled
    mm.preset_manager.create_preset("game_a", "Custom", snapshot={char.id: [mod.mod_id]})

    container = Path(mod.path).parent
    renamed = container.with_name("ManualRenamed")
    container.rename(renamed)

    report = mm.reconcile_library("game_a", trigger="manual")

    assert report.claimed == 1
    assert report.removed_sources == 0
    kept = mm._index.get_mod(mod.mod_id)
    assert kept is not None
    assert Path(kept.path).parent == renamed
    assert kept.is_enabled  # 启用态保住
    assert kept.folder_name == renamed.name  # 源目录名投影跟随实际位置
    assert report.intake_requests == []  # 已纳管的内容不得再次作为新增输入导入。
    presets = mm.preset_manager.get_all_presets("game_a")
    assert any(mod.mod_id in ids for preset in presets for ids in preset.character_mods.values())


def test_claim_moves_classification_when_container_moved(mm, tmp_path):
    source_char = mm.add_character(name="From", node_type="character")
    target_char = mm.add_character(name="To", node_type="character")
    mod = _import_mod(mm, source_char.id, tmp_path, "Moved")

    container = Path(mod.path).parent
    target_lib = mm._get_character_library(target_char.id)
    target_lib.mkdir(parents=True, exist_ok=True)
    container.rename(target_lib / container.name)

    report = mm.reconcile_library("game_a", trigger="manual")

    assert report.claimed == 1
    assert report.removed_sources == 0
    kept = mm._index.get_mod(mod.mod_id)
    assert kept.node_id == target_char.id
    assert Path(kept.path).parent.parent == target_lib


def test_claim_never_takes_a_path_another_record_owns(mm, tmp_path):
    char = mm.add_character(name="Character", node_type="character")
    keeper = _import_mod(mm, char.id, tmp_path, "Keeper")
    ghost = _import_mod(mm, char.id, tmp_path, "Ghost")

    # 幽灵记录：路径已消失，但内容哈希被改成与 keeper 相同（模拟哈希撞车）
    shutil.rmtree(Path(ghost.path).parent)
    ghost.content_hash = keeper.content_hash
    mm._index.update_mod(ghost)

    report = mm.reconcile_library("game_a", trigger="manual")

    assert report.claimed == 0
    assert report.removed_sources == 1
    assert mm._index.get_mod(ghost.mod_id) is None
    assert mm._index.get_mod(keeper.mod_id).path == keeper.path


def test_manual_reports_broken_container_without_blocking_batch(mm, tmp_path):
    char = mm.add_character(name="Character", node_type="character")
    healthy = _drop_bare_mod(mm, char.id, "Healthy")
    broken = mm._get_character_library(char.id) / "Broken"
    broken.mkdir()
    (broken / ".d3dxmod").write_text("{ not json", encoding="utf-8")
    (broken / "Broken").mkdir()

    report = mm.reconcile_library("game_a", trigger="manual")

    assert [Path(r.source_path) for r in report.intake_requests] == [healthy]
    assert [Path(p) for p in report.invalid_containers] == [broken]


# 未完成事务优先


def test_pending_preset_transaction_defers_whole_round(mm, tmp_path):
    char = mm.add_character(name="Character", node_type="character")
    doomed = _import_mod(mm, char.id, tmp_path, "Doomed")
    _drop_bare_mod(mm, char.id, "FreshDrop")
    shutil.rmtree(Path(doomed.path).parent)

    mm._preset_manager.has_pending_apply = lambda _gid: True

    report = mm.reconcile_library("game_a", trigger="manual")

    assert report.skipped
    assert report.intake_requests == []
    assert report.removed_sources == 0
    assert mm._index.get_mod(doomed.mod_id) is not None


def test_plan_intakes_false_still_reconciles_paths(mm, tmp_path):
    char = mm.add_character(name="Character", node_type="character")
    doomed = _import_mod(mm, char.id, tmp_path, "Doomed")
    _drop_bare_mod(mm, char.id, "FreshDrop")
    shutil.rmtree(Path(doomed.path).parent)

    report = mm.reconcile_library("game_a", trigger="manual", plan_intakes=False)

    assert report.intake_requests == []
    assert report.removed_sources == 1
    assert mm._index.get_mod(doomed.mod_id) is None
