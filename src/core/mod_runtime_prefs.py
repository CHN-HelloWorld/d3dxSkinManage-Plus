"""按 Mod 保存本地工作状态，命名变体保存在各自的资源容器中。"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from src.core.migoto_user_ini import normalize_var_key
from src.utils import fs
from src.utils.i18n import t as tr
from src.utils.logger import logger

PREFS_FILENAME = "mod_preferences.json"
SCHEMA = 2
# 所有管理器和独立工作线程在读改写期间共用此锁。
PREFS_LOCK = threading.RLock()


@dataclass
class RuntimeFileInfo:
    """保存运行时配置文件位置及最近观察到的内容摘要。"""

    path: str = ""
    last_seen_hash: str = ""


@dataclass
class ModWorkingState:
    """记录单个 Mod 的工作变量、选择变体、声明版本及采集来源证据。"""

    values: dict[str, str] = field(default_factory=dict)
    selected_variant_id: str | None = None
    declaration_fingerprint: str = ""
    deployment_generation: str = ""
    capture_generation: int = 0
    confirmed_keys: tuple[str, ...] = ()
    captured_at: str = ""
    sources: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """将工作状态转换为独立可序列化字典，并复制变量及来源数据。"""
        return {
            "values": dict(self.values),
            "selected_variant_id": self.selected_variant_id,
            "declaration_fingerprint": self.declaration_fingerprint,
            "deployment_generation": self.deployment_generation,
            "capture_generation": self.capture_generation,
            "confirmed_keys": list(self.confirmed_keys),
            "captured_at": self.captured_at,
            "sources": copy.deepcopy(self.sources),
        }

    @classmethod
    def from_dict(cls, data: object) -> ModWorkingState:
        """验证工作状态字段和采集代次，规范变量键后构造状态；无效结构抛出 ValueError。"""
        if not isinstance(data, dict):
            raise ValueError(tr("Mod 工作状态必须是对象"))
        values = data.get("values", {})
        if not isinstance(values, dict) or any(
            not isinstance(k, str) or not k.strip() or not isinstance(v, str) for k, v in values.items()
        ):
            raise ValueError(tr("Mod 工作状态变量必须是字符串映射"))
        selected = data.get("selected_variant_id")
        if selected is not None and (not isinstance(selected, str) or not selected.strip()):
            raise ValueError(tr("私有变体选择必须是非空 ID 或 null"))
        confirmed = data.get("confirmed_keys", [])
        if not isinstance(confirmed, list) or any(not isinstance(k, str) for k in confirmed):
            raise ValueError(tr("已确认变量必须是键列表"))
        sources = data.get("sources", {})
        if not isinstance(sources, dict) or any(
            not isinstance(k, str) or not isinstance(v, dict) for k, v in sources.items()
        ):
            raise ValueError(tr("变量采集来源必须是对象映射"))
        sequence = data.get("capture_generation", 0)
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError(tr("采集代次必须是非负整数"))
        strings = {}
        for key in ("declaration_fingerprint", "deployment_generation", "captured_at"):
            value = data.get(key, "")
            if not isinstance(value, str):
                raise ValueError(tr("工作状态 {key} 必须是字符串", key=key))
            strings[key] = value
        return cls(
            values={normalize_var_key(k): v for k, v in values.items()},
            selected_variant_id=selected,
            capture_generation=sequence,
            confirmed_keys=tuple(normalize_var_key(k) for k in confirmed),
            sources={normalize_var_key(k): copy.deepcopy(v) for k, v in sources.items()},
            **strings,
        )


@dataclass
class RuntimePrefsStore:
    """保存游戏运行偏好集合及读取时的原始内容，支持比较后写入。"""

    runtime_file: RuntimeFileInfo = field(default_factory=RuntimeFileInfo)
    mods: dict[str, ModWorkingState] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)
    _source_path: Path | None = field(default=None, init=False, repr=False, compare=False)
    _source_bytes: bytes | None = field(default=None, init=False, repr=False, compare=False)

    def _remember_source(self, path: str | Path, payload: bytes | None) -> None:
        """记住读取来源的绝对路径和原始字节，用于后续外部修改检查。"""
        self._source_path = Path(fs.normal_path(path)).absolute()
        self._source_bytes = payload

    def before_image(self, path: str | Path) -> bytes | None:
        """返回构造此存储时读取的原始内容，用于事务校验。"""
        if self._source_path != Path(fs.normal_path(path)).absolute():
            raise ValueError(tr("工作状态不是从目标文件读取，不能覆盖原文件"))
        return self._source_bytes

    def state_for(self, mod_id: str) -> ModWorkingState:
        """取得或新建指定 Mod 的工作状态，拒绝空身份。"""
        if not isinstance(mod_id, str) or not mod_id.strip():
            raise ValueError(tr("mod_id 不能为空"))
        return self.mods.setdefault(mod_id, ModWorkingState())

    def snapshot_for(self, mod_id: str) -> dict[str, str]:
        """返回指定 Mod 变量值的独立副本，未记录时返回空字典。"""
        state = self.mods.get(mod_id)
        return dict(state.values) if state is not None else {}

    def update_recent(self, mod_id: str, values: dict[str, str]) -> bool:
        """保留已有值，不将其标记为新的采集结果。"""
        if not mod_id or not values:
            return False
        state = self.state_for(mod_id)
        normalized = {normalize_var_key(k): str(v) for k, v in values.items()}
        changed = any(state.values.get(k) != v for k, v in normalized.items())
        state.values.update(normalized)
        return changed

    def drop_deployment(self, mod_id: str) -> bool:
        """仅删除源 Mod 时清除工作状态，部署缺失不会清除。"""
        return self.mods.pop(mod_id, None) is not None

    def to_dict(self) -> dict:
        """合并保留的未知字段、运行文件信息和排序后的 Mod 状态，生成存储数据。"""
        data = copy.deepcopy(self.extra)
        data.update(
            {
                "schema": SCHEMA,
                "runtime_file": {
                    "path": self.runtime_file.path,
                    "last_seen_hash": self.runtime_file.last_seen_hash,
                },
                "mods": {mid: state.to_dict() for mid, state in sorted(self.mods.items())},
            }
        )
        return data

    @classmethod
    def from_dict(cls, data: object) -> RuntimePrefsStore:
        """验证偏好顶层及成员结构，保留未知字段并逐项解析工作状态。"""
        if not isinstance(data, dict):
            raise ValueError(tr("mod_preferences.json 顶层必须是 JSON 对象"))
        known = {"schema", "runtime_file", "mods"}
        store = cls(extra={k: copy.deepcopy(v) for k, v in data.items() if k not in known})
        runtime = data.get("runtime_file", {})
        if not isinstance(runtime, dict):
            raise ValueError(tr("runtime_file 必须是对象"))
        store.runtime_file = RuntimeFileInfo(
            path=str(runtime.get("path") or ""), last_seen_hash=str(runtime.get("last_seen_hash") or "")
        )
        mods = data.get("mods", {})
        if not isinstance(mods, dict):
            raise ValueError(tr("mods 必须是按 Mod ID 索引的对象"))
        for mod_id, raw in mods.items():
            if not isinstance(mod_id, str) or not mod_id.strip():
                raise ValueError(tr("工作状态缺少 Mod ID"))
            store.mods[mod_id] = ModWorkingState.from_dict(raw)
        return store


def prefs_path(resource_dir: str | Path | None, data_dir: str | Path) -> Path | None:
    """按数据根和资源目录生成偏好文件位置，资源目录为空时返回 None。"""
    name = str(resource_dir or "").strip()
    return Path(fs.normal_path(data_dir)) / name / PREFS_FILENAME if name else None


def load(path: str | Path) -> RuntimePrefsStore:
    """在共享锁内加载运行偏好，主文件损坏时尝试良好备份并保留原文件。"""
    p = Path(fs.normal_path(path))
    with PREFS_LOCK:
        if not fs.is_file(p):
            store = RuntimePrefsStore()
            store._remember_source(p, None)
            return store
        try:
            return _read(p)
        except (OSError, UnicodeError, ValueError) as error:
            backup = p.with_name(p.name + ".bak")
            if fs.is_file(backup):
                try:
                    recovered = _read(backup)
                except (OSError, UnicodeError, ValueError):
                    pass
                else:
                    logger.warning("工作状态文件损坏，读取上一份良好备份；原文件保留: %s", p)
                    return recovered
            raise ValueError(tr("工作状态文件无法读取，原文件已保留：{p}: {error}", p=p, error=error)) from error


def _read(path: Path) -> RuntimePrefsStore:
    """读取并解析偏好文件，同时记录用于比较写入的原始来源字节。"""
    payload = fs.read_bytes(path)
    store = RuntimePrefsStore.from_dict(json.loads(payload.decode("utf-8-sig")))
    store._remember_source(path, payload)
    return store


def _current_bytes(path: Path) -> bytes | None:
    """读取当前文件字节，文件尚不存在时返回 None。"""
    try:
        return fs.read_bytes(path)
    except FileNotFoundError:
        return None


def save(path: str | Path, store: RuntimePrefsStore) -> bool:
    """在共享锁内比较原始快照，备份有效旧内容后原子替换偏好文件。

    外部修改、无效旧内容或写入错误均返回 False，并清理临时文件；成功后更新来源快照。"""
    p = Path(fs.normal_path(path))
    temporary = None
    with PREFS_LOCK:
        try:
            payload = json.dumps(store.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
            before = _current_bytes(p)
            if store._source_path is not None and before != store.before_image(p):
                raise ValueError(tr("工作状态文件已被外部修改，旧快照不能覆盖新记录"))
            fs.ensure_dir(p.parent)
            descriptor, temporary = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if before is not None:
                # 验证原内容有效后才将其作为良好备份。
                RuntimePrefsStore.from_dict(json.loads(before.decode("utf-8-sig")))
                fs.write_bytes(p.with_name(p.name + ".bak"), before)
            if _current_bytes(p) != before:
                raise ValueError(tr("工作状态文件已被外部修改，未替换原文件"))
            os.replace(temporary, str(p))
            temporary = None
            store._remember_source(p, payload)
            return True
        except (OSError, UnicodeError, TypeError, ValueError) as error:
            logger.error("工作状态保存失败，原文件保持不变: %s", error)
            return False
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
