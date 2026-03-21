"""验证游戏列表原子保存失败时的内存回退与后续重试。"""

from __future__ import annotations

import json

import pytest

import src.core.game_config_store as game_config_module
from src.core.game_config_store import GameConfigStore


def _game(preview: str, *, name: str) -> dict:
    """构造含预览路径的有效中性游戏配置，保留额外条目字段。"""
    return {
        "id": "game-one",
        "name": name,
        "name_en": name,
        "resource_dir": "LibraryOne",
        "mods_path": "",
        "preview": preview,
        "custom_entry": {"value": "preserved"},
    }


@pytest.mark.parametrize("has_games", [True, False])
def test_failed_game_list_save_keeps_memory_and_disk_then_can_retry(temp_data_dir, monkeypatch, has_games):
    """真实原子写入入口报错后保留原始内存和磁盘，重试成功时仍按既有规则存储预览路径。"""
    preview = temp_data_dir / "data" / "LibraryOne" / "new-preview.png"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"preview")
    original = {
        "schema_version": 1,
        "last_game_id": "game-one",
        "current_presets": {"game-one": "preset-one"},
        "gamebanana_overrides": {"community-key": 42},
        "custom_store": {"values": [1, 2]},
    }
    if has_games:
        original["games"] = [_game("data/LibraryOne/old-preview.png", name="Original")]
    path = temp_data_dir / "config" / "games.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    before_disk = path.read_bytes()
    store = GameConfigStore()
    before_games = store.get_games()
    requested = [_game(str(preview), name="Updated")]

    def unavailable_file(path, data) -> None:
        """模拟原子写入因配置文件权限而失败，不更改已有文件。"""
        raise PermissionError("配置文件不可写")

    with monkeypatch.context() as blocked:
        blocked.setattr(game_config_module, "write_json_atomic", unavailable_file)
        assert store.set_games(requested) is False

    assert store.get_games() == before_games
    assert store._data == original
    assert path.read_bytes() == before_disk
    assert requested[0]["preview"] == str(preview)

    assert store.set_games(requested) is True

    assert store.get_games() == requested
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["games"] == [_game("data/LibraryOne/new-preview.png", name="Updated")]
    assert {key: value for key, value in saved.items() if key != "games"} == {
        key: value for key, value in original.items() if key != "games"
    }
