from __future__ import annotations

import json
import sys
import types

import pytest


def _games_path(temp_data_dir):
    return temp_data_dir / "config" / "games.json"


def _settings_path(temp_data_dir):
    return temp_data_dir / "config" / "settings.json"


def _write(temp_data_dir, **top) -> dict:
    """落一份 games.json（schema_version 正确），返回写入的原始 payload。

    键名与生产一致（`games` / `last_game_id` / …）；其余键名沿用旧用例的写法以便对照，
    未声明的键一律原样保留。
    """
    from src.core.game_config_store import GAMES_SCHEMA_VERSION

    payload = {"schema_version": GAMES_SCHEMA_VERSION}
    payload.update(top)
    p = _games_path(temp_data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def _raw(temp_data_dir) -> dict:
    return json.loads(_games_path(temp_data_dir).read_text(encoding="utf-8"))


def _raw_settings(temp_data_dir) -> dict:
    return json.loads(_settings_path(temp_data_dir).read_text(encoding="utf-8"))


def _invalid_backups(temp_data_dir) -> list:
    return sorted((temp_data_dir / "config").glob("games.json.invalid-*"))


def _entry(gid: str = "Test Game", **over) -> dict:
    """一条合法的游戏条目：必填 id/name/name_en/mods_path/resource_dir。"""
    e = {
        "id": gid,
        "name": "测试游戏",
        "name_en": gid,
        "mods_path": "",
        "resource_dir": gid,
    }
    e.update(over)
    return e


@pytest.fixture
def fresh(temp_data_dir):
    """temp_data_dir 只清 Config/GameConfigStore，GameService 需另清。"""
    from src.core.config import Config
    from src.core.game_config_store import GameConfigStore
    from src.core.game_service import GameService

    Config._instance = None
    GameConfigStore._instance = None
    GameService._instance = None
    yield temp_data_dir
    Config._instance = None
    GameConfigStore._instance = None
    GameService._instance = None


def _service():
    from src.core.game_service import GameService

    return GameService()


def _store():
    from src.core.game_config_store import GameConfigStore

    return GameConfigStore()


class TestNoBuiltinGames:
    """程序不内置任何游戏配置：默认空列表，且没有第二份 `games.<id>.*` 存储。"""

    def test_default_games_is_empty_list(self, fresh):
        from src.core.game_config_store import GameConfigStore

        assert GameConfigStore._DEFAULTS["games"] == []

    def test_legacy_games_section_not_in_program_schema(self, fresh):
        from src.core.config import Config

        assert "games" not in Config._DEFAULT_SETTINGS

    def test_fresh_config_yields_zero_games(self, fresh):
        svc = _service()
        assert svc.games == []
        assert svc.has_active_game() is False

    def test_missing_key_yields_zero_games(self, fresh):
        _write(fresh, games=None)
        from src.core.game_config_store import GameConfigStore

        assert GameConfigStore().get_games() == []
        assert _service().games == []

    def test_none_value_falls_back_without_touching_file(self, fresh):
        _write(fresh, games=None)
        _store()
        assert _raw(fresh)["games"] is None  # 用户文件原样保留
        assert _invalid_backups(fresh) == []  # 不是整份重建

    def test_supported_games_not_imported_by_game_service(self, fresh):
        import src.core.game_service as gs_mod

        assert not hasattr(gs_mod, "SUPPORTED_GAMES")


class TestEntryContract:
    """无效配置条目独立剔除，缺失的可选元数据使用默认值。

    空 resource_dir 会将不同游戏的资料库定位到同一 data/mods 目录，必须拒绝；
    同一文件中的其他有效游戏配置仍应保留。
    """

    @pytest.mark.parametrize(
        "bad",
        [
            {"id": "X", "name": "x", "name_en": "X", "mods_path": ""},  # 缺 resource_dir
            {"id": "X", "name": "x", "name_en": "X", "resource_dir": "X"},  # 缺 mods_path
            {"name": "x", "name_en": "X", "mods_path": "", "resource_dir": "X"},  # 缺 id
        ],
    )
    def test_missing_required_field_drops_only_that_entry(self, fresh, bad):
        _write(fresh, games=[bad])

        assert _store().get_games() == []
        assert _service().games == []
        assert _raw(fresh)["games"] == [bad]  # 用户文件不被改写
        assert _invalid_backups(fresh) == []

    @pytest.mark.parametrize(
        "over",
        [
            {"id": ""},  # 空 id：查不到游戏
            {"resource_dir": ""},  # 空资源目录：data/<游戏>/mods 会塌缩成 data/mods
            {"id": 3},  # 类型错
            {"resource_dir": None},
            {"mods_path": 0},
            {"exe_path": 5},  # 可选元数据出现时也严格校验类型
        ],
    )
    def test_bad_field_value_drops_only_that_entry(self, fresh, over):
        _write(fresh, games=[_entry(**over)])

        assert _store().get_games() == []

    def test_non_dict_entry_is_dropped(self, fresh):
        _write(fresh, games=["ExampleGameA"])

        assert _store().get_games() == []

    def test_optional_metadata_missing_is_adopted(self, fresh):
        _write(fresh, games=[_entry("Example Game A", mods_path="E:/Mods")])
        svc = _service()
        assert [g.id for g in svc.games] == ["Example Game A"]
        g = svc.games[0]
        assert g.mods_path == "E:/Mods"
        assert g.resource_dir == "Example Game A"
        assert (g.exe_path, g.migoto_path, g.preview) == ("", "", "")

    @pytest.mark.parametrize(
        "bad",
        [
            {"id": "B"},  # 缺必填字段
            "not-a-dict",  # 根本不是对象
            {**_entry("B"), "resource_dir": ""},  # 空资源目录
            {**_entry("B"), "exe_path": 5},  # 可选字段类型错
        ],
    )
    def test_one_bad_entry_keeps_the_others(self, fresh, bad):
        """单项游戏配置无效时保留其他有效游戏。"""
        _write(fresh, games=[_entry("A"), bad, _entry("C")])

        assert _store().get_games() != []
        assert [g.id for g in _service().games] == ["A", "C"]

    def test_duplicate_id_keeps_the_first_entry(self, fresh):
        _write(
            fresh,
            games=[
                _entry("A", name="先出现"),
                _entry("A", name="后出现"),
                _entry("B"),
            ],
        )

        games = _service().games
        assert [g.id for g in games] == ["A", "B"]
        assert games[0].name == "先出现"

    def test_unknown_entry_field_is_kept(self, fresh):
        _write(fresh, games=[{**_entry("A"), "future_field": {"x": 1}}])

        assert [g.id for g in _service().games] == ["A"]
        assert _store().get_games()[0]["future_field"] == {"x": 1}

    def test_filtered_entries_are_not_written_back(self, fresh):
        payload = _write(fresh, games=[_entry("A"), {"id": "B"}])

        _store().get_games()

        assert _raw(fresh) == payload

    def test_get_games_returns_copy(self, fresh):
        _write(fresh, games=[_entry("A")])
        store = _store()

        store.get_games()[0]["mods_path"] = "被外部改了"

        assert store.get_games()[0]["mods_path"] == ""


class TestSingleTrack:
    """不读不写旧的 `games.<id>.*` 第二份存储。"""

    def test_reload_ignores_legacy_games_section(self, fresh):
        from src.core.config import Config

        _settings_path(fresh).parent.mkdir(parents=True, exist_ok=True)
        _settings_path(fresh).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "games": {"Example Game A": {"mods_path": "E:/from-legacy-games"}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        Config()
        _write(fresh, games=[_entry("Example Game A", mods_path="E:/from-games-json")])

        svc = _service()
        assert svc.get_game("Example Game A").mods_path == "E:/from-games-json"

    def test_save_game_writes_no_legacy_games_section(self, fresh):
        from src.core.config import Config
        from src.core.game_service import GameConfig

        Config()  # 建立程序设置文件
        svc = _service()
        svc.save_game(GameConfig(name="测试", name_en="Test Game", mods_path="E:/M"))

        assert "games" not in _raw_settings(fresh)  # 程序设置里没有游戏段
        assert isinstance(_raw(fresh)["games"], list)  # 游戏列表在 games.json 里

    def test_get_resource_dir_unknown_game_is_empty(self, fresh):
        assert _service().get_resource_dir("ghost") == ""

    def test_save_game_persists_all_required_fields(self, fresh):
        from src.core.game_service import GameConfig

        svc = _service()
        gid = svc.save_game(GameConfig(name="测试", name_en="Test Game"))
        entry = next(e for e in _raw(fresh)["games"] if e["id"] == gid)
        for key in ("id", "name", "name_en", "mods_path", "resource_dir"):
            assert key in entry and isinstance(entry[key], str)
        assert entry["resource_dir"] == "Test Game"


class _FakeModManager:
    """`migrate_game_resource_dir` 的替身（避免在业务层单测里拉起真 ModManager）。"""

    def __init__(self, *, boom: bool = False, effective: str | None = None) -> None:
        self.boom = boom
        self.effective = effective
        self.calls: list[tuple[str, str, str]] = []

    def migrate_game_resource_dir(self, gid: str, old: str, new: str) -> str:
        self.calls.append((gid, old, new))
        if self.boom:
            raise RuntimeError("磁盘迁移失败")
        return self.effective or new


@pytest.fixture
def fake_mm(monkeypatch):
    from src.core.mod_manager import ResourceDirectoryMigrationError

    def _install(**kw):
        stub = _FakeModManager(**kw)
        monkeypatch.setitem(
            sys.modules,
            "src.core.mod_manager",
            types.SimpleNamespace(
                ModManager=types.SimpleNamespace(instance=lambda: stub),
                ResourceDirectoryMigrationError=ResourceDirectoryMigrationError,
            ),
        )
        return stub

    return _install


class TestResourceDirMigration:
    """改英文名 → 资源目录事务迁移；失败回滚且不写半套配置。"""

    def test_rename_english_name_migrates_resource_dir(self, fresh, fake_mm):
        from src.core.game_service import GameConfig

        _write(
            fresh,
            games=[
                _entry("game_a", name="示例游戏甲", name_en="Example Game A", resource_dir="Example Game A"),
            ],
        )
        stub = fake_mm()
        svc = _service()
        svc.save_game(GameConfig(id="game_a", name="示例游戏甲", name_en="ExampleGameA 2"))
        assert stub.calls == [("game_a", "Example Game A", "ExampleGameA 2")]
        assert svc.get_resource_dir("game_a") == "ExampleGameA 2"

    def test_migration_failure_keeps_old_resource_dir(self, fresh, fake_mm):
        from src.core.game_service import GameConfig

        _write(
            fresh,
            games=[
                _entry("game_a", name="示例游戏甲", name_en="Example Game A", resource_dir="Example Game A"),
            ],
        )
        fake_mm(boom=True)
        svc = _service()
        svc.save_game(GameConfig(id="game_a", name="示例游戏甲", name_en="ExampleGameA 2"))
        assert svc.get_resource_dir("game_a") == "Example Game A"
        entry = _raw(fresh)["games"][0]
        assert entry["resource_dir"] == "Example Game A"  # 不写半套
        assert entry["name_en"] == "ExampleGameA 2"  # 名称照旧保存

    def test_unchanged_english_name_does_not_migrate(self, fresh, fake_mm):
        from src.core.game_service import GameConfig

        _write(
            fresh,
            games=[
                _entry("game_a", name="示例游戏甲", name_en="Example Game A", resource_dir="Example Game A"),
            ],
        )
        stub = fake_mm()
        svc = _service()
        svc.save_game(GameConfig(id="game_a", name="示例游戏甲改", name_en="Example Game A"))
        assert stub.calls == []
        assert svc.get_resource_dir("game_a") == "Example Game A"
