"""按游戏和父类别分别保存类别与 Mod 自由排序，不改变库数据及物理目录。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from src.core.config import Config
from src.core.config_file_io import backup_invalid_file, read_json, write_json_atomic
from src.utils.logger import logger

_ORDER_KEY = "ui.overview_category_orders"


class CategoryOrderStore:
    """以稳定 ID 分别保存类别及 Mod 顺序，根类别的父 ID 为空字符串。"""

    def __init__(self, config: Config) -> None:
        """读取独立排序文件，并在持久化成功后迁出旧设置中的顺序。"""
        self._config = config
        self._path = config._config_path.with_name("category_orders.json")
        self._writable = True
        self._data: dict[str, Any] = {}
        self._mod_data: dict[str, Any] = {}
        self._load()
        legacy = config.get(_ORDER_KEY)
        if isinstance(legacy, dict) and legacy:
            merged = self._orders()
            for game_id, scopes in legacy.items():
                if not isinstance(scopes, dict):
                    continue
                existing = merged.setdefault(game_id, {})
                if isinstance(existing, dict):
                    for parent_id, ids in scopes.items():
                        if isinstance(ids, list):
                            existing.setdefault(parent_id, deepcopy(ids))
            self._data = merged
            self._persist(merged)
        elif legacy is not None:
            self._remove_legacy()

    @property
    def path(self) -> Path:
        """返回独立排序配置的文件路径。"""
        return self._path

    def _load(self) -> None:
        """读取版本化顺序；坏文件先备份，访问失败或未来版本禁止覆盖。"""
        try:
            payload = read_json(self._path)
            if payload is None and not self._path.exists():
                return
            if not isinstance(payload, dict):
                raise ValueError("排序配置不是对象")
            version = payload.get("schema_version")
            if isinstance(version, int) and version > 2:
                self._writable = False
                logger.warning("类别排序配置版本较新，已禁止覆盖: %s", self._path)
                return
            if version not in (1, 2) or not isinstance(payload.get("games"), dict):
                raise ValueError("排序配置版本或游戏顺序无效")
            if not isinstance(payload.get("mods", {}), dict):
                raise ValueError("Mod 排序配置无效")
            self._data = payload["games"]
            self._mod_data = payload.get("mods", {})
        except (ValueError, UnicodeError) as exc:
            logger.warning("类别排序配置无效，备份后恢复: %s: %s", self._path, exc)
            self._writable = backup_invalid_file(self._path) is not None
        except OSError as exc:
            self._writable = False
            logger.error("类别排序配置读取失败，已保留原文件: %s: %s", self._path, exc)

    def _remove_legacy(self) -> None:
        """仅在排序成功迁出后删除旧设置键，失败可在下次保存时重试。"""
        if self._config.get(_ORDER_KEY) is not None and not self._config.remove_atomic(_ORDER_KEY):
            logger.warning("类别顺序已迁出，但旧设置键暂未清除")

    def _persist(self, orders: dict[str, Any], mod_orders: dict[str, Any] | None = None) -> bool:
        """先原子写入再发布内存状态，失败不覆盖之前有效的顺序。"""
        if not self._writable:
            return False
        if mod_orders is None:
            mod_orders = self._mod_data
        try:
            write_json_atomic(self._path, {"schema_version": 2, "games": orders, "mods": mod_orders})
        except OSError as exc:
            logger.error("类别排序配置保存失败，已保持原顺序: %s: %s", self._path, exc)
            return False
        self._data = deepcopy(orders)
        self._mod_data = deepcopy(mod_orders)
        self._remove_legacy()
        return True

    def has_order(self, game_id: str, parent_id: str, *, item_type: str = "category") -> bool:
        """判断作用域是否保存了列表顺序，空列表也表示已保存。"""
        game_orders = self._orders(item_type).get(game_id)
        return isinstance(game_orders, dict) and isinstance(game_orders.get(parent_id), list)

    def ordered_ids(
        self, game_id: str, parent_id: str, current_ids: list[str], *, item_type: str = "category"
    ) -> list[str]:
        """过滤已删 ID、去重并按传入顺序追加新增 ID，不写回偏好。"""
        game_orders = self._orders(item_type).get(game_id)
        saved_ids = game_orders.get(parent_id) if isinstance(game_orders, dict) else None
        return self._merge_ids(saved_ids, current_ids)

    def save_order(self, game_id: str, parent_id: str, ordered_ids: list[str], *, item_type: str = "category") -> bool:
        """保存完整序列，游戏及父类别 ID 均按字典原键处理；返回持久化结果。"""
        orders = self._orders(item_type)
        game_orders = orders.get(game_id)
        if not isinstance(game_orders, dict):
            game_orders = {}
            orders[game_id] = game_orders
        game_orders[parent_id] = self._merge_ids([], ordered_ids)
        return self._persist(self._data, orders) if item_type == "mod" else self._persist(orders)

    def reconcile_mod_scope(self, game_id: str, parent_id: str, current_ids: list[str]) -> None:
        """完整直属 Mod 快照清除已删或移出的 ID，新增 Mod 追加到当前作用域末尾。"""
        scopes = self._mod_data.get(game_id)
        saved_ids = scopes.get(parent_id) if isinstance(scopes, dict) else None
        if not isinstance(saved_ids, list):
            return
        reconciled = self._merge_ids(saved_ids, current_ids)
        if reconciled != saved_ids:
            self.save_order(game_id, parent_id, reconciled, item_type="mod")

    def reconcile_game(self, game_id: str, characters: list[dict]) -> None:
        """按完整类别快照协调当前游戏已保存的作用域，仅有变化时写回。"""
        orders = self._orders()
        game_orders = orders.get(game_id)
        mod_orders = self._orders("mod")
        mod_game_orders = mod_orders.get(game_id)
        game_orders = game_orders if isinstance(game_orders, dict) else {}
        mod_game_orders = mod_game_orders if isinstance(mod_game_orders, dict) else {}
        if not game_orders and not mod_game_orders:
            return

        parent_ids = {""}
        children: dict[str, list[str]] = {}
        for character in characters:
            if not isinstance(character, dict):
                continue
            category_id = character.get("id")
            if not isinstance(category_id, str) or not category_id:
                continue
            parent_ids.add(category_id)
            parent_id = character.get("parent_id") or ""
            if isinstance(parent_id, str):
                children.setdefault(parent_id, []).append(category_id)

        reconciled = {
            parent_id: self._merge_ids(saved_ids, children.get(parent_id, []))
            for parent_id, saved_ids in game_orders.items()
            if parent_id in parent_ids and isinstance(saved_ids, list)
        }
        reconciled_mods = {
            parent_id: saved_ids
            for parent_id, saved_ids in mod_game_orders.items()
            if parent_id in parent_ids and isinstance(saved_ids, list)
        }
        changed = reconciled != game_orders or reconciled_mods != mod_game_orders
        if reconciled != game_orders:
            orders[game_id] = reconciled
        if reconciled_mods != mod_game_orders:
            mod_orders[game_id] = reconciled_mods
        if changed:
            self._persist(orders, mod_orders)

    def _orders(self, item_type: str = "category") -> dict[str, Any]:
        """读取完整排序字典的独立副本，非法容器降级为空字典。"""
        if item_type not in ("category", "mod"):
            raise ValueError("未知排序对象类型")
        return deepcopy(self._mod_data if item_type == "mod" else self._data)

    @staticmethod
    def _merge_ids(saved_ids: object, current_ids: list[str]) -> list[str]:
        """保留有效且仍存在的已存 ID 顺序，随后按当前顺序去重追加。"""
        if not isinstance(saved_ids, list):
            saved_ids = []
        current_set = {category_id for category_id in current_ids if isinstance(category_id, str) and category_id}
        return list(
            dict.fromkeys(
                category_id
                for category_id in [*saved_ids, *current_ids]
                if isinstance(category_id, str) and category_id in current_set
            )
        )
