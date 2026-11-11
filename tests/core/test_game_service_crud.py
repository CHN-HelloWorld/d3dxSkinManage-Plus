"""GameService 增删改/迁移/排序/门禁 单测（业务层，不依赖 Qt）。

隔离 Config / GameConfigStore 单例到临时目录，避免污染真实的 settings.json 与 games.json。
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict

import pytest

from src.core.config import Config
from src.core.game_config_store import GameConfigStore
from src.core.game_service import GameConfig, GameService


@pytest.fixture
def svc(temp_data_dir):
    """temp_data_dir 已重定向 config/ 并清掉两个存储单例；GameService 需另清。"""
    Config._instance = None
    GameConfigStore._instance = None
    GameService._instance = None
    s = GameService()  # 干净空环境（程序不内置游戏配置）
    yield s
    Config._instance = None
    GameConfigStore._instance = None
    GameService._instance = None


def test_save_new_game_derives_id_and_persists(svc):
    gid = svc.save_game(GameConfig(name="测试游戏", name_en="Test Game"))
    assert gid
    g = svc.get_game(gid)
    assert g is not None and g.name == "测试游戏" and g.name_en == "Test Game"
    assert any(e["id"] == gid for e in svc._store.get_games())


def test_save_existing_game_updates_in_place(svc):
    gid = svc.save_game(GameConfig(name="A", name_en="A"))
    svc.save_game(GameConfig(id=gid, name="A2", name_en="A"))
    assert svc.get_game(gid).name == "A2"
    assert len([e for e in svc._store.get_games() if e["id"] == gid]) == 1


def test_save_new_game_does_not_lose_others(svc):
    a = svc.save_game(GameConfig(name="A", name_en="A"))
    b = svc.save_game(GameConfig(name="B", name_en="B"))
    ids = {g.id for g in svc.games}
    assert a in ids and b in ids


def test_delete_game_removes_entry(svc):
    gid = svc.save_game(GameConfig(name="Del", name_en="Del"))
    svc.delete_game(gid)
    assert svc.get_game(gid) is None
    assert all(e["id"] != gid for e in svc._store.get_games())


def test_delete_current_game_deactivates(svc):
    gid = svc.save_game(GameConfig(name="Cur", name_en="Cur"))
    svc.current_game_id = gid
    svc.delete_game(gid)
    assert svc.current_game_id == ""


def test_reorder_games_persists_order(svc):
    a = svc.save_game(GameConfig(name="A", name_en="A"))
    b = svc.save_game(GameConfig(name="B", name_en="B"))
    svc.reorder_games([b, a])
    assert [g.id for g in svc.games] == [b, a]


def test_has_active_game(svc):
    assert svc.has_active_game() is False
    gid = svc.save_game(GameConfig(name="A", name_en="A"))
    svc.current_game_id = gid
    assert svc.has_active_game() is True


def test_has_active_game_false_when_current_invalid(svc):
    svc.current_game_id = "ghost-id-not-exist"
    assert svc.has_active_game() is False


def test_rebase_deploy_dir_persists_only_target_path(svc, temp_data_dir):
    games = [
        dict(
            id="one",
            name="One",
            name_en="One",
            resource_dir="custom resource",
            mods_path="old root",
            preview="",
            extension={"keep": [1, 2]},
        ),
        dict(id="two", name="Two", name_en="Two", resource_dir="Two", mods_path="other root"),
    ]
    assert svc._store.set_games(games)
    svc.reload_games()
    svc.current_game_id = "one"
    resource = temp_data_dir / "data" / "custom resource"
    resource.mkdir(parents=True)
    sentinel = resource / "keep.txt"
    sentinel.write_text("unchanged")
    expected = json.loads(svc._store.path.read_text(encoding="utf-8"))
    expected["games"][0]["mods_path"] = "new root"

    svc.rebase_deploy_dir("one", "old root", "new root")

    assert json.loads(svc._store.path.read_text(encoding="utf-8")) == expected
    assert svc._store.get_games()[0]["mods_path"] == str(temp_data_dir / "new root")
    assert svc.get_game("one").mods_path == str(temp_data_dir / "new root")
    assert svc.get_game("one").resource_dir == "custom resource"
    assert svc.get_game("two").mods_path == str(temp_data_dir / "other root")
    assert svc.current_game_id == "one"
    assert sentinel.read_text() == "unchanged"


def test_rebase_deploy_dir_atomic_write_failure_preserves_disk_and_both_memories(svc, monkeypatch):
    from src.core import config_file_io

    gid = svc.save_game(GameConfig(name="One", name_en="One", mods_path="old root"))
    disk_before = svc._store.path.read_bytes()
    store_before = copy.deepcopy(svc._store._data)
    service_before = [asdict(game) for game in svc.games]

    def fail_replace(source, target):
        raise OSError("simulated atomic replacement failure")

    monkeypatch.setattr(config_file_io.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated atomic replacement failure"):
        svc.rebase_deploy_dir(gid, "old root", "new root")

    assert svc._store.path.read_bytes() == disk_before
    assert svc._store._data == store_before
    assert [asdict(game) for game in svc.games] == service_before
    assert not list(svc._store.path.parent.glob("games.json.tmp-*"))


@pytest.mark.parametrize("conflict", ["service", "store", "missing"])
def test_rebase_deploy_dir_rejects_changed_configuration(svc, conflict):
    gid = svc.save_game(GameConfig(name="One", name_en="One", mods_path="old root"))
    if conflict == "store":
        games = svc._store.get_games()
        games[0]["mods_path"] = "another selection"
        assert svc._store.set_games(games)
    disk_before = svc._store.path.read_bytes()
    store_before = copy.deepcopy(svc._store._data)
    service_before = [asdict(game) for game in svc.games]

    with pytest.raises(ValueError):
        svc.rebase_deploy_dir(
            "unknown" if conflict == "missing" else gid,
            "wrong old root" if conflict == "service" else "old root",
            "new root",
        )

    assert svc._store.path.read_bytes() == disk_before
    assert svc._store._data == store_before
    assert [asdict(game) for game in svc.games] == service_before
