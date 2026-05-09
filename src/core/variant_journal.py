"""预设切换与持久化变体文件操作之间的关联记录。"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class RuntimePatch:
    """记录文件操作身份和应用状态；文件快照由 VariantOperation 管理。"""

    file_operation_id: str = ""
    applied: bool = False

    @property
    def is_empty(self) -> bool:
        """判断补丁是否既无文件事务编号，也无已应用变量记录。"""
        return not self.file_operation_id and not self.applied

    def to_json(self) -> str:
        """将文件事务编号及已应用变量映射序列化为保留中文的 JSON。"""
        return json.dumps(
            {"file_operation_id": self.file_operation_id, "applied": self.applied},
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: object) -> RuntimePatch:
        """解析操作关联；无有效对象时不产生可执行的恢复引用。"""
        if not raw:
            return cls()
        try:
            data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (json.JSONDecodeError, TypeError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls(file_operation_id=str(data.get("file_operation_id") or ""), applied=bool(data.get("applied")))
