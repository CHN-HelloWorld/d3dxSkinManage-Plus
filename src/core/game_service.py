"""通过 config/games.json 管理用户自定义游戏。"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from src.core.game_config_store import GameConfigStore
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_data_dir


@dataclass
class GameConfig:
    """保存游戏身份、资源目录、运行路径及预览等服务层配置。"""

    id: str = ""
    name: str = ""
    name_en: str = ""
    exe_path: str = ""
    migoto_path: str = ""
    mods_path: str = ""  # 「Mod 文件夹」= 部署目录，必须用户显式指定（不从 migoto_path 推导）
    # data/ 下实际的游戏资源目录名（英文名化）。必填且非空（由 Config 的条目契约
    # 保证）；改英文名时由迁移流程重命名物理目录并更新此值。
    resource_dir: str = ""
    # 游戏卡片预览图路径（单张）；详情栏设置时复制进资源目录后回填此路径
    preview: str = ""


class GameService:
    """管理用户创建的游戏配置。

    游戏列表只从 GameConfigStore 读取；初始列表为空，不自动创建游戏。
    """

    _instance: GameService | None = None

    def __new__(cls) -> GameService:
        """返回游戏服务单例，首次分配时标记尚未初始化。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """首次创建时连接配置存储，初始化游戏集合并加载当前选择。"""
        if self._initialized:
            return
        self._initialized = True
        self._store = GameConfigStore()
        self._games: list[GameConfig] = []
        self._current_game_id: str = ""
        self.reload_games()

    def reload_games(self) -> None:
        """重新加载已校验的游戏列表。

        必填字段由存储层校验，无效条目不影响其他游戏；缺失的可选元数据使用空字符串。
        """
        self._games.clear()
        for g in self._store.get_games():
            self._games.append(
                GameConfig(
                    id=g["id"],
                    name=g["name"],
                    name_en=g["name_en"],
                    mods_path=g["mods_path"],
                    resource_dir=g["resource_dir"],
                    exe_path=g.get("exe_path", ""),
                    migoto_path=g.get("migoto_path", ""),
                    preview=g.get("preview", ""),
                )
            )

        # 启动仅恢复有效的 last_game_id，不自动激活首个游戏。

        logger.info("已加载 %d 个游戏配置", len(self._games))

    @property
    def games(self) -> list[GameConfig]:
        """返回游戏对象列表的副本，防止调用方直接增删内部集合。"""
        return list(self._games)

    @property
    def current_game_id(self) -> str:
        """返回当前游戏编号，空字符串表示尚未选择。"""
        return self._current_game_id

    @current_game_id.setter
    def current_game_id(self, game_id: str) -> None:
        """更新内存中的当前游戏编号，不触发配置保存。"""
        self._current_game_id = game_id

    def get_game(self, game_id: str) -> GameConfig | None:
        """按游戏编号查找配置对象，未找到时返回 None。"""
        for g in self._games:
            if g.id == game_id:
                return g
        return None

    def get_resource_dir(self, game_id: str) -> str:
        """返回游戏在 data 下的资源目录名；未知游戏返回空字符串。

        调用方须拒绝空目录名，避免路径拼接产生共享的 data/mods。
        """
        g = self.get_game(game_id)
        if g is None:
            logger.warning("未知游戏 %r，无资源目录可解析", game_id)
            return ""
        return g.resource_dir

    def get_game_list_for_ui(self) -> list[dict]:
        """返回适合 UI 下拉列表的游戏信息"""
        return [{"id": g.id, "name": g.name, "name_en": g.name_en} for g in self._games]

    def get_current_game(self) -> GameConfig | None:
        """返回当前选择对应的游戏配置，未选择或对象已删除时返回 None。"""
        return self.get_game(self._current_game_id)

    # 游戏配置的增删改、排序与资源目录迁移。

    @staticmethod
    def _sanitize_id(name_en: str, name: str = "") -> str:
        """把英文名（优先）/中文名清洗成可用作目录名/配置键的安全字符串。"""
        base = (name_en or name or "").strip()
        for ch in '\\/:.*?"<>|':
            base = base.replace(ch, "")
        return base.strip()

    @classmethod
    def _derive_game_id(cls, name_en: str, name: str, used: set) -> str:
        """从英文名（优先）/中文名派生稳定且唯一的内部 game_id。"""
        base = cls._sanitize_id(name_en, name) or "game"
        gid = base
        n = 2
        while gid in used:
            gid = f"{base} {n}"
            n += 1
        return gid

    def rebase_deploy_dir(self, game_id: str, old_path: str, new_path: str) -> None:
        """在所有者线程保存活动部署根的移动结果。

        写入失败时保持服务和存储的内存状态不变，并抛出 OSError 供调用方回滚部署记录。
        """
        game = self.get_game(game_id)
        if game is None or game.mods_path != old_path:
            raise ValueError(tr("游戏部署目录配置已变化，无法自动更新"))
        updated = [replace(item, mods_path=new_path) if item.id == game_id else item for item in self._games]
        self._store.rebase_deploy_dir(game_id, old_path, new_path)
        self._games = updated

    def save_game(
        self,
        cfg: GameConfig,
        *,
        migrate_resources: bool = True,
        on_migration_error: Callable[[str], None] | None = None,
    ) -> str:
        """保存游戏并返回编号；资源迁移须在宿主暂停相关监听后于主线程调用。

        迁移失败时保留原资源目录，其余字段照常保存，再向调用方报告；仅更新预览时可跳过迁移。
        回滚也未完成时保留实际资源位置并抛出异常，由宿主停用游戏；配置不可写时仅保留内存及错误信息。
        """
        name = (cfg.name or "").strip()
        name_en = (cfg.name_en or "").strip()
        games_list = self._store.get_games()

        gid = (cfg.id or "").strip()
        existing = next((e for e in games_list if e["id"] == gid), None) if gid else None
        if not gid:
            used_ids = {e["id"] for e in games_list}
            gid = self._derive_game_id(name_en, name, used_ids)

        # 所有游戏的资源目录名均由清洗后的英文名称确定，改名时同步迁移目录。
        old_rd = existing["resource_dir"] if existing else ""
        used_dirs = {e["resource_dir"] for e in games_list if e is not existing}
        desired = self._sanitize_id(name_en, name) or gid
        if existing and (not migrate_resources or desired == old_rd):
            new_rd = old_rd
        else:
            new_rd = desired
            n = 2
            while new_rd in used_dirs:
                new_rd = f"{desired} {n}"
                n += 1

        # 仅「已存在的游戏改了目录名」才迁移物理目录 + 重写 DB 路径
        migration_error = ""
        recovery_error: OSError | None = None
        if existing and new_rd != old_rd:
            from src.core.mod_manager import ModManager, ResourceDirectoryMigrationError

            requested_rd = new_rd
            try:
                eff = ModManager.instance().migrate_game_resource_dir(gid, old_rd, new_rd)
                if eff:
                    new_rd = eff
            except ResourceDirectoryMigrationError as exc:
                new_rd = exc.actual_resource_dir
                recovery_error = exc
            except Exception as exc:
                logger.error("游戏资源目录迁移失败 %s: %s -> %s: %s", gid, old_rd, new_rd, exc)
                new_rd = old_rd
            if new_rd != requested_rd and recovery_error is None:
                migration_error = tr(
                    "游戏设置已保存，但资源目录未能迁移。\n\n保留目录：{old_dir}\n目标目录：{new_dir}\n\n"
                    "请确认目标目录不存在且文件未被其他程序占用，然后重新保存。",
                    old_dir=get_data_dir() / old_rd,
                    new_dir=get_data_dir() / requested_rd,
                )

        entry = {
            "id": gid,
            "name": name,
            "name_en": name_en,
            "exe_path": (cfg.exe_path or "").strip(),
            "migoto_path": (cfg.migoto_path or "").strip(),
            "mods_path": (cfg.mods_path or "").strip(),
            "resource_dir": new_rd,
            "preview": (cfg.preview or "").strip(),
        }
        if existing and new_rd != old_rd:
            old_root = get_data_dir() / old_rd
            new_root = get_data_dir() / new_rd
            for field in ("exe_path", "migoto_path", "mods_path", "preview"):
                entry[field] = self._relocate_resource_path(entry[field], old_root, new_root)
        if existing:
            games_list[games_list.index(existing)] = entry
        else:
            games_list.append(entry)

        if not self._store.set_games(games_list):
            save_error = tr("无法写入游戏配置，请检查文件权限或占用后重试。")
            if existing and new_rd != old_rd and recovery_error is None:
                try:
                    effective = ModManager.instance().migrate_game_resource_dir(gid, new_rd, old_rd)
                except ResourceDirectoryMigrationError as exc:
                    recovery_error = exc
                else:
                    if effective != old_rd:
                        recovery_error = ResourceDirectoryMigrationError(new_rd, get_data_dir() / new_rd, [save_error])
            self.reload_games()
            if recovery_error is not None:
                # 配置仍不可写时仅保留可定位的内存状态，异常会使宿主停用游戏并显示实际目录。
                game = self.get_game(gid)
                if game is not None:
                    game.resource_dir = recovery_error.actual_resource_dir
                    for field in ("exe_path", "migoto_path", "mods_path", "preview"):
                        setattr(
                            game,
                            field,
                            self._relocate_resource_path(
                                getattr(game, field), get_data_dir() / old_rd, Path(recovery_error.actual_path)
                            ),
                        )
                raise ResourceDirectoryMigrationError(
                    recovery_error.actual_resource_dir,
                    Path(recovery_error.actual_path),
                    [save_error, *recovery_error.rollback_errors],
                ) from recovery_error
            raise OSError(save_error)
        self.reload_games()
        if recovery_error is not None:
            raise recovery_error
        if migration_error and on_migration_error is not None:
            on_migration_error(migration_error)
        return gid

    @staticmethod
    def _relocate_resource_path(value: str, old_root: Path, new_root: Path) -> str:
        """重定位资源目录内的配置路径，保留外部路径且不解析符号链接。"""
        if not value:
            return value
        try:
            relative = Path(os.path.abspath(value)).relative_to(os.path.abspath(old_root))
        except ValueError:
            return value
        return str(new_root / relative)

    def delete_game(self, game_id: str) -> None:
        """从持久配置中删除游戏条目，清除失效的当前选择后重新加载游戏列表。"""
        games_list = [e for e in self._store.get_games() if e["id"] != game_id]
        self._store.set_games(games_list)
        if self._current_game_id == game_id:
            self._current_game_id = ""
        self.reload_games()

    def reorder_games(self, ordered_ids: list[str]) -> None:
        """按给定 id 顺序重排并持久化（用于卡片拖拽排序）。"""
        games_list = self._store.get_games()
        by_id = {e["id"]: e for e in games_list}
        new_list = [by_id[i] for i in ordered_ids if i in by_id]
        for e in games_list:  # 容错：未列出的补到末尾
            if e["id"] not in ordered_ids:
                new_list.append(e)
        self._store.set_games(new_list)
        self.reload_games()

    def has_active_game(self) -> bool:
        """是否有有效的已激活游戏（导航门禁谓词）。"""
        return bool(self._current_game_id) and self.get_game(self._current_game_id) is not None

    @classmethod
    def instance(cls) -> GameService:
        """返回进程内共享的游戏服务实例。"""
        return cls()
