"""验证游戏运行路径随便携程序搬迁，并保留外部位置与启动参数。"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from src.core.game_config_store import GameConfigStore
from src.core.game_paths import resolve_game_paths, store_game_paths
from src.core.game_service import GameService
from src.core.migoto import _split_game_command
from src.utils import paths


def _bind_root(root, monkeypatch):
    """绑定独立应用根并清空配置服务，模拟重启后的路径环境。"""
    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith("src."):
            continue
        for attribute, relative in (("get_app_dir", ""), ("get_data_dir", "data"), ("get_config_dir", "config")):
            if hasattr(module, attribute):
                monkeypatch.setattr(module, attribute, lambda rel=relative: root / rel)
    GameConfigStore._instance = None
    GameService._instance = None


@pytest.fixture
def portable_app(temp_data_dir, monkeypatch):
    """在测试目录内建立可复制的程序布局，不接触真实配置。"""
    root = temp_data_dir / "original"
    (root / "config").mkdir(parents=True)
    (root / "data").mkdir()
    _bind_root(root, monkeypatch)
    yield root
    GameConfigStore._instance = None
    GameService._instance = None


def _copy_and_reopen(root, monkeypatch):
    """保留旧副本并从无关工作目录打开新副本。"""
    moved = root.parent / "copied"
    shutil.copytree(root, moved, symlinks=True)
    launch = root.parent / "unrelated-cwd"
    launch.mkdir()
    monkeypatch.chdir(launch)
    _bind_root(moved, monkeypatch)
    return moved, GameConfigStore().get_games()[0]


def _entry(**paths):
    """构造含扩展信息的中性游戏条目。"""
    return {
        "id": "sample",
        "name": "示例游戏",
        "name_en": "Sample",
        "resource_dir": "Sample",
        "mods_path": "",
        "migoto_path": "",
        "exe_path": "",
        "extension": {"keep": True},
        **paths,
    }


def _loader(root):
    """创建具有独立 INI 与部署目录的加载器，不启动可执行程序。"""
    (root / "Mods").mkdir(parents=True)
    (root / "d3dx.ini").write_text("[Include]\ninclude_recursive = Mods\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("field", ["migoto_path", "mods_path", "exe_path"])
def test_internal_game_path_uses_current_copy_even_when_original_exists(portable_app, monkeypatch, field):
    path = portable_app / "runtime" / field
    path.parent.mkdir()
    if field == "exe_path":
        path.write_bytes(b"executable")
    else:
        path.mkdir()
    store = GameConfigStore()
    assert store.set_games([_entry(**{field: str(path)})])
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert saved["games"][0][field] == "runtime/" + field

    moved, restored = _copy_and_reopen(portable_app, monkeypatch)

    assert restored[field] == str(moved / "runtime" / field)
    assert Path(restored[field]).exists()
    assert path.exists()
    assert restored["extension"] == {"keep": True}


def test_external_game_paths_keep_their_explicit_locations(portable_app, monkeypatch):
    external = portable_app.parent / "external"
    loader = _loader(external / "loader")
    executable = external / "game.exe"
    executable.write_bytes(b"external executable")
    paths = {"migoto_path": str(loader), "mods_path": str(loader / "Mods"), "exe_path": str(executable)}
    assert GameConfigStore().set_games([_entry(**paths)])

    _, restored = _copy_and_reopen(portable_app, monkeypatch)

    assert {field: restored[field] for field in paths} == paths


@pytest.mark.parametrize("quoted", [False, True])
def test_portable_game_command_preserves_literal_arguments(portable_app, monkeypatch, quoted):
    executable = portable_app / "Game files" / "程序.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"executable")
    arguments = r'--profile "Profile A" --directory "D:\Data Folder\\" --text "\"quoted\"" --empty ""'
    command = f'"{executable}"' if quoted else str(executable)
    assert GameConfigStore().set_games([_entry(exe_path=command + " " + arguments)])

    moved, restored = _copy_and_reopen(portable_app, monkeypatch)
    path, actual_arguments = _split_game_command(restored["exe_path"])

    assert path == moved / "Game files" / "程序.exe"
    assert actual_arguments == arguments


def test_legacy_internal_absolute_paths_are_upgraded_when_opened(portable_app, monkeypatch):
    loader = _loader(portable_app / "runtime" / "Sample")
    raw = {"schema_version": 1, "games": [_entry(migoto_path=str(loader), mods_path=str(loader / "Mods"))]}
    (portable_app / "config" / "games.json").write_text(json.dumps(raw), encoding="utf-8")
    GameConfigStore()

    moved, restored = _copy_and_reopen(portable_app, monkeypatch)

    assert restored["migoto_path"] == str(moved / "runtime" / "Sample")
    assert restored["mods_path"] == str(moved / "runtime" / "Sample" / "Mods")


def test_missing_legacy_loader_rebases_existing_portable_layout(portable_app):
    loader = _loader(portable_app / "runtime" / "Sample" / "loader")
    executable = loader.parent / "game.exe"
    executable.write_bytes(b"executable")
    previous = portable_app.parent / "old-device" / "package"
    raw = {
        "schema_version": 1,
        "games": [
            _entry(
                migoto_path=str(previous / "runtime" / "Sample" / "loader"),
                mods_path=str(previous / "runtime" / "Sample" / "loader" / "Mods"),
                exe_path=str(previous / "runtime" / "Sample" / "game.exe"),
            )
        ],
    }
    (portable_app / "config" / "games.json").write_text(json.dumps(raw), encoding="utf-8")

    restored = GameConfigStore().get_games()[0]

    assert restored["migoto_path"] == str(loader)
    assert restored["mods_path"] == str(loader / "Mods")
    assert restored["exe_path"] == str(executable)
    stored = json.loads((portable_app / "config" / "games.json").read_text(encoding="utf-8"))
    assert stored["games"][0]["migoto_path"] == "runtime/Sample/loader"


def test_missing_loader_uses_loader_that_includes_current_deployment(portable_app):
    current = _loader(portable_app.parent / "external-runtime" / "current")
    previous = portable_app.parent / "old-device" / "loader"
    raw = {"schema_version": 1, "games": [_entry(migoto_path=str(previous), mods_path=str(current / "Mods"))]}
    (portable_app / "config" / "games.json").write_text(json.dumps(raw), encoding="utf-8")

    restored = GameConfigStore().get_games()[0]

    assert restored["migoto_path"] == str(current)
    assert restored["mods_path"] == str(current / "Mods")


def test_ambiguous_portable_loaders_do_not_replace_configured_location(portable_app):
    _loader(portable_app / "runtime" / "loader")
    _loader(portable_app / "loader")
    previous = portable_app.parent / "old-device" / "runtime" / "loader"
    raw = {"schema_version": 1, "games": [_entry(migoto_path=str(previous), mods_path=str(previous / "Mods"))]}
    (portable_app / "config" / "games.json").write_text(json.dumps(raw), encoding="utf-8")

    restored = GameConfigStore().get_games()[0]

    assert restored["migoto_path"] == str(previous)
    assert restored["mods_path"] == str(previous / "Mods")


def test_missing_portable_location_does_not_fall_back_to_another_layout(portable_app):
    _loader(portable_app / "loader")
    raw = {"schema_version": 1, "games": [_entry(migoto_path="runtime/loader", mods_path="runtime/loader/Mods")]}
    (portable_app / "config" / "games.json").write_text(json.dumps(raw), encoding="utf-8")

    restored = GameConfigStore().get_games()[0]

    assert restored["migoto_path"] == str(portable_app / "runtime" / "loader")
    assert restored["mods_path"] == str(portable_app / "runtime" / "loader" / "Mods")


@pytest.mark.parametrize("destination", [r"E:\Moved Package", r"\\new-device\shared\Moved Package"])
def test_portable_runtime_paths_resolve_on_another_drive_or_device(monkeypatch, destination):
    """仅模拟根路径变更，不访问网络盘；文件搬迁由真实目录集成用例验证。"""
    monkeypatch.setattr(paths, "get_app_dir", lambda: Path(r"D:\Original Package"))
    stored = store_game_paths(
        _entry(
            migoto_path=r"D:\Original Package\loader",
            mods_path=r"D:\Original Package\loader\Mods",
            exe_path='"D:\\Original Package\\game.exe" --windowed',
        )
    )
    monkeypatch.setattr(paths, "get_app_dir", lambda: Path(destination))

    restored = resolve_game_paths(stored)

    assert restored["migoto_path"] == destination + r"\loader"
    assert restored["mods_path"] == destination + r"\loader\Mods"
    assert restored["exe_path"] == '"' + destination + '\\game.exe" --windowed'


def test_live_parent_move_updates_loader_and_executable_with_deployment(portable_app):
    parent = portable_app / "runtime" / "Sample"
    loader = _loader(parent / "loader")
    executable = parent / "game.exe"
    executable.write_bytes(b"executable")
    store = GameConfigStore()
    assert store.set_games([_entry(migoto_path=str(loader), mods_path=str(loader / "Mods"), exe_path=str(executable))])
    service = GameService()
    moved = parent.with_name("Renamed Sample")
    assert parent.resolve().is_relative_to(portable_app.resolve())
    assert moved.resolve().is_relative_to(portable_app.resolve())
    parent.rename(moved)

    service.rebase_deploy_dir("sample", str(loader / "Mods"), str(moved / "loader" / "Mods"))

    game = service.get_game("sample")
    assert game.mods_path == str(moved / "loader" / "Mods")
    assert game.migoto_path == str(moved / "loader")
    assert game.exe_path == str(moved / "game.exe")
    assert store.get_games()[0]["migoto_path"] == str(moved / "loader")
