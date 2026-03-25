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

    def add_mod(self, mod, *, commit=True):
        """校验 Mod 身份及容器归属，以保存点写入记录、图片引用和标签关系。

        默认提交事务；校验或数据库错误返回 False，自有事务会回滚，外层事务中的局部写入由保存点撤销。"""
        conn = self._db()
        owns_transaction = not conn.in_transaction
        try:
            if not mod.mod_id or not mod.game_id or not mod.imported_at:
                raise ValueError(tr("Mod 身份、游戏和导入时间不能为空"))
            if self.game_id and mod.game_id != self.game_id:
                raise ValueError(tr("Mod 不属于当前游戏"))
            parent = self.paths.node_path(mod.node_id) if mod.node_id else self.paths.roots[mod.game_id]
            container = parent / mod.folder_name
            body = Path(mod.path).relative_to(container).as_posix()
            if not valid_relative(mod.folder_name, True) or not valid_relative(body):
                raise ValueError(tr("本体必须在容器内"))
            info = dict(mod.to_dict()["info"])
            info.pop("name", None)
            previews = [self._image_reference(p, container, mod) for p in mod.preview_images]
            if owns_transaction:
                conn.execute("BEGIN")
            conn.execute("SAVEPOINT write_mod")
            try:
                conn.execute(
                    """INSERT INTO mods(mod_id,game_id,node_id,name,folder_name,path,
                    content_hash,info_json,preview_images_json,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(mod_id) DO UPDATE SET game_id=excluded.game_id,node_id=excluded.node_id,
                    name=excluded.name,folder_name=excluded.folder_name,path=excluded.path,
                    content_hash=excluded.content_hash,info_json=excluded.info_json,
                    preview_images_json=excluded.preview_images_json,imported_at=excluded.imported_at""",
                    (
                        mod.mod_id,
                        mod.game_id,
                        mod.node_id or None,
                        mod.name,
                        mod.folder_name,
                        body,
                        mod.content_hash,
                        json.dumps(info, ensure_ascii=False),
                        json.dumps(previews, ensure_ascii=False),
                        mod.imported_at,
                    ),
                )
                tags = sync_mod_tags(conn, mod.mod_id, mod.game_id, info.get("tags", []), datetime.now().isoformat())
                if tags != info.get("tags", []):
                    info["tags"] = tags
                    conn.execute(
                        "UPDATE mods SET info_json=? WHERE mod_id=?", (json.dumps(info, ensure_ascii=False), mod.mod_id)
                    )
                conn.execute("RELEASE SAVEPOINT write_mod")
            except Exception:
                conn.execute("ROLLBACK TO SAVEPOINT write_mod")
                conn.execute("RELEASE SAVEPOINT write_mod")
                raise
            if commit:
                conn.commit()
            return True
        except (sqlite3.Error, ValueError, TypeError, KeyError) as error:
            if owns_transaction:
                conn.rollback()
            logger.error("添加 Mod 失败: %s", error)
            return False

    def update_mod(self, mod, *, commit=True):
        """通过统一写入流程更新 Mod 记录，并按参数决定是否提交事务。"""
        return self.add_mod(mod, commit=commit)

    def _row_to_mod(self, row, cache=None):
        """将索引行和部署状态转换为 Mod 对象，解析受范围约束的图片及正文路径。"""
        container = self._container(row, {} if cache is None else cache)
        images = []
        for ref in json.loads(row["preview_images_json"]):
            if not valid_relative(ref["path"]):
                raise ValueError(tr("图片引用越界"))
            roots = {
                "container": container,
                "preview_cache": Path(self.paths.preview_roots[row["game_id"]]) / row["mod_id"],
            }
            if row["node_id"]:
                roots["node"] = container.parent
            images.append(str(roots[ref["scope"]] / ref["path"]))
        return Mod.from_dict(
            {
                "mod_id": row["mod_id"],
                "game_id": row["game_id"],
                "node_id": row["node_id"],
                "name": row["name"],
                "folder_name": row["folder_name"],
                "path": str(container / row["path"]),
                "content_hash": row["content_hash"],
                "info": json.loads(row["info_json"]),
                "preview_images": images,
                "imported_at": row["imported_at"],
                "status": "enabled" if row["deployment_state"] == "active" else "disabled",
                "deployed_path": row["actual_path"] or "",
            }
        )

    def _mods(self, where="", params=(), *, order=True):
        """执行带条件的 Mod 联合查询，复用分类路径缓存构造结果对象列表。"""
        rows = self._db().execute(_SELECT + where + (" ORDER BY m.name" if order else ""), params).fetchall()
        cache = {}
        return [self._row_to_mod(row, cache) for row in rows]

    def get_mod(self, mod_id):
        """按持久编号返回单个 Mod，记录不存在时返回 None。"""
        rows = self._mods("WHERE m.mod_id=?", (mod_id,), order=False)
        return rows[0] if rows else None

    def get_mod_game_id(self, mod_id):
        """直接查询 Mod 所属游戏编号，记录不存在时返回 None。"""
        row = self._db().execute("SELECT game_id FROM mods WHERE mod_id=?", (mod_id,)).fetchone()
        return row[0] if row else None

    def get_mod_ids(self, game_id=""):
        """返回指定游戏或全部游戏的 Mod 身份集合。"""
        sql = "SELECT mod_id FROM mods" + (" WHERE game_id=?" if game_id else "")
        return {r[0] for r in self._db().execute(sql, (game_id,) if game_id else ())}

    def iter_mod_info(self, game_id, *, ordered=True):
        """读取受管元数据，不解析归档、预览或部署路径。"""
        cursor = self._db().execute(
            "SELECT mod_id,info_json FROM mods WHERE game_id=?" + (" ORDER BY name" if ordered else ""), (game_id,)
        )
        try:
            for row in cursor:
                yield row["mod_id"], json.loads(row["info_json"])
        finally:
            cursor.close()

    def get_mods_by_ids(self, mod_ids):
        """对输入编号去重并分批查询 Mod 对象，以限制每次 SQL 参数数量。"""
        ids = tuple(dict.fromkeys(mod_ids))
        result = []
        for offset in range(0, len(ids), 500):
            chunk = ids[offset : offset + 500]
            result.extend(self._mods("WHERE m.mod_id IN (" + ",".join("?" for _ in chunk) + ")", chunk))
        return result

    def find_mod_id_by_path(self, path, game_id):
        """解析路径所属 Mod，并仅在其属于指定游戏时返回身份。"""
        owner = self.paths.locate(str(path))
        return owner[1] if owner and owner[0] == "mod" and self.get_mod_game_id(owner[1]) == game_id else None

    def get_all_mods(self, game_id=None):
        """获取指定或当前游戏的全部 Mod，无有效游戏范围时查询全库。"""
        gid = game_id or self.game_id
        return self._mods("WHERE m.game_id=?", (gid,)) if gid else self._mods()

    def iter_mods(self, game_id, character_ids=None):
        """按名称逐条生成指定游戏和可选分类范围的 Mod，迭代结束时关闭游标。"""
        where = "WHERE m.game_id=?"
        params = (game_id,)
        if character_ids is not None:
            ids = tuple(dict.fromkeys(character_ids))
            if not ids:
                return
            where += " AND m.node_id IN (" + ",".join("?" for _ in ids) + ")"
            params += ids
        # 仅解码请求的记录，使界面可按时间预算让出执行。
        cursor = self._db().execute(_SELECT + where + " ORDER BY m.name", params)
        cache = {}
        try:
            for row in cursor:
                yield self._row_to_mod(row, cache)
        finally:
            cursor.close()
