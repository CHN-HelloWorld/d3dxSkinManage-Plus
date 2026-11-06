"""提供不可变的插件导入请求，隔离宿主内部文件准备结构。"""

from __future__ import annotations

import hashlib
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from src.utils.i18n import t as tr


def directory_fingerprint(source_path: str, should_cancel: Callable[[], bool] | None = None) -> str:
    """冻结普通目录的路径与文件内容，拒绝链接并在读取边界响应取消。"""
    if not isinstance(source_path, str):
        raise TypeError("source_path")
    if not source_path.strip():
        raise ValueError(tr("导入来源和目标分类不能为空"))
    root = Path(source_path).absolute()

    def check(path: Path):
        """检查取消状态和重解析点，返回本次读取的文件身份。"""
        if should_cancel is not None and should_cancel():
            raise InterruptedError(tr("操作已取消"))
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError(tr("导入来源包含链接：{path}", path=path))
        return metadata

    for ancestor in (root, *root.parents):
        check(ancestor)
    if not root.is_dir():
        raise ValueError(tr("导入来源不是目录：{path}", path=root))
    digest = hashlib.sha256()
    pending = [root]
    while pending:
        directory = pending.pop()
        check(directory)
        for path in sorted(directory.iterdir(), key=lambda value: value.name):
            before = check(path)
            relative = path.relative_to(root).as_posix().encode("utf-8")
            if stat.S_ISDIR(before.st_mode):
                digest.update(b"D\0" + relative + b"\0")
                pending.append(path)
            elif stat.S_ISREG(before.st_mode):
                content = hashlib.sha256()
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        check(path)
                        content.update(chunk)
                after = check(path)
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise ValueError(tr("Mod 来源已变化，请重新扫描"))
                digest.update(b"F\0" + relative + b"\0" + content.digest())
            else:
                raise ValueError(tr("导入来源包含不支持的文件：{path}", path=path))
    return digest.hexdigest()


@dataclass(frozen=True)
class ModImportRequest:
    """描述复制导入的来源、分类和可选元数据；None 表示保留随包字段。

    expected_sha1 是原文件字节摘要；expected_directory_hash 校验选定目录快照，二者互斥。
    这些来源校验不是 Plus 内容指纹。外部预览复制进新容器，
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
    expected_directory_hash: str = ""

    def __post_init__(self) -> None:
        """校验公开字段并冻结集合，防止请求排队后被调用者改写。"""
        for field in ("source_path", "node_id", "name", "expected_sha1", "expected_directory_hash"):
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
        if self.expected_directory_hash and re.fullmatch(r"[0-9a-fA-F]{64}", self.expected_directory_hash) is None:
            raise ValueError(tr("目录来源指纹必须为六十四位十六进制字符串"))
        if self.expected_sha1 and self.expected_directory_hash:
            raise ValueError(tr("文件摘要与目录指纹不能同时指定"))
        object.__setattr__(self, "expected_directory_hash", self.expected_directory_hash.lower())
        for field in ("tags", "preview_paths"):
            values = getattr(self, field)
            if values is None and field == "tags":
                continue
            if not isinstance(values, (list, tuple)) or not all(isinstance(value, str) for value in values):
                raise TypeError(field)
            object.__setattr__(self, field, tuple(dict.fromkeys(value.strip() for value in values if value.strip())))
