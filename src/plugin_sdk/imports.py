"""提供不可变的插件导入请求，隔离宿主内部文件准备结构。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.utils.i18n import t as tr


@dataclass(frozen=True)
class ModImportRequest:
    """描述复制导入的来源、分类和可选元数据；None 表示保留随包字段。

    expected_sha1 是原文件字节摘要，不是 Plus 内容指纹。外部预览复制进新容器，
    源文件及预览均不移动；标签和预览序列在创建请求时冻结。
    """

    source_path: str
    node_id: str
    name: str = ""
    author: str | None = None
    description: str | None = None
    rating: str | None = None
    tags: tuple[str, ...] | None = None
    preview_paths: tuple[str, ...] = ()
    source_url: str | None = None
    expected_sha1: str = ""

    def __post_init__(self) -> None:
        """校验公开字段并冻结集合，防止请求排队后被调用者改写。"""
        for field in ("source_path", "node_id", "name", "expected_sha1"):
            if not isinstance(getattr(self, field), str):
                raise TypeError(field)
        if not self.source_path.strip() or not self.node_id.strip():
            raise ValueError(tr("导入来源和目标分类不能为空"))
        for field in ("author", "description", "rating", "source_url"):
            if getattr(self, field) is not None and not isinstance(getattr(self, field), str):
                raise TypeError(field)
        if self.rating is not None and self.rating not in {"G", "P", "R", "X"}:
            raise ValueError(tr("Mod 分级必须为 G、P、R 或 X"))
        if self.expected_sha1 and re.fullmatch(r"[0-9a-fA-F]{40}", self.expected_sha1) is None:
            raise ValueError(tr("原文件 SHA-1 必须为四十位十六进制字符串"))
        object.__setattr__(self, "expected_sha1", self.expected_sha1.lower())
        for field in ("tags", "preview_paths"):
            values = getattr(self, field)
            if values is None and field == "tags":
                continue
            if not isinstance(values, (list, tuple)) or not all(isinstance(value, str) for value in values):
                raise TypeError(field)
            object.__setattr__(self, field, tuple(dict.fromkeys(value.strip() for value in values if value.strip())))
