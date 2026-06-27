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
