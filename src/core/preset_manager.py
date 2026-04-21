"""管理各游戏的预设、启用状态和差量部署事务。

每个游戏保留一个不可删除的默认预设。Mod 启用、删除和移动操作同步更新
当前预设，删除 Mod 或类别时清理相关引用。

通过 preset_changed、preset_list_changed 和 preset_applied 信号通知
当前预设切换、预设列表变更及部署完成。
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

from src.core.game_config_store import GameConfigStore
from src.core.library_snapshot import library_mutation
from src.core.mod_identity import IdentityConflict, read_deployment_identity, validate_mod_id
from src.models.deployment import (
    DeploymentRecord,
    DeploymentState,
    PresetApplyJournal,
)
from src.models.mod import ModStatus
from src.models.preset import Preset
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_data_dir

if TYPE_CHECKING:
    from src.models.mod import Mod


@dataclass(frozen=True)
class _PresetMod:
    """提供预设规划需要的轻量 Mod 字段，并通过路径服务实时解析正文位置。"""

    mod_id: str
    node_id: str | None
    folder_name: str
    status: ModStatus
    body_path: Callable[[str], Path]

    @property
    def path(self) -> str:
        """按当前持久身份解析并返回正文路径字符串。"""
        return str(self.body_path(self.mod_id))

    @property
    def is_enabled(self) -> bool:
        """判断轻量记录的状态是否为已启用。"""
        return self.status == ModStatus.ENABLED


DEFAULT_PRESET_NAME = "默认"
# 预设版本标记用于诊断；读取时按字段校验，缺失字段使用默认值。
SCHEMA_VERSION = 2


class PresetManager(QObject):
    """预设管理器（非单例；ModManager 持有引用，UI 通过 ModManager 访问）"""

    preset_changed = Signal(str)  # game_id 当前预设切换
    preset_list_changed = Signal()  # 预设增删
    preset_applied = Signal(list)  # 切换完成，参数=受影响 mod_id

    def __init__(
        self,
        parent: QObject | None = None,
        mod_index=None,
        mod_storage=None,
    ) -> None:
        """初始化预设集合、持久与解析引用、事务钩子及修改计数，并加载预设文件。"""
        super().__init__(parent)
        self._library_epoch_lock = threading.RLock()
        self._library_epoch = 0
        self._library_mutations = 0
        self._game_store = GameConfigStore()
        self._presets: dict[str, Preset] = {}
        self._durable_refs: dict[str, dict[str, list[str]]] = {}
        self._resolved_refs: dict[str, dict[str, list[str]]] = {}
        self._presets_file: Path = get_data_dir() / "presets.json"
        self._mod_index = mod_index
        self._mod_storage = mod_storage
        self._before_plan_io = None
        self._before_reconcile = None
        self._after_plan_commit = None
        self._after_plan_rollback = None
        self._on_deployments_invalidated = None
        self._before_plan_commit = None
        self._on_runtime_rollback = None
        self._verify_configuration = None
        self._load()

    def _get_index(self):
        """返回注入的索引，尚未提供时延迟创建共享于该管理器的索引实例。"""
        if self._mod_index is None:
            from src.core.mod_index import ModIndex

            self._mod_index = ModIndex()
        return self._mod_index

    def _get_storage(self):
        """返回注入的存储服务，尚未提供时延迟创建实例。"""
        if self._mod_storage is None:
            from src.core.mod_storage import ModStorage

            self._mod_storage = ModStorage()
        return self._mod_storage

    def set_deployment_lifecycle_callbacks(
        self,
        *,
        before_io=None,
        after_commit=None,
        after_rollback=None,
        on_invalidated=None,
        before_commit=None,
        on_runtime_rollback=None,
        before_reconcile=None,
        verify_configuration=None,
    ) -> None:
        """注入主线程 INI 生命周期钩子，规划层不依赖 Qt。"""
        self._before_plan_io = before_io
        self._before_reconcile = before_reconcile
        self._after_plan_commit = after_commit
        self._after_plan_rollback = after_rollback
        self._on_deployments_invalidated = on_invalidated
        # 差量部署完成、数据库提交前执行的回调，用于保存运行时变体。
        self._before_plan_commit = before_commit
        #: 回滚时反向执行运行时补丁；返回问题列表（非空表示未能完整还原）
        self._on_runtime_rollback = on_runtime_rollback
        self._verify_configuration = verify_configuration

    def has_pending_apply(self, game_id: str = "") -> bool:
        """判断持久化预设事务是否仍占用部署状态。"""
        getter = getattr(self._get_index(), "get_journals", None)
        return bool(getter(game_id)) if callable(getter) else False

    # 持久化

    def _load(self) -> None:
        """加载预设，逐条过滤无效记录并保留其他有效项。

        JSON 无法解析或整体结构错误时备份原文件后重新初始化；
        单条无效记录写入诊断日志，未知字段保留，版本字段不阻止读取。
        """
        if not self._presets_file.exists():
            return
        try:
            with open(self._presets_file, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            self._invalidate(f"无法解析: {e}")
            return

        try:
            presets = self._parse_file(data)
        except ValueError as e:
            self._invalidate(str(e))
            return

        self._presets = presets
        for preset in presets.values():
            self._durable_refs[preset.id] = copy.deepcopy(preset.character_mods)
            preset.character_mods = {}
            self._resolve_references(preset)
        logger.info("预设已加载: %d 个", len(self._presets))

    @staticmethod
    def _parse_file(data) -> dict[str, Preset]:
        """把整份文件解析成 {id: Preset}；只有"整份读不动"才抛 ValueError。"""
        if not isinstance(data, dict):
            raise ValueError(tr("根节点是 {name__}，不是对象", name__=type(data).__name__))
        version = data.get("version")
        if version != SCHEMA_VERSION:
            logger.info(
                "预设文件 version=%r 与本程序的 %d 不同，按「缺失即默认」照常读取",
                version,
                SCHEMA_VERSION,
            )
        records = data.get("presets")
        if not isinstance(records, list):
            raise ValueError(tr("presets 字段是 {name__}，不是列表", name__=type(records).__name__))

        out: dict[str, Preset] = {}
        for record in records:
            try:
                preset = Preset.from_dict(record)
                for identities in preset.character_mods.values():
                    for identity in identities:
                        validate_mod_id(identity)
            except ValueError as e:
                logger.error("预设记录不合法，已跳过该条（其余预设不受影响）: %s；原始内容: %r", e, record)
                continue
            if preset.id in out:
                logger.error("预设 id 重复，已跳过后出现的这条: %s", preset.id)
                continue
            out[preset.id] = preset
        return out

    def _invalidate(self, reason: str) -> None:
        """整份文件判无效：改名保留原文件（用户可自行找回），预设从空开始。"""
        logger.error("预设文件无效（%s），已备份并从空开始: %s", reason, self._presets_file)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self._presets_file.with_name(f"{self._presets_file.name}.invalid-{stamp}")
        seq = 1
        while target.exists():
            target = self._presets_file.with_name(f"{self._presets_file.name}.invalid-{stamp}-{seq}")
            seq += 1
        try:
            os.replace(self._presets_file, target)
            logger.info("原预设文件已备份为: %s", target)
        except OSError as e:
            logger.error("备份无效预设文件失败: %s", e)
        self._presets = {}

    @library_mutation
    def _save(self) -> None:
        """采集必要的持久成员引用并原子保存预设文件。

        写入失败时恢复引用映射、清理临时文件并传播异常，避免内存引用被部分更新。"""
        temp_file = self._presets_file.with_name(self._presets_file.name + ".tmp")
        durable_before = self._durable_refs.copy()
        resolved_before = self._resolved_refs.copy()
        try:
            self._presets_file.parent.mkdir(parents=True, exist_ok=True)
            records = []
            for preset in self._presets.values():
                if (
                    preset.id not in self._durable_refs
                    or preset.id not in self._resolved_refs
                    or preset.character_mods != self._resolved_refs[preset.id]
                ):
                    self._capture_references(preset)
                record = {
                    "id": preset.id,
                    "name": preset.name,
                    "description": preset.description,
                    "game_id": preset.game_id,
                    "is_default": preset.is_default,
                    "character_mods": self._durable_refs[preset.id],
                    "mod_variants": {
                        mid: preset.mod_variants.get(mid)
                        for mids in self._durable_refs[preset.id].values()
                        for mid in mids
                    },
                    "created_at": preset.created_at,
                    "updated_at": preset.updated_at,
                }
                records.append(record)
            data = {
                "version": SCHEMA_VERSION,
                "presets": records,
            }
            with open(temp_file, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_file, self._presets_file)
        except Exception as e:
            self._durable_refs = durable_before
            self._resolved_refs = resolved_before
            logger.error("保存预设文件失败: %s", e)
            try:
                temp_file.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _capture_references(self, preset: Preset, mods=None) -> None:
        """保存运行时修改，同时保留空索引暂时无法解析的引用。"""
        represented = {mid for mids in self._resolved_refs.get(preset.id, {}).values() for mid in mids}
        durable = {
            cid: [identity for identity in identities if identity not in represented]
            for cid, identities in self._durable_refs.get(preset.id, {}).items()
        }
        for cid, mids in preset.character_mods.items():
            bucket = durable.setdefault(cid, [])
            included = set(bucket)
            for mid in mids:
                validate_mod_id(mid)
                if mid not in included:
                    bucket.append(mid)
                    included.add(mid)
        self._durable_refs[preset.id] = {cid: refs for cid, refs in durable.items() if refs}
        self._resolved_refs[preset.id] = {cid: list(mids) for cid, mids in preset.character_mods.items()}

    def _resolve_references(self, preset: Preset, mods=None) -> None:
        """按当前 Mod 身份与分类重新解析持久引用，更新预设成员及解析快照。"""
        if mods is None:
            mods = self._read_plan_mods(preset.game_id)
        by_identity = {mod.mod_id: mod for mod in mods}
        resolved: dict[str, list[str]] = {}
        for identities in self._durable_refs.get(preset.id, {}).values():
            for identity in identities:
                mod = by_identity.get(identity)
                if mod is not None:
                    bucket = resolved.setdefault(mod.node_id or "", [])
                    if mod.mod_id not in bucket:
                        bucket.append(mod.mod_id)
        preset.character_mods = resolved
        self._resolved_refs[preset.id] = copy.deepcopy(resolved)

    def refresh_references(self, game_id: str, mod_ids) -> None:
        """投影重建后刷新可用性，不重写用户选择。"""
        from types import SimpleNamespace

        changed = set(mod_ids)
        if not changed:
            return
        current = {}
        index = self._get_index()
        ids = tuple(changed)
        for offset in range(0, len(ids), 500):
            batch = ids[offset : offset + 500]
            marks = ",".join("?" for _ in batch)
            for identity, node_id in index._db().execute(
                f"SELECT mod_id,node_id FROM mods WHERE game_id=? AND mod_id IN ({marks})", (game_id, *batch)
            ):
                current[identity] = SimpleNamespace(mod_id=identity, node_id=node_id)
        for preset in self.get_all_presets(game_id):
            references = self._durable_refs.get(preset.id, {})
            if not any(changed.intersection(values) for values in references.values()):
                continue
            available = {
                identity: SimpleNamespace(mod_id=identity, node_id=node_id)
                for node_id, values in preset.character_mods.items()
                for identity in values
                if identity not in changed
            }
            available.update(current)
            self._resolve_references(preset, tuple(available.values()))

    def _read_plan_mods(self, game_id: str) -> tuple[_PresetMod, ...]:
        """规划字段独立且不可变，使用时才解析资源路径。"""
        try:
            index = self._get_index()
            cursor = index._db().execute(
                "SELECT m.mod_id,m.node_id,m.folder_name,COALESCE(d.state,'absent') "
                "FROM mods m LEFT JOIN deployment_records d ON d.mod_id=m.mod_id "
                "WHERE m.game_id=? ORDER BY m.name",
                (game_id,),
            )
            try:
                return tuple(
                    _PresetMod(
                        row[0],
                        row[1],
                        row[2],
                        ModStatus.ENABLED if row[3] == "active" else ModStatus.DISABLED,
                        index.paths.body_path,
                    )
                    for row in cursor.fetchall()
                )
            finally:
                cursor.close()
        except sqlite3.Error as e:
            # 索引尚待恢复时保留未解析的持久引用，避免加载预设提前失败。
            logger.error("获取预设 Mod 投影失败: %s", e)
            return ()

    # 默认预设

    def ensure_default(self, game_id: str) -> Preset:
        """幂等创建该游戏的默认预设"""
        if not game_id:
            raise ValueError(tr("game_id 必填"))
        default_id = f"default_{game_id}"
        existing = self._presets.get(default_id)
        if existing:
            return existing
        return self._create_default(game_id)

    @library_mutation
    def _create_default(self, game_id: str) -> Preset:
        """为游戏创建固定身份的默认预设，保存后发送列表变化通知。"""
        default_id = f"default_{game_id}"
        now = datetime.now().isoformat()
        p = Preset(
            id=default_id,
            name=DEFAULT_PRESET_NAME,
            description="",
            game_id=game_id,
            is_default=True,
            character_mods={},
            created_at=now,
            updated_at=now,
        )
        self._presets[default_id] = p
        self._save()
        self.preset_list_changed.emit()
        logger.info("默认预设已创建: %s", game_id)
        return p

    # 查询

    def get_preset(self, preset_id: str) -> Preset | None:
        """按预设编号返回内存对象，不存在时返回 None。"""
        return self._presets.get(preset_id)

    def get_all_presets(self, game_id: str) -> list[Preset]:
        """返回该游戏的预设列表；默认预设排第一"""
        ps = [p for p in self._presets.values() if p.game_id == game_id]
        ps.sort(key=lambda p: (0 if p.is_default else 1, p.created_at))
        return ps

    def get_unresolved_mod_ids(self, preset_id: str) -> tuple[str, ...]:
        """返回尚未解析的持久引用，不将未保存的成员移除误判为资源缺失。"""
        preset = self._presets.get(preset_id)
        if preset is None:
            return ()
        represented = {mid for mids in self._resolved_refs.get(preset_id, {}).values() for mid in mids}
        represented.update(preset.enabled_mod_ids)
        return tuple(
            dict.fromkeys(
                mid for mids in self._durable_refs.get(preset_id, {}).values() for mid in mids if mid not in represented
            )
        )

    @library_mutation
    def purge_game(self, game_id: str) -> int:
        """删除某游戏的**全部**预设（含默认预设）。删除游戏时用，返回删除条数。

        与 :meth:`delete_preset` 的差别：那条路刻意拒绝删默认预设（默认预设是运行期
        不变量），而游戏本身都要消失了，默认预设留着就是垃圾。
        """
        victims = [pid for pid, p in self._presets.items() if p.game_id == game_id]
        for pid in victims:
            self._presets.pop(pid, None)
            self._durable_refs.pop(pid, None)
            self._resolved_refs.pop(pid, None)
        if victims:
            self._save()
            logger.info("已删除游戏 %s 的 %d 个预设", game_id, len(victims))
        return len(victims)

    def get_current(self, game_id: str) -> Preset:
        """读取当前激活预设；若未设或指向不存在的 preset，回落到默认"""
        cur_id = self._game_store.get_current_preset_id(game_id)
        if cur_id and cur_id in self._presets:
            return self._presets[cur_id]
        default = self.ensure_default(game_id)
        self._game_store.set_current_preset_id(game_id, default.id)
        return default

    # 预设增删改查。

    @library_mutation
    def create_preset(
        self,
        game_id: str,
        name: str,
        snapshot: dict[str, list[str]] | None = None,
        description: str = "",
        copied_from: str = "",
    ) -> Preset:
        """创建非默认预设，并深拷贝 snapshot 中的启用集合。

        copied_from 指向已有预设时，复制所选 Mod 的命名变体引用；其余引用使用默认配置。
        """
        if not game_id:
            raise ValueError(tr("game_id 必填"))
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError(tr("name 不能为空"))
        preset_id = str(uuid.uuid4())[:8]
        cm = copy.deepcopy(snapshot) if snapshot else {}
        now = datetime.now().isoformat()
        p = Preset(
            id=preset_id,
            name=clean_name,
            description=description,
            game_id=game_id,
            is_default=False,
            character_mods=cm,
            mod_variants={mid: self._presets[copied_from].mod_variants.get(mid) for mids in cm.values() for mid in mids}
            if copied_from in self._presets
            else {mid: None for mids in cm.values() for mid in mids},
            created_at=now,
            updated_at=now,
        )
        self._presets[preset_id] = p
        self._save()
        self.preset_list_changed.emit()
        logger.info("预设已创建: %s (%s)", clean_name, preset_id)
        return p
