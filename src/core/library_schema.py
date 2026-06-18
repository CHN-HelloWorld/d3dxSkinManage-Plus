"""统一文件库投影与持久化部署状态。

访问索引前为连接注册 Windows 路径规范化函数；本模块不访问文件系统或初始化运行时单例。
"""

from __future__ import annotations

import ntpath
import sqlite3

SCHEMA_VERSION = 2


def directory_key(value: str) -> str:
    """按 Windows 路径规则规范化大小写和分隔符，生成数据库路径比较键。"""
    return ntpath.normcase(ntpath.normpath(value))


def valid_relative(value, single=False):
    """验证无盘符、无越界段的相对路径，可进一步要求仅包含单个目录名。"""
    if not isinstance(value, str) or not value or ntpath.splitdrive(value)[0] or value.startswith(("/", "\\")):
        return False
    parts = value.replace("\\", "/").split("/")
    return all(p not in ("", ".", "..") and ":" not in p for p in parts) and (not single or len(parts) == 1)


def register_connection(conn: sqlite3.Connection):
    """注册路径校验及比较函数，并为连接启用外键约束。"""
    conn.create_function("directory_key", 1, directory_key, deterministic=True)
    conn.create_function("valid_relative", 1, valid_relative, deterministic=True)
    conn.create_function("valid_directory", 1, lambda value: valid_relative(value, True), deterministic=True)
    conn.execute("PRAGMA foreign_keys=ON")


