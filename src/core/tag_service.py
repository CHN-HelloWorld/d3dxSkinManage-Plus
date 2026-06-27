"""按游戏查询标签，并原子提交独立编辑草稿。"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from src.models.mod import AgeRating, Mod
from src.models.tag import MAX_TAGS, TagCommitResult, TagDefinition, TagDraft
from src.utils.i18n import t as tr

if TYPE_CHECKING:
    from src.core.mod_index import ModIndex


class TagService:
    """管理当前游戏的标签目录、编辑草稿及与 Mod 描述同步的事务提交。"""

    def __init__(self, index: ModIndex, *, begin_check=None) -> None:
        """绑定索引和可选的文件前置检查回调。"""
        self.index = index
        self.begin_check = begin_check

    def list_tags(self, *, used_only: bool = False) -> list[TagDefinition]:
        """查询当前游戏标签及引用次数，可只返回仍被使用的标签。"""
        rows = (
            self.index._db()
            .execute(
                """SELECT d.*,
            (SELECT COUNT(*) FROM mod_tags mt WHERE mt.tag_id=d.id) AS usage_count
            FROM tag_definitions d WHERE d.game_id=?
            ORDER BY d.normalized_name,d.id""",
                (self.index.game_id,),
            )
            .fetchall()
        )
        return [
            TagDefinition(
                row["id"],
                row["game_id"],
                row["name"],
                row["normalized_name"],
                row["color"],
                last_used_at=row["last_used_at"],
                usage_count=row["usage_count"],
            )
            for row in rows
            if not used_only or row["usage_count"]
        ]

    def begin_edit(self, mod: Mod) -> TagDraft:
        """核对 Mod 游戏归属后采集标签及选中关系，并按需获取文件检查凭据。"""
        conn = self.index._db()
        row = conn.execute("SELECT game_id FROM mods WHERE mod_id=?", (mod.mod_id,)).fetchone()
        if row is None or not self.index.game_id or row[0] != self.index.game_id:
            raise ValueError(tr("Mod 已不存在或所属游戏已切换，请重新打开编辑。"))
        selected = [
            row[0]
            for row in conn.execute("SELECT tag_id FROM mod_tags WHERE mod_id=? ORDER BY position", (mod.mod_id,))
        ]
        draft = TagDraft(mod, self.index.game_id, self.list_tags(), selected)
        if self.begin_check is not None:
            draft.file_receipt = self.begin_check(mod.mod_id)
        return draft

    @staticmethod
    def _field_changes(draft: TagDraft, fields: dict) -> dict:
        """比较草稿原值与提交字段，仅返回实际变化并验证字符串和年龄分级。"""
        before = {
            "name": draft.original_mod.name,
            **{key: getattr(draft.original_mod.info, key) for key in ("author", "description", "version", "url")},
            "age_rating": draft.original_mod.info.age_rating.value,
        }
        changes = {key: fields[key] for key, value in before.items() if key in fields and fields[key] != value}
        for key, value in changes.items():
            if not isinstance(value, str):
                raise ValueError(tr("Mod 信息格式无效，请检查输入。"))
            if key == "age_rating":
                AgeRating(value)
        return changes

    def commit_edit(
        self, draft: TagDraft, fields: dict | None = None, *, before_write=None, write_sidecars=None
    ) -> TagCommitResult:
        """先暂存文件元数据，再发布数据库，中间不交出控制权给回调。"""
        draft.validate()
        if not draft.game_id or self.index.game_id != draft.game_id:
            raise ValueError(tr("游戏已切换，请重新打开 Mod 编辑。"))
        conn = self.index._db()
        if conn.in_transaction:
            raise ValueError(tr("另一个写入操作尚未完成，请稍后重试。"))
        field_changes = self._field_changes(draft, fields or {})
        game_id, mod_id = draft.game_id, draft.mod_id
        deleted_ids = set(draft.deleted_ids)
        if conn.execute("SELECT 1 FROM mods WHERE mod_id=? AND game_id=?", (mod_id, game_id)).fetchone() is None:
            raise ValueError(tr("Mod 已被删除，请关闭编辑窗口。"))
        live = {tag.id: tag for tag in self.list_tags()}
        projected = copy.deepcopy(live)
        changes: dict[str, dict] = {}
        for tag_id, edited in draft.definitions.items():
            if tag_id in deleted_ids:
                continue
            original = draft.original_definitions.get(tag_id)
            if original is None:
                projected[tag_id] = copy.deepcopy(edited)
                continue
            delta = {
                name: getattr(edited, name)
                for name in ("name", "normalized_name", "color")
                if getattr(original, name) != getattr(edited, name)
            }
            if not delta:
                continue
            if tag_id not in live:
                raise ValueError(tr("正在编辑的标签已被其他窗口删除，请重新打开编辑。"))
            for name, value in delta.items():
                if getattr(live[tag_id], name) not in (getattr(original, name), value):
                    raise ValueError(tr("这个标签已被其他窗口修改，请重新打开编辑后再保存。"))
                setattr(projected[tag_id], name, value)
            changes[tag_id] = delta
        for tag_id in deleted_ids:
            projected.pop(tag_id, None)
        keys: set[str] = set()
        for tag in projected.values():
            if tag.game_id != game_id or tag.normalized_name in keys:
                raise ValueError(tr("已有同名标签，保存未生效，请修改名称后重试。"))
            keys.add(tag.normalized_name)

        def catalog_state(tags):
            """提取标签身份、游戏、名称和颜色，生成可比较的目录状态映射。"""
            return {tag.id: (tag.game_id, tag.name, tag.normalized_name, tag.color) for tag in tags}

        def read_references():
            """查询本次修改或删除标签的全部 Mod 引用集合。"""
            return {
                tag_id: {row[0] for row in conn.execute("SELECT mod_id FROM mod_tags WHERE tag_id=?", (tag_id,))}
                for tag_id in set(changes) | deleted_ids
            }

        catalog = catalog_state(live.values())
        references = read_references()
        affected, metadata_affected = {mod_id}, {mod_id}
        for tag_id, ids in references.items():
            affected.update(ids)
            if tag_id in deleted_ids or "name" in changes.get(tag_id, {}):
                metadata_affected.update(ids)
        rows, relations = self._read_edit_rows(conn, game_id, metadata_affected)
        old_current = [tag_id for tag_id, _position in relations[mod_id]]
        removed = set(draft.original_selected_ids) - set(draft.selected_ids)
        added = set(draft.selected_ids) - set(draft.original_selected_ids)
        if not added.issubset(projected):
            raise ValueError(tr("要添加的标签已被删除，请重新选择。"))
        final_ids = [tag_id for tag_id in old_current if tag_id not in removed and tag_id in projected]
        if draft.selected_ids != draft.original_selected_ids:
            final_ids = list(
                dict.fromkeys(
                    [tag_id for tag_id in draft.selected_ids if tag_id in final_ids or tag_id in added] + final_ids
                )
            )
        if added and len(final_ids) > MAX_TAGS and not set(final_ids).issubset(draft.original_selected_ids):
            raise ValueError(tr("其他编辑已增加标签，当前选择超过 20 个，请调整后重试。"))

        updates, names_by_mod = {}, {}
        for mid, row in rows.items():
            info = json.loads(row["info_json"])
            if not isinstance(info, dict):
                raise ValueError(tr("Mod 元数据格式无效，保存未生效。"))
            ids = final_ids if mid == mod_id else [tag_id for tag_id, _position in relations[mid]]
            names = [projected[tag_id].name for tag_id in ids if tag_id in projected]
            names_by_mod[mid] = names
            changed_info = info.get("tags", []) != names
            info["tags"] = names
            name = row["name"]
            if mid == mod_id:
                for key, value in field_changes.items():
                    if key == "name":
                        name = value
                    else:
                        changed_info |= info.get(key) != value
                        info[key] = value
            if changed_info or name != row["name"]:
                updates[mid] = (name, json.dumps(info, ensure_ascii=False), mid)
        result = TagCommitResult(sidecar_mod_ids=set(updates), deleted_tag_ids=deleted_ids, affected_mod_ids=affected)

        def validate_snapshot():
            """复核游戏连接、标签目录、引用与受影响描述行，外部变化时拒绝提交。"""
            if self.index.game_id != game_id or self.index._db() is not conn:
                raise ValueError(tr("游戏已切换，请重新打开 Mod 编辑。"))
            if catalog_state(self.list_tags()) != catalog or read_references() != references:
                raise ValueError(tr("保存期间标签或引用已变化，请重新打开编辑。"))
            if self._read_edit_rows(conn, game_id, metadata_affected) != (rows, relations):
                raise ValueError(tr("保存期间 Mod 信息已变化，请重新打开编辑。"))

        # 后台检查器可能执行无关完成回调，进入和退出时共享连接均不得残留本次未提交修改。
        if before_write is not None:
            before_write(set(metadata_affected))
            validate_snapshot()
        if conn.in_transaction:
            raise ValueError(tr("另一个写入操作尚未完成，请稍后重试。"))
        if write_sidecars is not None and updates:
            result.mods = sorted(self.index.get_mods_by_ids(updates), key=lambda mod: mod.mod_id)
            for mod in result.mods:
                if mod.mod_id in names_by_mod:
                    mod.info.tags = names_by_mod[mod.mod_id]
                if mod.mod_id == mod_id:
                    for key, value in field_changes.items():
                        if key == "name":
                            mod.name = value
                        else:
                            setattr(mod.info, key, AgeRating(value) if key == "age_rating" else value)
            write_sidecars(result)

        owns_transaction = False
        try:
            with self.index.transaction():
                owns_transaction = True
                validate_snapshot()
                now = datetime.now().isoformat()
                for tag_id in deleted_ids:
                    conn.execute("DELETE FROM tag_definitions WHERE id=? AND game_id=?", (tag_id, game_id))
                # 先释放旧唯一键，允许名称互换。
                for tag_id, delta in changes.items():
                    if "normalized_name" in delta:
                        conn.execute(
                            "UPDATE tag_definitions SET normalized_name=? WHERE id=?",
                            ("\x1f" + uuid.uuid4().hex, tag_id),
                        )
                for tag_id, tag in projected.items():
                    if tag_id not in live:
                        conn.execute(
                            """INSERT INTO tag_definitions
                            (id,game_id,name,normalized_name,color,last_used_at) VALUES (?,?,?,?,?,?)""",
                            (tag.id, game_id, tag.name, tag.normalized_name, tag.color, ""),
                        )
                    elif tag_id in changes:
                        delta = changes[tag_id]
                        conn.execute(
                            "UPDATE tag_definitions SET " + ",".join(f"{key}=?" for key in delta) + " WHERE id=?",
                            (*delta.values(), tag_id),
                        )
                if final_ids != old_current:
                    conn.execute("DELETE FROM mod_tags WHERE mod_id=?", (mod_id,))
                    conn.executemany(
                        "INSERT INTO mod_tags (mod_id,tag_id,position) VALUES (?,?,?)",
                        [(mod_id, tag_id, i) for i, tag_id in enumerate(final_ids)],
                    )
                for tag_id in set(final_ids) - set(old_current):
                    conn.execute(
                        "UPDATE tag_definitions SET last_used_at=MAX(last_used_at,?) WHERE id=?", (now, tag_id)
                    )
                conn.executemany("UPDATE mods SET name=?,info_json=? WHERE mod_id=?", updates.values())
                result.mods = sorted(self.index.get_mods_by_ids(set(updates) | {mod_id}), key=lambda mod: mod.mod_id)
        except Exception:
            # 延迟约束可能在提交时失败，不能回滚其他回调新开的事务。
            if owns_transaction and conn.in_transaction:
                conn.rollback()
            raise
        return result

    @staticmethod
    def _read_edit_rows(conn, game_id, mod_ids):
        """分批采集本次编辑使用的记录和关系。"""
        ids = sorted(mod_ids)
        rows, relations = {}, {mid: [] for mid in ids}
        for offset in range(0, len(ids), 500):
            chunk = ids[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"SELECT * FROM mods WHERE game_id=? AND mod_id IN ({placeholders})", (game_id, *chunk)
            ):
                rows[row["mod_id"]] = dict(row)
            for mid, tag_id, position in conn.execute(
                f"SELECT mod_id,tag_id,position FROM mod_tags WHERE mod_id IN ({placeholders})"
                " ORDER BY mod_id,position",
                chunk,
            ):
                relations[mid].append((tag_id, position))
        return rows, relations
