"""游戏配置的单例存储，统一读写 config/games.json。

保存游戏列表、上次激活的游戏、各游戏当前预设和社区映射。
该文件与程序偏好独立，重置设置不会删除游戏数据。

本类负责存取和条目校验；游戏操作由 GameService 执行，预设内容由 PresetManager 管理。
缺省值只在读取时提供，无效条目单独过滤，整份文件不可读时才备份并重新初始化。
"""

from __future__ import annotations

import json
from typing import Any

from src.core.config_file_io import backup_invalid_file, read_json, write_json_atomic
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_config_dir, resolve_stored_path, to_stored_path

# 游戏配置的版本标记仅用于诊断，不作为拒绝读取配置的条件。
GAMES_SCHEMA_VERSION = 1


class GameConfigStore:
    """游戏配置存储管理器 - 单例模式"""

    _instance: GameConfigStore | None = None

    _DEFAULTS: dict[str, Any] = {
        "schema_version": GAMES_SCHEMA_VERSION,
        "games": [],
        "last_game_id": "",
        "current_presets": {},
        "gamebanana_overrides": {},
    }

    # 游戏必填字段须为字符串，id 和 resource_dir 还须非空；可选元数据出现时校验类型。
    # 空资源目录会使多个游戏共用 data/mods，必须拒绝该配置。
    _GAME_REQUIRED_KEYS: tuple[str, ...] = ("id", "name", "name_en", "mods_path", "resource_dir")
    _GAME_NONEMPTY_KEYS: tuple[str, ...] = ("id", "resource_dir")
    _GAME_OPTIONAL_KEYS: tuple[str, ...] = ("exe_path", "migoto_path", "preview")

    def __new__(cls) -> GameConfigStore:
        """复用游戏配置存储单例，并为首次初始化设置状态标记。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """首次构造时绑定 games.json 路径并加载游戏配置数据。"""
        if self._initialized:
            return
        self._initialized = True
        self._path = get_config_dir() / "games.json"
        self._data: dict[str, Any] = {}
        self._load()

    # 加载与校验

    @classmethod
    def _defaults_copy(cls) -> dict[str, Any]:
        """复制默认游戏配置的完整 JSON 数据，避免共享嵌套容器。"""
        return json.loads(json.dumps(cls._DEFAULTS))

    def _load(self) -> None:
        """读取游戏配置。缺文件写默认结构；存在则原样采纳（缺键不补、未知键保留）。"""
        try:
            raw = read_json(self._path)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            self._rebuild_from_defaults(f"无法解析: {e}")
            return

        if raw is None:
            # 首次安装：建默认结构（空游戏列表）。**程序不内置任何游戏配置**。
            self._data = self._defaults_copy()
            self._save()
            return

        if not isinstance(raw, dict):
            self._rebuild_from_defaults(f"根节点是 {type(raw).__name__}，不是对象")
            return

        version = raw.get("schema_version")
        if version != GAMES_SCHEMA_VERSION:
            logger.info(
                "游戏配置 schema_version=%r 与本程序的 %d 不同，按「缺失即默认」照常读取",
                version,
                GAMES_SCHEMA_VERSION,
            )
        self._data = raw
        logger.info("游戏配置已加载: %s", self._path)

    def _rebuild_from_defaults(self, reason: str) -> None:
        """备份无法解析的游戏配置后恢复默认结构。

        备份失败时保留原文件，避免覆盖仍可用于人工恢复的内容。
        """
        logger.error("游戏配置文件无效（%s），将备份并重建: %s", reason, self._path)
        if backup_invalid_file(self._path) is None:
            logger.error("备份失败，保留原文件不覆盖；本次以空游戏列表运行")
            self._data = self._defaults_copy()
            return
        self._data = self._defaults_copy()
        self._save()

    def _save(self) -> bool:
        """写入游戏配置，失败时记录错误。

        此方法也用于部署事务提交后的指针保存，写入失败不能触发已完成部署的回滚。
        """
        try:
            write_json_atomic(self._path, self._data)
        except OSError as e:
            logger.error("游戏配置保存失败: %s", e)
            return False
        logger.debug("游戏配置已保存")
        return True

    # 游戏条目契约

    @classmethod
    def _game_entry_ok(cls, entry: Any) -> bool:
        """单个游戏条目是否满足契约（必填字段齐全且为字符串、id/resource_dir 非空）。"""
        if not isinstance(entry, dict):
            return False
        if any(not isinstance(entry.get(k), str) for k in cls._GAME_REQUIRED_KEYS):
            return False
        if any(not entry[k].strip() for k in cls._GAME_NONEMPTY_KEYS):
            return False
        return not any(k in entry and not isinstance(entry[k], str) for k in cls._GAME_OPTIONAL_KEYS)

    @classmethod
    def _filter_game_entries(cls, value: list) -> list:
        """逐条校验游戏配置，跳过无效条目和重复标识，保留其他有效配置。

        记录被跳过的原始内容供诊断，过滤结果不会直接回写磁盘。
        """
        kept: list = []
        seen: set[str] = set()
        for entry in value:
            if not cls._game_entry_ok(entry):
                logger.error("游戏配置条目不合法，已跳过该条（其余游戏不受影响）；原始内容: %r", entry)
                continue
            game_id = entry["id"].strip()
            if game_id in seen:
                logger.error("游戏配置 id 重复，已跳过后出现的这条: %s", game_id)
                continue
            seen.add(game_id)
            kept.append(entry)
        return kept

    # 四类游戏值的显式读写

    def get_games(self) -> list[dict]:
        """返回已过滤无效条目的游戏列表副本，修改结果不影响存储状态。"""
        raw = self._data.get("games")
        if not isinstance(raw, list):
            return []
        games = json.loads(json.dumps(self._filter_game_entries(raw)))
        for game in games:
            if "preview" in game:
                game["preview"] = resolve_stored_path(game["preview"])
        return games

    def set_games(self, games: list[dict]) -> bool:
        """复制并持久化游戏列表，保持预览图存储规则，写入失败时恢复原始内存状态。"""
        stored = json.loads(json.dumps(games))
        for game in stored:
            if isinstance(game, dict) and isinstance(game.get("preview"), str):
                game["preview"] = to_stored_path(game["preview"])
        previous = self._data
        self._data = {**previous, "games": stored}
        if self._write():
            return True
        self._data = previous
        return False

    def rebase_deploy_dir(self, game_id: str, old_path: str, new_path: str) -> None:
        """持久化已验证的资源根移动后再发布内存状态。

        该操作参与部署路径调整，原子写入失败须向所有者传播。
        保留原始条目中的扩展字段和预览路径。
        """
        candidate = json.loads(json.dumps(self._data))
        games = candidate.get("games")
        entries = games if isinstance(games, list) else []
        matches = [game for game in entries if isinstance(game, dict) and game.get("id") == game_id]
        if len(matches) != 1 or matches[0].get("mods_path") != old_path:
            raise ValueError(tr("游戏部署目录配置已变化，无法自动更新"))
        matches[0]["mods_path"] = new_path
        write_json_atomic(self._path, candidate)
        self._data = candidate

    def get_last_game_id(self) -> str:
        """返回最后选择的游戏编号；存储值类型无效时返回空字符串。"""
        value = self._data.get("last_game_id", "")
        return value if isinstance(value, str) else ""

    def set_last_game_id(self, game_id: str) -> bool:
        """更新最后选择的游戏编号并保存，返回写入结果。"""
        self._data["last_game_id"] = game_id
        return self._write()

    def get_current_preset_id(self, game_id: str) -> str | None:
        """某游戏当前激活的预设 id；未设置返回 None（由 PresetManager 回落默认预设）。"""
        mapping = self._data.get("current_presets")
        if not isinstance(mapping, dict):
            return None
        value = mapping.get(game_id)
        return value if isinstance(value, str) else None

    def set_current_preset_id(self, game_id: str, preset_id: str) -> bool:
        """更新指定游戏的当前预设映射并持久化，返回写入结果。"""
        mapping = self._data.get("current_presets")
        if not isinstance(mapping, dict):
            mapping = {}
            self._data["current_presets"] = mapping
        mapping[game_id] = preset_id
        return self._write()

    def get_gamebanana_overrides(self) -> dict:
        """返回 GameBanana 社区游戏映射的副本。

        映射键由商店页的 gb_override_key 定义，存储层原样保存。
        """
        mapping = self._data.get("gamebanana_overrides")
        if not isinstance(mapping, dict):
            return {}
        return json.loads(json.dumps(mapping))

    def set_gamebanana_overrides(self, overrides: dict) -> bool:
        """替换 GameBanana 覆盖配置并保存，返回写入是否成功。"""
        self._data["gamebanana_overrides"] = overrides
        return self._write()

    # 杂项

    def _write(self) -> bool:
        """写入前补上版本标记（只写不读），再写入磁盘。"""
        self._data["schema_version"] = GAMES_SCHEMA_VERSION
        return self._save()

    def reload(self) -> None:
        """重新读取磁盘游戏配置并执行加载时的默认值及结构处理。"""
        self._load()

    @property
    def path(self):
        """返回当前游戏配置文件的路径对象。"""
        return self._path

    @classmethod
    def instance(cls) -> GameConfigStore:
        """返回进程内共享的游戏配置存储实例。"""
        return cls()
