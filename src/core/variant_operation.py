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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from src.core.mod_runtime_prefs import PREFS_LOCK
from src.utils import fs
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import resolve_stored_path, to_stored_path


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
        """将可移植路径、前后内容编码及摘要转换为恢复日志结构。"""
        return {
            "path": to_stored_path(self.path.absolute()),
            "before": _encode(self.before),
            "after": _encode(self.after),
            "before_hash": digest(self.before),
            "after_hash": digest(self.after),
        }


class VariantRecoveryError(RuntimeError):
    """表示变体事务前提、应用结果或恢复过程无法安全完成。"""

    pass


class IniInventoryChanged(VariantRecoveryError):
    """INI 成员发生变化，调用方只能在确认原状态完整保留后重建计划。"""


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


def _convert_source_inventory_paths(expected: str, convert: Callable[[str], str]) -> str:
    """转换源清单镜像中的路径，保留原有成员与文件版本供严格比较。"""
    image = json.loads(expected)
    if not isinstance(image, dict):
        raise ValueError(tr("无效的 INI 清单前提"))
    for key in ("body", "override_root"):
        value = image.get(key)
        if value is not None:
            if not isinstance(value, str) or not value:
                raise ValueError(tr("无效的 INI 清单前提"))
            image[key] = convert(value)
    return json.dumps(image, sort_keys=True)


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
    def deployed(cls, path, relative_paths, *, rules=None):
        """保存部署成员及可选加载规则，使变量分析与提交前的集合比较一致。"""
        paths = sorted(relative_paths)
        expected = {"paths": paths, "rules": rules.to_dict()} if rules is not None else paths
        return cls(Path(path), "deployed", json.dumps(expected))

    def verify(self) -> None:
        """重新枚举源或部署 INI 并比较预期清单，外部变化时拒绝陈旧声明。"""
        from src.core.mod_package import _strict_ini_tree, inspect_effective_inis

        if self.scope == "source":
            current = _source_inventory_image(inspect_effective_inis(self.path))
        elif self.scope == "deployed":
            from src.core.ini_loading import IniLoadRules

            expected = json.loads(self.expected)
            paths = [rel for rel, _token in _strict_ini_tree(self.path, exclude_meta=True)]
            if isinstance(expected, dict):
                rules = IniLoadRules.from_dict(expected["rules"])
                current = json.dumps(
                    {"paths": sorted(rel for rel in paths if rules.accepts(rel)), "rules": rules.to_dict()}
                )
            else:
                current = json.dumps(sorted(paths))
        else:
            raise ValueError(tr("无效的 INI 清单前提类型"))
        if current != self.expected:
            logger.warning(
                "INI 清单校验不一致: scope=%s, path=%s, expected=%s, actual=%s",
                self.scope,
                self.path,
                self.expected,
                current,
            )
            raise IniInventoryChanged(tr("INI 清单已被外部修改，无法应用陈旧声明：{path}", path=self.path))

    def to_dict(self) -> dict:
        """持久化可移植路径及清单镜像，不改写归档身份或版本前提。"""
        expected = self.expected
        if self.scope == "source":
            expected = _convert_source_inventory_paths(expected, to_stored_path)
        return {"path": to_stored_path(self.path.absolute()), "scope": self.scope, "expected": expected}