TABLES = {
    "nodes": """CREATE TABLE nodes (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '', name_en TEXT NOT NULL DEFAULT '',
        game_id TEXT NOT NULL,
        parent_id TEXT REFERENCES nodes(id),
        directory_name TEXT NOT NULL CHECK(valid_directory(directory_name)),
        thumbnail TEXT NOT NULL DEFAULT '', sort_order INTEGER NOT NULL DEFAULT 0,
        node_type TEXT NOT NULL DEFAULT 'category' CHECK(node_type IN ('category','character'))
    )""",
    "mods": """CREATE TABLE mods (
        mod_id TEXT PRIMARY KEY, game_id TEXT NOT NULL,
        node_id TEXT REFERENCES nodes(id), name TEXT NOT NULL DEFAULT '',
        folder_name TEXT NOT NULL CHECK(valid_directory(folder_name)),
        path TEXT NOT NULL CHECK(valid_relative(path)),
        content_hash TEXT NOT NULL DEFAULT '', info_json TEXT NOT NULL DEFAULT '{}',
        preview_images_json TEXT NOT NULL DEFAULT '[]', imported_at TEXT NOT NULL
    )""",
    "deployment_records": """CREATE TABLE deployment_records (
        mod_id TEXT PRIMARY KEY REFERENCES mods(mod_id),
        state TEXT NOT NULL CHECK(state IN ('absent','materializing','active','disabled','error')),
        actual_path TEXT NOT NULL DEFAULT ''
    )""",
    "preset_apply_journal": """CREATE TABLE preset_apply_journal (
        operation_id TEXT PRIMARY KEY, game_id TEXT NOT NULL,
        from_preset_id TEXT NOT NULL, to_preset_id TEXT NOT NULL, phase TEXT NOT NULL,
        plan_json TEXT NOT NULL, completed_steps_json TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        runtime_patch_json TEXT NOT NULL DEFAULT '{}'
    )""",
    "category_import_journal": """CREATE TABLE category_import_journal (
        operation_id TEXT PRIMARY KEY, game_id TEXT NOT NULL,
        phase TEXT NOT NULL CHECK(phase IN ('prepared','committed')),
        plan_json TEXT NOT NULL, staging_path TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    "game_deployment_state": """CREATE TABLE game_deployment_state (
        game_id TEXT PRIMARY KEY, current_preset_id TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
    )""",
    "tag_definitions": """CREATE TABLE tag_definitions (
        id TEXT PRIMARY KEY, game_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
        normalized_name TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT 'blue',
        last_used_at TEXT NOT NULL DEFAULT '', UNIQUE(game_id,normalized_name)
    )""",
    "mod_tags": """CREATE TABLE mod_tags (
        mod_id TEXT NOT NULL REFERENCES mods(mod_id) ON DELETE CASCADE,
        tag_id TEXT NOT NULL REFERENCES tag_definitions(id) ON DELETE CASCADE,
        position INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(mod_id,tag_id)
    )""",
}

INDEXES = (
    "CREATE INDEX idx_deployments_path ON deployment_records(directory_key(actual_path))",
    "CREATE INDEX idx_mods_game ON mods(game_id)",
    "CREATE INDEX idx_mods_game_content ON mods(game_id,content_hash)",
    "CREATE INDEX idx_mods_node ON mods(node_id)",
    "CREATE UNIQUE INDEX idx_mods_location ON mods(node_id,directory_key(folder_name)) WHERE node_id IS NOT NULL",
    "CREATE UNIQUE INDEX idx_mods_root ON mods(game_id,directory_key(folder_name)) WHERE node_id IS NULL",
    "CREATE INDEX idx_nodes_game ON nodes(game_id)",
    "CREATE INDEX idx_nodes_parent ON nodes(parent_id)",
    (
        "CREATE UNIQUE INDEX idx_nodes_location ON nodes(parent_id,directory_key(directory_name)) WHERE "
        "parent_id IS NOT NULL"
    ),
    "CREATE UNIQUE INDEX idx_nodes_root ON nodes(game_id,directory_key(directory_name)) WHERE parent_id IS NULL",
    "CREATE INDEX idx_mod_tags_tag ON mod_tags(tag_id)",
)


def create_schema(conn: sqlite3.Connection):
    """创建资料库表、索引和父链保护触发器，写入当前模式版本；事务提交由调用方负责。"""
    register_connection(conn)
    for sql in TABLES.values():
        conn.execute(sql)
    for sql in INDEXES:
        conn.execute(sql)
    for event in ("INSERT", "UPDATE"):
        conn.execute(f"""CREATE TRIGGER nodes_guard_{event.lower()} BEFORE {event} ON nodes BEGIN
            SELECT CASE WHEN NEW.parent_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM nodes WHERE id=NEW.parent_id AND game_id=NEW.game_id
            ) THEN RAISE(ABORT,'node parent must belong to same game') END;
            SELECT CASE WHEN NEW.id=NEW.parent_id OR EXISTS (
                WITH RECURSIVE ancestors(id,parent_id) AS (
                    SELECT id,parent_id FROM nodes WHERE id=NEW.parent_id
                    UNION SELECT n.id,n.parent_id FROM nodes n JOIN ancestors a ON n.id=a.parent_id
                ) SELECT 1 FROM ancestors WHERE id=NEW.id
            ) THEN RAISE(ABORT,'node parent cycle') END;
            SELECT CASE WHEN EXISTS (
                SELECT 1 FROM mods WHERE game_id=NEW.game_id AND node_id IS NEW.parent_id
                AND directory_key(folder_name)=directory_key(NEW.directory_name)
            ) THEN RAISE(ABORT,'node directory occupied by mod') END;
            SELECT CASE WHEN EXISTS (SELECT 1 FROM nodes WHERE parent_id=NEW.id AND game_id<>NEW.game_id)
                OR EXISTS (SELECT 1 FROM mods WHERE node_id=NEW.id AND game_id<>NEW.game_id)
            THEN RAISE(ABORT,'node game change leaves foreign descendants') END;
        END""")
        conn.execute(f"""CREATE TRIGGER mods_guard_{event.lower()} BEFORE {event} ON mods BEGIN
            SELECT CASE WHEN NEW.node_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM nodes WHERE id=NEW.node_id AND game_id=NEW.game_id
            ) THEN RAISE(ABORT,'mod node must belong to same game') END;
            SELECT CASE WHEN EXISTS (
                SELECT 1 FROM nodes WHERE game_id=NEW.game_id AND parent_id IS NEW.node_id
                AND directory_key(directory_name)=directory_key(NEW.folder_name)
            ) THEN RAISE(ABORT,'mod directory occupied by node') END;
        END""")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
