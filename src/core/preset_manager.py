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

    @library_mutation
    def delete_preset(self, preset_id: str) -> bool:
        """删除预设；默认预设拒绝删除"""
        p = self._presets.get(preset_id)
        if not p:
            return False
        if p.is_default:
            logger.warning("拒绝删除默认预设: %s", preset_id)
            return False
        del self._presets[preset_id]
        self._durable_refs.pop(preset_id, None)
        self._resolved_refs.pop(preset_id, None)
        self._save()
        self.garbage_collect_deployments(p.game_id)
        self.preset_list_changed.emit()
        logger.info("预设已删除: %s", preset_id)
        return True

    def prepare_preset_metadata(self, game_id, operation, *, preset_id="", name=None, description=None):
        """冻结元数据修改，交给独立的文件暂存操作。"""
        from types import SimpleNamespace

        from src.core.preset_delete import PreparedPresetDelete, StalePresetDelete, file_version

        if not game_id or self.has_pending_apply(game_id):
            raise ValueError(tr("宿主正在处理其他 Mod 任务，请稍后重试"))
        token, version = self._cascade_delete_token(), file_version(self._presets_file)
        source = self.get_preset(preset_id) if preset_id else None
        if operation != "create" and (source is None or source.game_id != game_id):
            raise ValueError(tr("预设不存在或不属于当前游戏"))
        if operation == "duplicate" and name is None:
            name = self.generate_unique_name(game_id, source.display_name + tr(" 副本"))
        if operation in {"create", "duplicate", "rename"}:
            name = (name or "").strip()
            if not name:
                raise ValueError(tr("预设名称不能为空"))
        now = datetime.now().isoformat()
        if operation in {"create", "duplicate"}:
            changed = Preset(
                id=str(uuid.uuid4())[:8],
                game_id=game_id,
                name=name,
                description=(source.description if source else description or ""),
                character_mods=copy.deepcopy(source.character_mods) if source else {},
                mod_variants=copy.deepcopy(source.mod_variants) if source else {},
                created_at=now,
                updated_at=now,
            )
        elif operation in {"rename", "update_description"}:
            changed = copy.deepcopy(source)
            if operation == "rename":
                changed.name = name
            else:
                changed.description = description
            changed.updated_at = now
        else:
            raise ValueError(tr("未知操作: {operation}", operation=operation))
        shadow = SimpleNamespace(
            _durable_refs=copy.deepcopy(self._durable_refs), _resolved_refs=copy.deepcopy(self._resolved_refs)
        )
        snapshots = dict(self._presets)
        snapshots[changed.id] = changed
        records = []
        for pid, preset in snapshots.items():
            if (
                pid not in shadow._durable_refs
                or pid not in shadow._resolved_refs
                or preset.character_mods != shadow._resolved_refs[pid]
            ):
                PresetManager._capture_references(shadow, preset)
            record = preset.to_dict()
            record["character_mods"] = shadow._durable_refs[pid]
            record["mod_variants"] = {
                mid: preset.mod_variants.get(mid) for mids in shadow._durable_refs[pid].values() for mid in mids
            }
            records.append(record)
        if self._cascade_delete_token() != token or file_version(self._presets_file) != version:
            raise StalePresetDelete(tr("预设已经变化，请重试"))
        temporary = self._presets_file.with_name(f".{self._presets_file.name}.metadata-{uuid.uuid4().hex}.tmp")
        return PreparedPresetDelete(
            id(self),
            "",
            game_id,
            self._presets_file,
            temporary,
            token,
            version,
            {"version": SCHEMA_VERSION, "presets": records},
            {changed.id: changed},
            {},
            shadow._durable_refs,
            shadow._resolved_refs,
        )

    @library_mutation
    def commit_preset_metadata(self, candidate, *, copied_from=""):
        """原子安装暂存内容，再发布发生变化的模型。"""
        from src.core.preset_delete import StalePresetDelete, discard_prepared_preset_delete, file_version

        try:
            if (
                candidate.owner_id != id(self)
                or candidate.committed
                or candidate.cancelled.is_set()
                or self._cascade_delete_token() != candidate.token
                or self.has_pending_apply(candidate.game_id)
                or file_version(self._presets_file) != candidate.target_version
                or not candidate.written.is_set()
                or candidate.staged_version is None
                or file_version(candidate.temporary) != candidate.staged_version
            ):
                raise StalePresetDelete(tr("预设已经变化，请重试"))
            (changed,) = candidate.runtime.values()
            os.replace(candidate.temporary, candidate.target)
            live = self._presets.get(changed.id)
            if live is None:
                self._presets[changed.id] = changed
            else:
                live.name, live.description, live.updated_at = changed.name, changed.description, changed.updated_at
            self._durable_refs, self._resolved_refs = candidate.durable, candidate.resolved
            candidate.committed = True
        except BaseException:
            discard_prepared_preset_delete(candidate)
            raise
        self.preset_list_changed.emit()
        return changed.id

    def prepare_delete_presets(self, preset_ids, game_id):
        """冻结批量删除候选，不修改活动预设或扫描文件。"""
        from types import SimpleNamespace

        from src.core.preset_delete import PreparedPresetDelete, StalePresetDelete, file_version

        ids = tuple(dict.fromkeys(preset_ids))
        if not ids:
            raise ValueError(tr("请先选择要删除的预设"))
        for pid in ids:
            preset = self.get_preset(pid)
            if preset is None or preset.game_id != game_id or preset.is_default:
                raise ValueError(tr("所选预设已变化，默认预设不能删除"))
        if self.get_current(game_id).id in ids or self.has_pending_apply(game_id):
            raise ValueError(tr("请先完成默认预设的切换，再删除当前预设"))
        token = self._cascade_delete_token()
        version = file_version(self._presets_file)
        shadow = SimpleNamespace(
            _durable_refs=copy.deepcopy(self._durable_refs), _resolved_refs=copy.deepcopy(self._resolved_refs)
        )
        records = []
        for pid, live in self._presets.items():
            if pid in ids:
                shadow._durable_refs.pop(pid, None)
                shadow._resolved_refs.pop(pid, None)
                continue
            preset = copy.deepcopy(live)
            if (
                pid not in shadow._durable_refs
                or pid not in shadow._resolved_refs
                or preset.character_mods != shadow._resolved_refs[pid]
            ):
                PresetManager._capture_references(shadow, preset)
            record = preset.to_dict()
            record["character_mods"] = shadow._durable_refs[pid]
            record["mod_variants"] = {
                mid: preset.mod_variants.get(mid) for mids in shadow._durable_refs[pid].values() for mid in mids
            }
            records.append(record)
        referenced = {
            mid
            for record in records
            if record["game_id"] == game_id
            for mids in record["character_mods"].values()
            for mid in mids
        }
        cleanup = tuple(
            record
            for record in self._get_index().get_deployments(game_id)
            if record.state == DeploymentState.DISABLED and record.actual_path and record.mod_id not in referenced
        )
        if self._cascade_delete_token() != token or file_version(self._presets_file) != version:
            raise StalePresetDelete(tr("准备删除时预设已发生变化，请重试"))
        temporary = self._presets_file.with_name(f".{self._presets_file.name}.delete-{uuid.uuid4().hex}.tmp")
        return PreparedPresetDelete(
            id(self),
            "",
            game_id,
            self._presets_file,
            temporary,
            token,
            version,
            {"version": SCHEMA_VERSION, "presets": records},
            {},
            {},
            shadow._durable_refs,
            shadow._resolved_refs,
            removed_presets=ids,
            cleanup_records=cleanup,
        )

    @library_mutation
    def commit_delete_presets(self, candidate):
        """核对候选所有者、取消状态、文件版本和当前预设后发布删除结果。

        原子替换已写完的暂存文件后同步内存集合；提交前任一条件不符时丢弃候选并抛出异常。"""
        from src.core.preset_delete import StalePresetDelete, discard_prepared_preset_delete, file_version

        try:
            if (
                candidate.owner_id != id(self)
                or candidate.committed
                or candidate.cancelled.is_set()
                or self._cascade_delete_token() != candidate.token
                or file_version(self._presets_file) != candidate.target_version
                or self.get_current(candidate.game_id).id in candidate.removed_presets
            ):
                raise StalePresetDelete(tr("预设已经变化，本次未删除，请重试"))
            if (
                not candidate.written.is_set()
                or candidate.staged_version is None
                or file_version(candidate.temporary) != candidate.staged_version
            ):
                raise StalePresetDelete(tr("预设文件尚未写入完成"))
            os.replace(candidate.temporary, candidate.target)
            for pid in candidate.removed_presets:
                self._presets.pop(pid, None)
            self._durable_refs, self._resolved_refs = candidate.durable, candidate.resolved
            candidate.committed = True
        except BaseException:
            discard_prepared_preset_delete(candidate)
            raise
        self.preset_list_changed.emit()

    @library_mutation
    def publish_deleted_preset_deployments(self, records, game_id):
        """仅对仍与清理快照一致的部署记录写入缺失状态，避免覆盖并发变化。"""
        index = self._get_index()
        current = {record.mod_id: record for record in index.get_deployments(game_id)}
        with index.transaction():
            for record in records:
                if current.get(record.mod_id) == record:
                    index.upsert_deployment(DeploymentRecord(record.mod_id), commit=False)

    @library_mutation
    def update_preset_meta(
        self,
        preset_id: str,
        name: str | None = None,
        description: str | None = None,
    ) -> bool:
        """更新预设的 name/description（不动 character_mods）"""
        p = self._presets.get(preset_id)
        if not p:
            return False
        if name is not None:
            clean = name.strip()
            if not clean:
                return False
            p.name = clean
        if description is not None:
            p.description = description
        p.updated_at = datetime.now().isoformat()
        self._save()
        self.preset_list_changed.emit()
        return True

    def generate_unique_name(self, game_id: str, base: str) -> str:
        """生成不与同游戏现有预设重名的名称。

        优先返回 base；冲突时尝试 "base 2"、"base 3"...
        若 base 末尾是 " N" 形式则递增 N。
        """
        existing = {p.name for p in self.get_all_presets(game_id)}
        if base not in existing:
            return base
        # 提取尾部数字（如 "新建预设 2" 提取出 2）
        import re

        m = re.match(r"^(.*?)\s*(\d+)$", base)
        if m:
            root = m.group(1).rstrip() or base
            n = int(m.group(2)) + 1
        else:
            root = base
            n = 2
        while f"{root} {n}" in existing:
            n += 1
        return f"{root} {n}"

    # 自动同步

    def variant_references(self, mod_id: str, variant_id: str) -> list[Preset]:
        """列出引用指定 Mod 变体的全部预设。"""
        return [p for p in self._presets.values() if p.mod_variants.get(mod_id) == variant_id]

    @library_mutation
    def set_mod_variant(self, preset_id: str, mod_id: str, variant_id: str | None) -> None:
        """为预设已启用成员设置变体引用，保存失败时恢复旧映射。"""
        preset = self._presets[preset_id]
        if mod_id not in preset.enabled_mod_ids:
            raise ValueError(tr("只能为组合中的启用 Mod 设置变体引用"))
        before = dict(preset.mod_variants)
        preset.mod_variants[mod_id] = variant_id
        try:
            self._save()
        except Exception:
            preset.mod_variants = before
            raise

    @library_mutation
    def sync_mod_state(self, mod: Mod, game_id: str) -> None:
        """根据 mod.status 更新当前预设；无 character_id 则跳过"""
        if not mod or not mod.node_id:
            return
        if self.has_pending_apply(game_id):
            logger.debug("预设切换事务期间忽略自动状态回写: %s", mod.mod_id)
            return
        cur = self.get_current(game_id)
        if mod.is_enabled:
            cur.add_mod_for_character(mod.node_id, mod.mod_id)
        else:
            cur.remove_mod(mod.mod_id)
        self._save()

    @library_mutation
    def strip_mod_ids(self, mod_ids: list[str], game_id: str) -> None:
        """从当前预设批量移除 mod_id（用于 enforce_single_mod 副作用）"""
        if not mod_ids:
            return
        cur = self.get_current(game_id)
        for mid in mod_ids:
            cur.remove_mod(mid)
        self._save()

    # 级联清理

    def _cascade_delete_token(self):
        """包含尚未保存的原地修改，不仅比较方法调用计数。"""

        def mapping(value):
            """将成员映射冻结为元组，供级联删除候选比较内存版本。"""
            return tuple((key, tuple(ids)) for key, ids in value.items())

        records = tuple(
            (
                pid,
                id(preset),
                tuple(
                    (
                        item.name,
                        mapping(preset.character_mods)
                        if item.name == "character_mods"
                        else tuple(sorted(preset.mod_variants.items()))
                        if item.name == "mod_variants"
                        else getattr(preset, item.name),
                    )
                    for item in fields(Preset)
                ),
            )
            for pid, preset in self._presets.items()
        )
        return (
            str(self._presets_file),
            records,
            tuple((pid, mapping(value)) for pid, value in self._durable_refs.items()),
            tuple((pid, mapping(value)) for pid, value in self._resolved_refs.items()),
        )

    def prepare_cascade_delete(self, mod_id: str, game_id: str):
        """在所有者线程采集独立候选，不修改活动选择。"""
        from types import SimpleNamespace

        from src.core.preset_delete import PreparedPresetDelete, StalePresetDelete, file_version

        if not mod_id:
            return None
        if not any(
            preset.game_id == game_id
            and (
                any(mod_id in ids for ids in preset.character_mods.values())
                or any(mod_id in ids for ids in self._durable_refs.get(pid, {}).values())
            )
            for pid, preset in self._presets.items()
        ):
            return None
        token = self._cascade_delete_token()
        version = file_version(self._presets_file)

        def clone(mapping):
            """复制成员映射及各成员列表，隔离待提交候选与当前预设。"""
            return {key: list(ids) for key, ids in mapping.items()}

        shadow = SimpleNamespace(
            _durable_refs={pid: clone(value) for pid, value in self._durable_refs.items()},
            _resolved_refs={pid: clone(value) for pid, value in self._resolved_refs.items()},
        )
        runtime, updated, records = {}, {}, []
        dirty = False
        for pid, live in self._presets.items():
            preset = copy.copy(live)
            preset.character_mods = clone(live.character_mods)
            preset.mod_variants = dict(live.mod_variants)
            if preset.game_id == game_id:
                durable = shadow._durable_refs.get(pid, {})
                captured = (
                    pid in shadow._durable_refs
                    and pid in shadow._resolved_refs
                    and preset.character_mods == shadow._resolved_refs[pid]
                )
                in_runtime = any(mod_id in ids for ids in preset.character_mods.values())
                in_durable = any(mod_id in ids for ids in durable.values())

                def remove(mapping):
                    """从候选成员映射剔除目标 Mod，并移除因此变空的分类条目。"""
                    return {
                        key: kept for key, ids in mapping.items() if (kept := [mid for mid in ids if mid != mod_id])
                    }

                if in_runtime:
                    preset.character_mods = remove(preset.character_mods)
                    preset.updated_at = datetime.now().isoformat()
                    runtime[pid] = preset.character_mods
                    updated[pid] = preset.updated_at
                if in_durable:
                    shadow._durable_refs[pid] = remove(durable)
                if captured and (in_runtime or in_durable):
                    shadow._resolved_refs[pid] = clone(preset.character_mods)
                dirty |= in_runtime or in_durable
                if in_runtime or in_durable:
                    preset.mod_variants.pop(mod_id, None)
            if (
                pid not in shadow._durable_refs
                or pid not in shadow._resolved_refs
                or preset.character_mods != shadow._resolved_refs[pid]
            ):
                PresetManager._capture_references(shadow, preset)
            record = {item.name: getattr(preset, item.name) for item in fields(Preset) if item.name != "character_mods"}
            # 保持原有 JSON 键顺序。
            record = {
                key: (shadow._durable_refs[pid] if key == "character_mods" else record[key])
                for key in (
                    "id",
                    "name",
                    "description",
                    "game_id",
                    "is_default",
                    "character_mods",
                    "mod_variants",
                    "created_at",
                    "updated_at",
                )
            }
            record["mod_variants"] = {
                mid: preset.mod_variants.get(mid) for mids in shadow._durable_refs[pid].values() for mid in mids
            }
            records.append(record)
        if not dirty:
            return None
        if self._cascade_delete_token() != token or file_version(self._presets_file) != version:
            raise StalePresetDelete("Preset changed while preparing deletion")
        temporary = self._presets_file.with_name(f".{self._presets_file.name}.delete-{uuid.uuid4().hex}.tmp")
        return PreparedPresetDelete(
            id(self),
            mod_id,
            game_id,
            self._presets_file,
            temporary,
            token,
            version,
            {"version": SCHEMA_VERSION, "presets": records},
            runtime,
            updated,
            shadow._durable_refs,
            shadow._resolved_refs,
        )

    @library_mutation
    def commit_cascade_delete(self, candidate) -> None:
        """仅发布与已刷新到磁盘的候选完全一致的预设选择。"""
        from src.core.preset_delete import StalePresetDelete, discard_prepared_preset_delete, file_version

        if candidate.committed:
            raise StalePresetDelete("Preset deletion candidate was already committed")
        try:
            if (
                candidate.owner_id != id(self)
                or candidate.cancelled.is_set()
                or self._cascade_delete_token() != candidate.token
                or file_version(self._presets_file) != candidate.target_version
            ):
                raise StalePresetDelete("Preset changed before deletion publication")
            if (
                not candidate.written.is_set()
                or candidate.staged_version is None
                or file_version(candidate.temporary) != candidate.staged_version
            ):
                raise StalePresetDelete("Preset deletion candidate is not durably staged")
            os.replace(candidate.temporary, candidate.target)
            for pid, runtime in candidate.runtime.items():
                self._presets[pid].character_mods = runtime
                self._presets[pid].updated_at = candidate.updated_at[pid]
            for preset in self._presets.values():
                if preset.game_id == candidate.game_id:
                    preset.mod_variants.pop(candidate.mod_id, None)
            self._durable_refs = candidate.durable
            self._resolved_refs = candidate.resolved
            candidate.committed = True
        except BaseException:
            discard_prepared_preset_delete(candidate)
            raise

    @library_mutation
    def cascade_delete_mod(self, mod_id: str, game_id: str) -> None:
        """删除某 mod 时清理该游戏所有预设的引用"""
        if not mod_id:
            return
        durable_before = self._durable_refs.copy()
        resolved_before = self._resolved_refs.copy()
        changed = []
        try:
            for preset in self._presets.values():
                if preset.game_id != game_id:
                    continue
                runtime = preset.character_mods
                durable = self._durable_refs.get(preset.id, {})
                captured = (
                    preset.id in self._durable_refs
                    and preset.id in self._resolved_refs
                    and runtime == self._resolved_refs[preset.id]
                )
                in_runtime = any(mod_id in ids for ids in runtime.values())
                in_durable = any(mod_id in ids for ids in durable.values())
                if not in_runtime and not in_durable:
                    continue
                changed.append((preset, runtime, preset.updated_at, dict(preset.mod_variants)))
                preset.mod_variants.pop(mod_id, None)

                def remove(mapping):
                    """删除各分类中的目标成员，并仅保留仍有成员的分类映射。"""
                    return {
                        cid: remaining
                        for cid, ids in mapping.items()
                        if (remaining := [mid for mid in ids if mid != mod_id])
                    }

                if in_runtime:
                    preset.character_mods = remove(runtime)
                    preset.updated_at = datetime.now().isoformat()
                if in_durable:
                    self._durable_refs[preset.id] = remove(durable)
                if captured:
                    self._resolved_refs[preset.id] = {cid: list(ids) for cid, ids in preset.character_mods.items()}
            if changed:
                self._save()
        except Exception:
            for preset, runtime, updated_at, variants in changed:
                preset.character_mods = runtime
                preset.updated_at = updated_at
                preset.mod_variants = variants
            self._durable_refs = durable_before
            self._resolved_refs = resolved_before
            raise

    @library_mutation
    def cascade_delete_character(self, character_id: str, game_id: str) -> None:
        """删除某角色时清理该游戏所有预设的对应 key"""
        if not character_id:
            return
        dirty = False
        for p in self._presets.values():
            if p.game_id != game_id:
                continue
            if self._durable_refs.get(p.id, {}).pop(character_id, None) is not None:
                dirty = True
            if character_id in p.character_mods:
                p.remove_character(character_id)
                dirty = True
        if dirty:
            self._save()

    @library_mutation
    def cascade_move_mod(
        self,
        mod_id: str,
        old_character_id: str,
        new_character_id: str,
        game_id: str,
    ) -> None:
        """move_mod 后同步预设：从旧角色 list 移除并加入新角色 list"""
        if not mod_id or old_character_id == new_character_id:
            return
        dirty = False
        for p in self._presets.values():
            if p.game_id != game_id:
                continue
            bucket = p.character_mods.get(old_character_id, [])
            if mod_id not in bucket:
                continue
            bucket.remove(mod_id)
            if not bucket:
                del p.character_mods[old_character_id]
            new_bucket = p.character_mods.setdefault(new_character_id, [])
            if mod_id not in new_bucket:
                new_bucket.append(mod_id)
            p.updated_at = datetime.now().isoformat()
            dirty = True
        if dirty:
            self._save()

    @library_mutation
    def refresh_after_rebuild(self, game_id: str) -> int:
        """刷新节点分组，保持 Mod 身份稳定。"""
        mods = self._read_plan_mods(game_id)
        changed = 0
        save_needed = False
        for preset in self.get_all_presets(game_id):
            before = copy.deepcopy(preset.character_mods)
            durable_before = copy.deepcopy(self._durable_refs.get(preset.id, {}))
            self._capture_references(preset)
            self._resolve_references(preset, mods)
            self._capture_references(preset)
            if before != preset.character_mods:
                changed += 1
            save_needed |= durable_before != self._durable_refs[preset.id]
        if save_needed:
            self._save()
        return changed

    def _refresh_deployments(self, game_id: str, *, locations=None, rows=None, records=None) -> None:
        """通过宿主钩子或默认对账服务刷新部署状态，可复用已采集的位置和记录。"""
        if self._before_reconcile is not None:
            if locations is None:
                self._before_reconcile(game_id)
            else:
                self._before_reconcile(game_id, locations=locations, rows=rows, records=records)
        else:
            from src.core.deployment_reconcile import reconcile_deployment_records

            reconcile_deployment_records(
                self._get_index(),
                self._get_storage(),
                game_id,
                force=True,
                locations=locations,
                rows=rows,
                records=records,
            )

    @library_mutation
    def prepare_preset_apply(self, preset_id: str, *, preparation=None):
        """在主线程创建、校验并记录不可变计划。

        没有部署目录时允许不涉及文件的状态切换，需要部署文件时拒绝执行并说明原因。
        """
        from src.core.preset_deployment_plan import (
            build_preset_deployment_plan,
            validate_plan_filesystem,
        )

        target = self._presets.get(preset_id)
        if target is None:
            raise ValueError(f"preset does not exist: {preset_id}")
        if preparation is not None and target.game_id != preparation.game_id:
            raise ValueError(tr("预设不存在或不属于当前游戏"))
        idx = self._get_index()
        if self.has_pending_apply(target.game_id):
            raise RuntimeError(tr("另一个预设切换事务尚未完成"))
        storage = preparation.storage if preparation is not None else self._get_storage()
        records = {r.mod_id: r for r in idx.get_deployments(target.game_id)} if preparation is not None else None
        # 用户操作核对计划中的精确路径，位置修复和重新对账由后台审计负责。
        root = storage.get_deploy_dir()
        state_only = root is None
        if state_only:
            # 占位根目录：零文件操作的计划不会用到它；一旦发现有动作就在下面拒绝。
            root = get_data_dir()
        mods = preparation.mods if preparation is not None else self._read_plan_mods(target.game_id)
        if records is None:
            records = {r.mod_id: r for r in idx.get_deployments(target.game_id)}
        self._capture_references(target, mods)
        self._resolve_references(target, mods)
        resolved = {mod.mod_id for mod in mods}
        unresolved = {
            identity
            for identities in self._durable_refs.get(target.id, {}).values()
            for identity in identities
            if identity not in resolved
        }
        if unresolved:
            raise IdentityConflict(tr("预设引用的源 Mod 暂未恢复：") + ", ".join(sorted(unresolved)))
        if state_only and (target.enabled_mod_ids or any(mod.is_enabled for mod in mods)):
            raise RuntimeError(tr("未配置「Mod 文件夹」，无法切换需要部署的预设"))
        current_id = idx.get_current_preset_id(target.game_id)
        if not current_id:
            current_id = self._game_store.get_current_preset_id(target.game_id) or ""
        plan = build_preset_deployment_plan(
            game_id=target.game_id,
            from_preset_id=current_id,
            to_preset_id=preset_id,
            mods=mods,
            target_mod_ids=set(target.enabled_mod_ids),
            deployments=list(records.values()),
            deploy_root=root,
        )
        if state_only and plan.actions:
            raise RuntimeError(
                tr(
                    "未配置「Mod 文件夹」，无法部署该预设中的 Mod。"
                    "请先在「游戏」页设置「Mod 文件夹」，或先禁用当前已启用的 Mod。"
                )
            )
        if preparation is None:
            validate_plan_filesystem(plan)
        journal = PresetApplyJournal(
            operation_id=plan.operation_id,
            game_id=plan.game_id,
            from_preset_id=plan.from_preset_id,
            to_preset_id=plan.to_preset_id,
            phase="planned",
            plan_json=plan.to_json(),
        )
        idx.save_journal(journal)
        try:
            if preparation is None and self._before_plan_io is not None:
                self._before_plan_io(plan)
        except Exception:
            idx.delete_journal(plan.operation_id)
            raise
        return plan

    @library_mutation
    def mark_preset_apply_executing(self, operation_id: str) -> None:
        """将预设恢复日志推进到执行阶段，并初始化已完成步骤列表。"""
        self._get_index().update_journal(operation_id, phase="executing", completed_steps_json="[]")

    @library_mutation
    def record_preset_apply_steps(self, operation_id: str, completed) -> None:
        """序列化已完成部署步骤并更新执行阶段日志，供失败或重启时恢复。"""
        idx = self._get_index()
        idx.update_journal(
            operation_id,
            phase="executing",
            completed_steps_json=json.dumps([asdict(step) for step in completed], ensure_ascii=False),
        )

    @library_mutation
    def commit_preset_apply(self, plan, completed, *, preparation=None) -> list[str]:
        """持有偏好和变体锁，核对并固定全部源对象后提交预设切换状态。"""
        from contextlib import ExitStack

        from src.core.mod_runtime_prefs import PREFS_LOCK
        from src.core.mod_variant_store import VARIANT_STORE_LOCK
        from src.core.operation_preflight import OperationPreflightError, access_file, confirm_source

        with PREFS_LOCK, VARIANT_STORE_LOCK, ExitStack() as stack:
            for action in plan.actions:
                mod = self._get_index().get_mod(action.mod_id)
                if mod is None or mod.path != action.source_path:
                    raise OperationPreflightError(action.mod_id, action.source_path, tr("预设源位置已变化"))
                previous = preparation.receipts.get(action.mod_id) if preparation is not None else None
                receipt = confirm_source(
                    action.source_path, action.mod_id, mod.content_hash, full=previous is None, expected=previous
                )
                stack.enter_context(access_file(receipt.path, pin=True))
                receipt.recheck()
            return self._commit_preset_apply(plan, completed, preparation=preparation)
