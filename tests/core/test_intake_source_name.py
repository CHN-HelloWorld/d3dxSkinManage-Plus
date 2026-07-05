"""验证原始来源文件名只影响新导入容器的默认名称。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.core.mod_intake import ModIntakeService
from src.core.mod_manager import ModManager
from src.core.mod_package import MANIFEST_KIND, read_sidecar
from tests.helpers import register_games, reset_game_service

CACHE_NAME = "0123456789abcdef0123456789abcdef__11__payload.zip"
CACHE_STEM = "0123456789abcdef0123456789abcdef__11__payload"


@pytest.fixture
def library(temp_data_dir):
    """创建独立游戏和导入目标，结束后关闭资源监视器并恢复游戏服务。"""
    register_games("game-a")
    manager = ModManager()
    manager.set_game("game-a")
    node = manager.add_character(name="Character", node_type="character")
    yield manager, node
    manager.stop_ini_sync()
    manager.variants.stop_watch()
    reset_game_service()


def _archive(path: Path, *, shared_name: str = "") -> Path:
    """写入内容各异的真实归档，按需附带分享名称。"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mod.ini", "[Constants]\nx = 1\n")
        archive.writestr("body.buf", path.stem.encode("utf-8"))
        if shared_name:
            manifest = {"kind": MANIFEST_KIND, "schema": 2, "name": shared_name, "info": {}, "previews": []}
            archive.writestr(".d3dxmeta/info.json", json.dumps(manifest))
    return path


@pytest.mark.parametrize(
    ("source_name", "expected"),
    [
        ("skin.v2.zip", "skin.v2"),
        ("../nested/skin.v2.zip", "skin.v2"),
        (r"C:\incoming\skin.zip", "skin"),
        ("  skin?.zip  ", "skin_"),
        ("资源.v2.zip", "资源.v2"),
        ("..", CACHE_STEM),
        ("CON.zip", CACHE_STEM),
        ("", CACHE_STEM),
    ],
)
def test_source_name_uses_safe_stem_and_preserves_source(library, tmp_path, source_name, expected):
    """原始文件名按单一 Windows 基名处理，无效名称回退且不改动手动导入源。"""
    manager, node = library
    source = _archive(tmp_path / CACHE_NAME)
    original = source.read_bytes()
    service = ModIntakeService(manager)
    request = service.plan_request(
        source_path=source,
        game_id="game-a",
        node_id=node.id,
        source_name=source_name,
    )
    mod = service.commit(service.prepare(request))
    body = Path(mod.path)
    assert mod.name == expected
    assert mod.folder_name == expected
    assert body.parent == manager._get_character_library(node.id) / expected
    assert body.name == expected + ".zip"
    assert read_sidecar(body)["name"] == expected
    assert body.read_bytes() == original
    assert source.read_bytes() == original


@pytest.mark.parametrize(
    ("display_name", "expected"),
    [("", "Formal package"), ("Custom name", "Custom name"), ("skin", "skin")],
)
def test_original_filename_stays_below_explicit_and_shared_names(library, tmp_path, display_name, expected):
    manager, node = library
    source = _archive(tmp_path / CACHE_NAME, shared_name="Formal package")
    service = ModIntakeService(manager)
    request = service.plan_request(
        source_path=source,
        game_id="game-a",
        node_id=node.id,
        display_name=display_name,
        source_name="skin.zip",
    )
    mod = service.commit(service.prepare(request))
    assert mod.name == expected
    assert read_sidecar(mod.path)["name"] == expected
    assert manager.get_mod(mod.mod_id).name == expected
    assert Path(mod.path).parent.name == "skin"


def test_original_filename_collision_allocates_new_container_without_renaming_existing_mod(library, tmp_path):
    """相同原始文件名的不同内容分配独立容器，已入库条目保持名称和文件内容。"""
    manager, node = library
    service = ModIntakeService(manager)
    first_source = _archive(tmp_path / "first.zip")
    second_source = _archive(tmp_path / "second.zip")
    original = first_source.read_bytes()
    first = service.commit(
        service.prepare(
            service.plan_request(source_path=first_source, game_id="game-a", node_id=node.id, source_name="skin.zip")
        )
    )
    second = service.commit(
        service.prepare(
            service.plan_request(source_path=second_source, game_id="game-a", node_id=node.id, source_name="skin.zip")
        )
    )
    assert manager.get_mod(first.mod_id).name == "skin"
    assert Path(first.path).read_bytes() == original
    assert second.name == "skin_1"
    assert Path(second.path).parent.name == "skin_1"
    assert Path(second.path).name == "skin_1.zip"
    assert first.mod_id != second.mod_id


def test_original_filename_preserves_version_dots_during_archive_conversion(library, tmp_path):
    manager, node = library
    source = _archive(tmp_path / CACHE_NAME)
    service = ModIntakeService(manager)
    request = service.plan_request(
        source_path=source,
        game_id="game-a",
        node_id=node.id,
        source_name="skin.v2.zip",
        target_format="7z",
        strict_format=True,
    )
    mod = service.commit(service.prepare(request))
    assert mod.name == "skin.v2"
    assert Path(mod.path).parent.name == "skin.v2"
    assert Path(mod.path).name == "skin.v2.7z"
    assert read_sidecar(mod.path)["name"] == "skin.v2"
