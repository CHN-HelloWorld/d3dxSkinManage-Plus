"""库节点的物理目录名称独立于显示名称。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Node:
    """分类树节点，保存父子关系、磁盘目录名和界面展示计数。"""

    id: str = ""
    game_id: str = ""
    name: str = ""
    name_en: str = ""
    parent_id: str | None = None
    directory_name: str = ""
    thumbnail: str = ""
    sort_order: int = 0
    node_type: str = "category"
    mod_count: int = 0
    enabled_count: int = 0

    @property
    def display_name(self):
        """优先返回本地名称，未填写时使用英文名称。"""
        return self.name or self.name_en

    @classmethod
    def from_row(cls, row):
        """从数据库记录构造节点；统计计数由后续查询单独填充。"""
        return cls(
            **{
                key: row[key]
                for key in (
                    "id",
                    "game_id",
                    "name",
                    "name_en",
                    "parent_id",
                    "directory_name",
                    "thumbnail",
                    "sort_order",
                    "node_type",
                )
            }
        )