class VariantOperation:
    """以持久恢复日志组织多个配置文件的比较写入、提交和保留外部修改的回滚。"""

    def __init__(
        self,
        directory: Path,
        game_id: str,
        mod_ids: Iterable[str],
        changes: Iterable[FileChange | IniInventoryGuard],
        *,
        operation_id: str = "",
        metadata: dict | None = None,
    ) -> None:
        """验证操作编号和文件唯一性，拆分 INI 清单前提与实际文件变化并初始化事务。"""
        self.operation_id = operation_id or uuid.uuid4().hex
        if not self.operation_id or any(c not in "0123456789abcdef-_" for c in self.operation_id):
            raise ValueError(tr("无效的变体操作 ID"))
        self.journal_path = Path(directory) / (self.operation_id + ".json")
        self.game_id = game_id
        self.mod_ids = tuple(dict.fromkeys(mod_ids))
        # 相同内容也构成读取约束，重复应用仍须发现等待日志期间的外部修改。
        inputs = tuple(changes)
        self.prerequisites = tuple(item for item in inputs if isinstance(item, IniInventoryGuard))
        self.changes = tuple(item for item in inputs if not isinstance(item, IniInventoryGuard))
        paths = [os.path.normcase(os.path.abspath(str(c.path))) for c in self.changes]
        if len(set(paths)) != len(paths):
            raise ValueError(tr("变体事务中同一文件出现多次"))
        self.metadata = dict(metadata or {})
        self.phase = "new"

    def _persist(self) -> None:
        """将当前事务阶段、变化、前提及元数据原子写入恢复日志。"""
        payload = json.dumps(
            {
                "kind": "d3dx-variant-operation",
                "operation_id": self.operation_id,
                "game_id": self.game_id,
                "mod_ids": list(self.mod_ids),
                "phase": self.phase,
                "metadata": self.metadata,
                "changes": [change.to_dict() for change in self.changes],
                "prerequisites": [guard.to_dict() for guard in self.prerequisites],
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        atomic_write(self.journal_path, payload)

    def prepare(self) -> None:
        """在偏好锁内核对清单和所有文件原值，成功后持久化准备阶段日志。"""
        with PREFS_LOCK:
            for guard in self.prerequisites:
                guard.verify()
            for change in self.changes:
                if read_bytes(change.path) != change.before:
                    raise VariantRecoveryError(tr("文件已被外部修改，未开始应用：{path}", path=change.path))
            self.phase = "prepared"
            self._persist()

    def apply(self) -> None:
        """复核准备前提并逐文件比较写入和回读，成功后持久化已应用阶段。

        任一步失败会尝试回滚；存在无法恢复的外部修改时抛出带恢复说明的异常。"""
        with PREFS_LOCK:
            if self.phase == "new":
                self.prepare()
            if self.phase != "prepared":
                raise VariantRecoveryError(tr("变体操作不处于可应用阶段"))
            try:
                for guard in self.prerequisites:
                    guard.verify()
                for change in self.changes:
                    if read_bytes(change.path) != change.before:
                        raise VariantRecoveryError(tr("文件已被外部修改：{path}", path=change.path))
                    if change.before != change.after:
                        atomic_write(change.path, change.after)
                    if read_bytes(change.path) != change.after:
                        raise VariantRecoveryError(tr("文件写入回读不一致：{path}", path=change.path))
                self.verify_applied()
                self.phase = "applied"
                self._persist()
            except Exception as error:
                problems = self.rollback()
                if problems:
                    raise VariantRecoveryError(
                        tr("配置应用失败：{error}；存在待恢复内容：{value}", error=error, value="；".join(problems))
                    ) from error
                raise

    def verify_applied(self) -> None:
        """发布应用状态前重新检查每个目标。"""
        with PREFS_LOCK:
            for change in self.changes:
                if read_bytes(change.path) != change.after:
                    raise VariantRecoveryError(tr("文件已被外部修改，配置校验失败：{path}", path=change.path))
            for guard in self.prerequisites:
                guard.verify()

    def commit(self, *, verify: bool = True) -> None:
        """读回验证或数据库持久提交后完成事务。

        数据库提交前由所有者验证；之后加载器写入的内容属于下次采集，不撤销本次提交。
        """
        with PREFS_LOCK:
            if self.phase not in ("applied", "committed"):
                raise VariantRecoveryError(tr("配置文件尚未全部写入，不能提交"))
            if verify and self.phase == "applied":
                self.verify_applied()
            previous_phase = self.phase
            self.phase = "committed"
            try:
                self._persist()
            except Exception:
                self.phase = previous_phase
                raise
            # 已提交的残留日志可在下次启动时清理。
            try:
                fs.unlink(self.journal_path, missing_ok=True)
            except OSError:
                pass

    def rollback(self) -> tuple[str, ...]:
        """逆序恢复仍等于本次写入值的文件，保留外部修改并记录待恢复阶段。

        全部恢复后删除日志；已提交或已回滚的操作直接返回空问题列表。"""
        with PREFS_LOCK:
            if self.phase in {"committed", "rolled_back"}:
                return ()
            problems = []
            for change in reversed(self.changes):
                # 只读前提没有被本事务修改，外部新内容应保留，仅撤销实际写入。
                if change.before == change.after:
                    continue
                try:
                    current = read_bytes(change.path)
                    if current == change.before:
                        continue
                    if current != change.after:
                        problems.append(tr("文件有外部修改，已保留：{path}", path=change.path))
                        continue
                    atomic_write(change.path, change.before)
                    if read_bytes(change.path) != change.before:
                        problems.append(tr("恢复回读不一致：{path}", path=change.path))
                except OSError as error:
                    problems.append(f"{change.path}：{error}")
            if problems:
                self.phase = "recovery_required"
                self._persist()
            else:
                self.phase = "rolled_back"
                fs.unlink(self.journal_path, missing_ok=True)
            return tuple(problems)

    @classmethod
    def load(cls, path: Path, *, allowed_roots: Iterable[Path]) -> VariantOperation:
        """读取并验证恢复日志的类型、路径范围、内容摘要、前提及阶段，重建事务对象。"""
        data = json.loads(fs.read_bytes(path).decode("utf-8-sig"))
        if not isinstance(data, dict) or data.get("kind") != "d3dx-variant-operation":
            raise ValueError(tr("变体恢复记录类型无效"))
        roots = [Path(root).resolve() for root in allowed_roots]

        def restore_path(value: str) -> Path:
            """以当前应用目录解析新相对路径，兼容旧绝对路径并保持授权边界。"""
            target = Path(resolve_stored_path(value))
            resolved = target.resolve()
            if not target.is_absolute() or not any(resolved == r or r in resolved.parents for r in roots):
                raise ValueError(tr("恢复路径越界：{target}", target=target))
            return target

        changes = []
        for raw in data.get("changes", []):
            target = restore_path(raw["path"])
            before, after = _decode(raw.get("before")), _decode(raw.get("after"))
            if digest(before) != raw.get("before_hash") or digest(after) != raw.get("after_hash"):
                raise ValueError(tr("恢复文件哈希不一致：{target}", target=target))
            changes.append(FileChange(target, before, after))
        for raw in data.get("prerequisites", []):
            target = restore_path(raw["path"])
            scope, expected = raw["scope"], raw["expected"]
            if scope not in {"source", "deployed"} or not isinstance(expected, str):
                raise ValueError(tr("无效的 INI 清单前提"))
            json.loads(expected)
            if scope == "source":
                expected = _convert_source_inventory_paths(expected, resolve_stored_path)
            changes.append(IniInventoryGuard(target, scope, expected))
        op = cls(
            Path(path).parent,
            data["game_id"],
            data["mod_ids"],
            changes,
            operation_id=data["operation_id"],
            metadata=data.get("metadata"),
        )
        if op.journal_path.name != Path(path).name:
            raise ValueError(tr("恢复记录身份与文件名不符"))
        op.phase = data.get("phase", "")
        if op.phase not in {"prepared", "applied", "committed", "recovery_required"}:
            raise ValueError(tr("变体恢复阶段无效"))
        return op


def _encode(data: bytes | None) -> str | None:
    """将存在的字节编码为 Base64 文本，保留 None 表示文件缺失。"""
    return base64.b64encode(data).decode("ascii") if data is not None else None


def _decode(value) -> bytes | None:
    """严格解码 Base64 恢复内容，接受 None 并拒绝其他数据类型或无效编码。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(tr("恢复内容必须是 base64 字符串或 null"))
    return base64.b64decode(value, validate=True)
