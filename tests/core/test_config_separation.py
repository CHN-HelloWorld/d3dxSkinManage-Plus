from __future__ import annotations

import json

import pytest

# 路径 / 读写小工具


def _settings_path(root):
    return root / "config" / "settings.json"


def _games_path(root):
    return root / "config" / "games.json"


def _write_json(path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _entry(gid: str = "AA", **over) -> dict:
    e = {
        "id": gid,
        "name": gid,
        "name_en": gid,
        "exe_path": "",
        "migoto_path": "",
        "mods_path": "",
        "resource_dir": gid,
        "preview": "",
    }
    e.update(over)
    return e


def _write_games(root, **top) -> dict:
    """落一份合法 games.json，返回写入的 payload。"""
    from src.core.game_config_store import GAMES_SCHEMA_VERSION

    payload = {
        "schema_version": GAMES_SCHEMA_VERSION,
        "games": [_entry()],
        "last_game_id": "AA",
        "current_presets": {"AA": "default_AA"},
        "gamebanana_overrides": {},
    }
    payload.update(top)
    _write_json(_games_path(root), payload)
    return payload


def _write_settings(root, **top) -> dict:
    from src.core.config import SETTINGS_SCHEMA_VERSION

    payload = {"schema_version": SETTINGS_SCHEMA_VERSION, "general": {"theme": "light"}}
    payload.update(top)
    _write_json(_settings_path(root), payload)
    return payload


@pytest.fixture
def fresh(temp_data_dir):
    """两个存储 + GameService 都要清，否则单例把上个用例的文件路径带过来。"""
    from src.core.config import Config
    from src.core.game_config_store import GameConfigStore
    from src.core.game_service import GameService

    for cls in (Config, GameConfigStore, GameService):
        cls._instance = None
    yield temp_data_dir
    for cls in (Config, GameConfigStore, GameService):
        cls._instance = None


def _store():
    from src.core.game_config_store import GameConfigStore

    return GameConfigStore()


class TestResetBoundary:
    """恢复程序偏好时保持游戏配置文件的字节内容不变。"""

    def test_reset_does_not_touch_games_file(self, fresh):
        from src.core.config import Config

        _write_games(fresh)
        _write_settings(fresh)
        before = _games_path(fresh).read_bytes()

        Config().reset()

        assert _games_path(fresh).read_bytes() == before

    def test_reset_restores_program_defaults(self, fresh):
        from src.core.config import Config

        _write_games(fresh)
        _write_settings(fresh, general={"theme": "light", "language": "en-US"})
        cfg = Config()
        assert cfg.get("general.theme") == "light"

        cfg.reset()

        assert cfg.get("general.theme") == Config._DEFAULT_SETTINGS["general"]["theme"]

    def test_reset_keeps_games_after_reload(self, fresh):
        from src.core.config import Config
        from src.core.game_config_store import GameConfigStore
        from src.core.game_service import GameService

        _write_games(fresh)
        _write_settings(fresh)
        Config().reset()

        GameConfigStore._instance = None
        GameService._instance = None
        store = _store()
        assert [g["id"] for g in store.get_games()] == ["AA"]
        assert store.get_last_game_id() == "AA"
        assert store.get_current_preset_id("AA") == "default_AA"
        assert [g.id for g in GameService().games] == ["AA"]

    def test_reset_reports_failure_without_publishing_defaults(self, fresh, monkeypatch):
        """写盘失败 → 保持原值并如实返回失败，不能先清内存再让保存失败（那样界面显示
        默认值、磁盘还是旧值，用户以为重置成功）。"""
        from src.core import config as config_mod
        from src.core.config import Config

        _write_settings(fresh, general={"theme": "light"})
        cfg = Config()

        def _boom(*_a, **_kw):
            raise OSError("磁盘只读")

        monkeypatch.setattr(config_mod, "write_json_atomic", _boom)
        assert cfg.reset() is False
        assert cfg.get("general.theme") == "light"


# 2. 两个文件的职责边界


class TestFileOwnership:
    """哪些键落哪个文件，是这次拆分的全部意义。"""

    @pytest.mark.parametrize(
        "key",
        [
            "custom_games",
            "presets",
            "games",
            "last_game_id",
            "current_presets",
        ],
    )
    def test_game_keys_gone_from_program_defaults(self, fresh, key):
        from src.core.config import Config

        assert key not in Config._DEFAULT_SETTINGS

    def test_last_game_id_gone_from_general(self, fresh):
        from src.core.config import Config

        assert "last_game_id" not in Config._DEFAULT_SETTINGS["general"]

    def test_gb_overrides_gone_from_program_defaults(self, fresh):
        from src.core.config import Config

        assert "game_id_overrides" not in Config._DEFAULT_SETTINGS["gamebanana"]

    def test_program_keys_stay_in_settings(self, fresh):
        from src.core.config import Config

        defaults = Config._DEFAULT_SETTINGS
        assert defaults["mods"]["delete_method"]  # 全局删除规则仍属程序设置
        assert defaults["gamebanana"]["update_check"]  # 全局检查策略同理

    def test_game_writes_do_not_touch_settings_file(self, fresh):
        from src.core.game_service import GameConfig, GameService

        _write_settings(fresh)
        _write_games(fresh, games=[], last_game_id="", current_presets={})
        from src.core.config import Config

        Config()  # 建立程序设置文件的既有内容
        before = _settings_path(fresh).read_bytes()

        svc = GameService()
        gid = svc.save_game(GameConfig(name="甲", name_en="Alpha", mods_path="E:/m"))
        svc.reorder_games([gid])
        svc.delete_game(gid)

        assert _settings_path(fresh).read_bytes() == before

    def test_gb_override_writes_only_games_file(self, fresh):
        from src.core.config import Config

        _write_settings(fresh)
        _write_games(fresh)
        Config()
        before = _settings_path(fresh).read_bytes()

        store = _store()
        store.set_gamebanana_overrides({"alpha": 101})

        assert store.get_gamebanana_overrides() == {"alpha": 101}
        assert _settings_path(fresh).read_bytes() == before
        assert json.loads(_games_path(fresh).read_text(encoding="utf-8"))["gamebanana_overrides"] == {"alpha": 101}

    def test_preset_pointer_writes_only_games_file(self, fresh):
        from src.core.config import Config

        _write_settings(fresh)
        _write_games(fresh)
        Config()
        before = _settings_path(fresh).read_bytes()

        store = _store()
        store.set_current_preset_id("AA", "p-123")

        assert store.get_current_preset_id("AA") == "p-123"
        assert _settings_path(fresh).read_bytes() == before


# 3. 损坏 / 缺失互不牵连


class TestIndependentFailure:
    """任一配置损坏不得影响另一份独立配置。"""

    def test_broken_settings_does_not_rebuild_games(self, fresh):
        from src.core.config import Config

        _write_games(fresh)
        _settings_path(fresh).parent.mkdir(parents=True, exist_ok=True)
        _settings_path(fresh).write_text("{ 这不是 JSON", encoding="utf-8")
        before = _games_path(fresh).read_bytes()

        Config()  # 触发「备份 + 重建默认」

        assert _games_path(fresh).read_bytes() == before
        assert [g["id"] for g in _store().get_games()] == ["AA"]

    def test_broken_games_does_not_rebuild_settings(self, fresh):
        from src.core.config import Config

        _write_settings(fresh, general={"theme": "light"})
        Config()
        before = _settings_path(fresh).read_bytes()
        _games_path(fresh).write_text("{ 坏了", encoding="utf-8")

        store = _store()

        assert store.get_games() == []
        assert _settings_path(fresh).read_bytes() == before
        assert sorted((fresh / "config").glob("games.json.invalid-*"))

    def test_missing_games_file_creates_empty_list(self, fresh):
        store = _store()

        assert store.get_games() == []
        assert store.get_last_game_id() == ""
        assert store.get_gamebanana_overrides() == {}
        assert _games_path(fresh).exists()

    def test_missing_settings_file_does_not_create_games(self, fresh):
        from src.core.config import Config

        Config()

        assert not _games_path(fresh).exists()


# 4. 游戏条目契约（从 Config 原样移入游戏存储）


class TestGameEntryContract:
    def test_bad_entry_skipped_only_that_one(self, fresh):
        _write_games(fresh, games=[_entry("A"), {"id": "B"}, _entry("C")])

        assert [g["id"] for g in _store().get_games()] == ["A", "C"]

    def test_duplicate_id_keeps_first(self, fresh):
        _write_games(fresh, games=[_entry("A", mods_path="first"), _entry("A", mods_path="second")])

        games = _store().get_games()
        assert len(games) == 1
        assert games[0]["mods_path"] == str(fresh / "first")

    def test_empty_resource_dir_rejected(self, fresh):
        _write_games(fresh, games=[_entry("A", resource_dir="")])

        assert _store().get_games() == []

    def test_unknown_field_preserved(self, fresh):
        _write_games(fresh, games=[{**_entry("A"), "future_field": {"x": 1}}])

        assert _store().get_games()[0]["future_field"] == {"x": 1}

    def test_filtered_entries_not_written_back(self, fresh):
        payload = _write_games(fresh, games=[_entry("A"), {"id": "B"}])
        _store().get_games()

        assert json.loads(_games_path(fresh).read_text(encoding="utf-8")) == payload

    def test_get_games_returns_copy(self, fresh):
        _write_games(fresh)
        store = _store()

        store.get_games()[0]["mods_path"] = "被外部改了"

        assert store.get_games()[0]["mods_path"] == ""


class TestSchemaTolerance:
    def test_missing_keys_use_defaults_without_rewrite(self, fresh):
        _write_json(_games_path(fresh), {"games": [_entry("A")]})
        store = _store()

        assert store.get_last_game_id() == ""
        assert store.get_current_preset_id("A") is None
        assert store.get_gamebanana_overrides() == {}
        assert json.loads(_games_path(fresh).read_text(encoding="utf-8")) == {"games": [_entry("A")]}

    def test_unknown_version_not_discarded(self, fresh):
        _write_games(fresh, schema_version=999)

        assert [g["id"] for g in _store().get_games()] == ["AA"]

    def test_unknown_top_level_key_preserved_on_write(self, fresh):
        _write_games(fresh, future_top={"x": 1})
        store = _store()
        store.set_last_game_id("AA")

        raw = json.loads(_games_path(fresh).read_text(encoding="utf-8"))
        assert raw["future_top"] == {"x": 1}

    def test_last_game_id_pointing_to_missing_game_not_auto_selected(self, fresh):
        _write_games(fresh, games=[_entry("A")], last_game_id="ghost")
        store = _store()

        assert store.get_last_game_id() == "ghost"
        assert [g["id"] for g in store.get_games()] == ["A"]
