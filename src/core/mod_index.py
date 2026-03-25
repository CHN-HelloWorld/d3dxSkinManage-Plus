"""统一保存持久身份，绝对路径和部署状态由投影提供。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from src.core.library_paths import LibraryPaths
from src.core.library_schema import (
    SCHEMA_VERSION,
    TABLES,
    create_schema,
    register_connection,
    valid_relative,
)
from src.core.tag_store import sync_mod_tags
from src.models.deployment import DeploymentRecord, DeploymentState, PresetApplyJournal
from src.models.mod import Mod
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_app_dir, get_data_dir

DB_SCHEMA_VERSION = SCHEMA_VERSION
CURRENT_TABLES = set(TABLES)


def ensure_schema(conn):
    """只读检查索引结构，启动时不转换或重建已有数据。"""
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        return tr("索引结构不符合当前格式，原数据库已保留")
    reference = sqlite3.connect(":memory:")
    try:
        create_schema(reference)
        for table in TABLES:
            expected = [tuple(r)[1:] for r in reference.execute(f"PRAGMA table_info({table})")]
            actual = [tuple(r)[1:] for r in conn.execute(f"PRAGMA table_info({table})")]
            if actual != expected:
                return tr("索引表 {table} 不符合当前格式，原数据库已保留", table=table)
        return ""
    finally:
        reference.close()


_SELECT = (
    "SELECT m.*,d.state AS deployment_state,d.actual_path AS actual_path "
    "FROM mods m LEFT JOIN deployment_records d ON d.mod_id=m.mod_id "
)


class ModIndex:
    """管理资料库 SQLite 索引、路径投影、部署记录及可恢复预设事务日志。"""

    def __init__(self, game_id=""):
        """初始化数据库状态和行缓存，成功打开数据库后建立游戏路径服务。"""
        self.game_id = game_id
        self._db_path = get_data_dir() / "mod_index.db"
        self._conn = None
        self.schema_error = ""
        self.open_error = ""
        self._library_rows_cache = None
        self._init_db()
        if self._conn is not None:
            self.refresh_paths()

    def _init_db(self):
        """创建或验证数据库结构后启用 WAL；结构不兼容或打开失败时保存错误并关闭连接。"""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        register_connection(conn)
        try:
            if self._is_blank_db(conn):
                create_schema(conn)
                conn.commit()
            else:
                self.schema_error = ensure_schema(conn)
                if self.schema_error:
                    conn.close()
                    return
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._conn = conn
        except sqlite3.Error as error:
            conn.close()
            self.open_error = str(error)
            logger.error("数据库初始化失败: %s", error)

    def refresh_paths(self):
        """从当前游戏配置重建资料库与预览缓存的路径解析服务。"""
        from src.core.game_config_store import GameConfigStore

        games = GameConfigStore.instance().get_games()
        roots = {g["id"]: get_data_dir() / g["resource_dir"] / "mods" for g in games}
        previews = {g["id"]: get_data_dir() / g["resource_dir"] / "preview_cache" for g in games}
        self.paths = LibraryPaths(self._db(), roots, previews)

    @staticmethod
    def _is_blank_db(conn):
        """检查数据库是否尚未包含任何模式对象。"""
        return not conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]

    def _db(self):
        """返回已打开的数据库连接；连接不可用时抛出包含结构错误的操作异常。"""
        if self._conn is None:
            raise sqlite3.OperationalError(self.schema_error or tr("索引数据库未打开"))
        return self._conn

    @property
    def change_stamp(self):
        """组合连接身份、修改计数、外部数据版本和应用目录，生成缓存失效标记。"""
        conn = self._db()
        return (id(conn), conn.total_changes, conn.execute("PRAGMA data_version").fetchone()[0], str(get_app_dir()))

    @property
    def in_transaction(self):
        """返回底层连接当前是否处于事务中。"""
        return self._db().in_transaction

    def close(self):
        """清空轻量记录缓存并关闭数据库连接，重复关闭不执行额外操作。"""
        self._library_rows_cache = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _container(self, row, cache):
        """借助本次查询的分类路径缓存解析数据库行对应的容器目录。"""
        node_id = row["node_id"]
        if node_id:
            if node_id not in cache:
                cache[node_id] = self.paths.node_path(node_id)
            parent = cache[node_id]
        else:
            parent = self.paths.roots[row["game_id"]]
        return parent / row["folder_name"]

    def _image_reference(self, image, container, mod):
        """校验或生成容器、节点及预览缓存范围内的图片逻辑引用，拒绝范围外路径。"""
        if isinstance(image, dict):
            if image.get("scope") not in ("container", "node", "preview_cache") or not valid_relative(
                image.get("path")
            ):
                raise ValueError(tr("无效图片逻辑引用"))
            return image
        target = Path(image)
        scopes = [("container", container), ("preview_cache", Path(self.paths.preview_roots[mod.game_id]) / mod.mod_id)]
        if mod.node_id:
            scopes.append(("node", self.paths.node_path(mod.node_id)))
        for scope, base in scopes:
            try:
                relative = target.relative_to(base).as_posix()
            except ValueError:
                continue
            if valid_relative(relative):
                return {"scope": scope, "path": relative}
        raise ValueError(tr("图片不属于容器、节点或实例缓存: {image}", image=image))
