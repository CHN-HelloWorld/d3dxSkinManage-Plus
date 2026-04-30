"""对变体、INI 和引用文件执行可恢复的条件写入。

首次写入前记录修改前后内容，恢复时按实际内容判断进度。
若文件既不等于原件也不等于目标内容，则保留外部修改。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from src.core.mod_runtime_prefs import PREFS_LOCK
from src.utils import fs
from src.utils.i18n import t as tr


def read_bytes(path: Path) -> bytes | None:
    """读取配置文件原始字节，文件不存在时返回 None。"""
    try:
        return fs.read_bytes(path)
    except FileNotFoundError:
        return None


def digest(data: bytes | None) -> str:
    """计算字节内容的 SHA-256，None 用空字符串表示不存在的文件。"""
    return hashlib.sha256(data).hexdigest() if data is not None else ""


def atomic_write(path: Path, payload: bytes | None) -> None:
    """原子写入并刷新文件内容，传入 None 时删除文件；始终清理写入临时文件。"""
    if payload is None:
        fs.unlink(path, missing_ok=True)
        return
    fs.ensure_dir(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=fs.native_path(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, fs.native_path(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class FileChange:
    """保存单个文件事务前后的字节，None 表示对应阶段文件不存在。"""

    path: Path
    before: bytes | None
    after: bytes | None

    def to_dict(self) -> dict:
        """将绝对路径、前后内容编码及摘要转换为恢复日志结构。"""
        return {
            "path": str(self.path.absolute()),
            "before": _encode(self.before),
            "after": _encode(self.after),
            "before_hash": digest(self.before),
            "after_hash": digest(self.after),
        }


class VariantRecoveryError(RuntimeError):
    """表示变体事务前提、应用结果或恢复过程无法安全完成。"""

    pass


def _source_inventory_image(inventory) -> str:
    """编码产生声明的成员集合，不再次读取。"""
    folder = stat.S_ISDIR(inventory.body_version[0])
    return json.dumps(
        {
            "body": str(inventory.path),
            "body_version": None if folder else inventory.body_version,
            "folder": folder,
            "override_root": str(inventory.override_root) if inventory.override_root is not None else None,
            "seeded": inventory.seed_version is not None,
            "overrides": sorted(rel for rel, _token in inventory.overrides),
            "originals": sorted(rel for rel, _token in inventory.originals),
        },
        sort_keys=True,
    )


@dataclass(frozen=True)
class IniInventoryGuard:
    """只读的 INI 成员前提，保留到所有者提交完成。"""

    path: Path
    scope: str
    expected: str

    @classmethod
    def source(cls, inventory):
        """从有效源 INI 清单生成带完整清单镜像的事务前提。"""
        return cls(inventory.path, "source", _source_inventory_image(inventory))

    @classmethod
    def deployed(cls, path, relative_paths):
        """将部署 INI 相对路径排序并保存为文件集合前提。"""
        return cls(Path(path), "deployed", json.dumps(sorted(relative_paths)))

    def verify(self) -> None:
        """重新枚举源或部署 INI 并比较预期清单，外部变化时拒绝陈旧声明。"""
        from src.core.mod_package import _strict_ini_tree, inspect_effective_inis

        if self.scope == "source":
            current = _source_inventory_image(inspect_effective_inis(self.path))
        elif self.scope == "deployed":
            current = json.dumps(sorted(rel for rel, _token in _strict_ini_tree(self.path)))
        else:
            raise ValueError(tr("无效的 INI 清单前提类型"))
        if current != self.expected:
            raise VariantRecoveryError(tr("INI 清单已被外部修改，无法应用陈旧声明：{path}", path=self.path))

    def to_dict(self) -> dict:
        """将清单前提的绝对路径、范围和预期镜像序列化为恢复字段。"""
        return {"path": str(self.path.absolute()), "scope": self.scope, "expected": self.expected}
