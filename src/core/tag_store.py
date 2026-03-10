"""导入、重建和元数据编辑共用的标签关系维护。"""

from __future__ import annotations

import sqlite3
import uuid

from src.models.tag import default_tag_color, normalize_tag_name, tag_key
from src.utils.i18n import t as tr


def sync_mod_tags(conn: sqlite3.Connection, mod_id: str, game_id: str, names: list[str], timestamp: str) -> list[str]:
    """在调用方事务中更新 Mod 关系，保留用户偏好。"""
    names = [] if names is None else names
    if not isinstance(names, list):
        raise ValueError(tr("Mod 标签必须是列表"))
    previous = {row[0] for row in conn.execute("SELECT tag_id FROM mod_tags WHERE mod_id=?", (mod_id,))}
    canonical_names: list[str] = []
    ids: list[str] = []
    seen: set[str] = set()
    for raw in names:
        if not isinstance(raw, str):
            continue
        key = tag_key(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        row = conn.execute(
            "SELECT id,name FROM tag_definitions WHERE game_id=? AND normalized_name=?", (game_id, key)
        ).fetchone()
        if row is None:
            name = normalize_tag_name(raw)
            tag_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO tag_definitions
                (id,game_id,name,normalized_name,color,last_used_at) VALUES (?,?,?,?,?,'')""",
                (tag_id, game_id, name, key, default_tag_color(name)),
            )
        else:
            tag_id, name = row[0], row[1]
        ids.append(tag_id)
        canonical_names.append(name)
    conn.execute("DELETE FROM mod_tags WHERE mod_id=?", (mod_id,))
    conn.executemany(
        "INSERT INTO mod_tags (mod_id,tag_id,position) VALUES (?,?,?)",
        [(mod_id, tag_id, position) for position, tag_id in enumerate(ids)],
    )
    for tag_id in set(ids) - previous:
        conn.execute("UPDATE tag_definitions SET last_used_at=MAX(last_used_at,?) WHERE id=?", (timestamp, tag_id))
    return canonical_names
