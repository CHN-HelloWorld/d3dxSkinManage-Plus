"""验证资源更名后的配置路径重定位与预览独立保存。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.core.game_service import GameConfig, GameService
from src.core.mod_manager import ModManager


@pytest.fixture
def resource_service(temp_data_dir):
    """为资源配置测试重建游戏服务，并关闭迁移过程中创建的索引。"""
    GameService._instance = None
    yield GameService()
    manager = ModManager._instance
    if manager is not None:
        manager.stop_ini_sync()
        manager.variants.stop_watch()
        manager._index.close()
    GameService._instance = None


@pytest.mark.parametrize("field", ["exe_path", "migoto_path", "mods_path", "preview"])
@pytest.mark.parametrize("internal", [False, True])
def test_resource_move_relocates_only_internal_config_paths(resource_service, temp_data_dir, field, internal):
    service = resource_service
    game_id = service.save_game(GameConfig(name="示例游戏", name_en="Original Resource"))
    old_root = temp_data_dir / "data" / "Original Resource"
    old_root.mkdir(parents=True, exist_ok=True)
    parent = old_root if internal else temp_data_dir / "external"
    parent.mkdir(parents=True, exist_ok=True)
    item = parent / "example.dat"
    item.write_bytes(b"preserved resource")
    config = replace(service.get_game(game_id), **{field: str(item)}, name_en="Renamed Resource")

    service.save_game(config)

    expected = temp_data_dir / "data" / "Renamed Resource" / item.name if internal else item
    assert getattr(service.get_game(game_id), field) == str(expected)
    assert expected.read_bytes() == b"preserved resource"


def test_preview_only_save_does_not_retry_failed_resource_move(resource_service, temp_data_dir):
    service = resource_service
    game_id = service.save_game(GameConfig(name="示例游戏", name_en="Original Resource"))
    old_root = temp_data_dir / "data" / "Original Resource"
    new_root = temp_data_dir / "data" / "Renamed Resource"
    old_root.mkdir(parents=True, exist_ok=True)
    new_root.mkdir()
    notices = []
    service.save_game(replace(service.get_game(game_id), name_en="Renamed Resource"), on_migration_error=notices.append)
    assert len(notices) == 1
    new_root.rmdir()
    preview = old_root / "_game_preview.png"
    preview.write_bytes(b"preview data")

    service.save_game(replace(service.get_game(game_id), preview=str(preview)), migrate_resources=False)

    assert service.get_resource_dir(game_id) == "Original Resource"
    assert service.get_game(game_id).preview == str(preview)
    assert preview.read_bytes() == b"preview data"
    assert not new_root.exists()
