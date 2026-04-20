"""管理独立命名变体、配置采集和可恢复的配置命令。"""

from __future__ import annotations

import copy
import json
import os
import stat
import uuid
from collections.abc import Iterable
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.core import migoto_user_ini as user_ini
from src.core import mod_runtime_prefs as prefs
from src.core.mod_package import (
    inspect_effective_inis,
    override_path_for,
    overrides_seeded,
)
from src.core.mod_variant_service import collect
from src.core.mod_variant_store import VARIANT_STORE_LOCK, ModVariantStore
from src.core.persist_vars import (
    AttributionIndex,
    ModIniSource,
    build_attribution_index,
    declaration_fingerprint,
    resolve_variant_values,
    snapshot_variables,
)
from src.core.variant_configuration import configuration_changes, default_values
from src.core.variant_journal import RuntimePatch
from src.core.variant_operation import FileChange, IniInventoryGuard, VariantOperation, digest, read_bytes
from src.models.mod_variant import ModVariant
from src.utils import fs
from src.utils.constants import MIGOTO_DISABLED_PREFIX
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_data_dir


@dataclass
class RestoreOutcome:
    """汇总配置恢复触及的 Mod、移除键、缺失引用和错误，供部署流程决定刷新提示。"""

    applied: tuple[str, ...] = ()
    removed_keys: int = 0
    needs_reload_hint: bool = False
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = field(default_factory=tuple)
    missing: tuple[str, ...] = ()

    @property
    def touched(self) -> bool:
        """报告是否应用了目标或移除了运行时键，跳过及错误本身不算配置变更。"""
        return bool(self.applied) or self.removed_keys > 0


@dataclass
class ConfigurationIndex(AttributionIndex):
    """变量声明及产生这些声明的精确源内容。"""

    source_checks: tuple[FileChange | IniInventoryGuard, ...] = ()


class ModVariantManager:
    """管理变体文件与运行时偏好，以文件原件校验、操作日志和资源保护协调配置事务。"""

    def __init__(self, manager) -> None:
        """建立会话身份、声明缓存及事务记录，并订阅库事件以使部署上下文失效。"""
        self._manager = manager
        self._watcher = None
        self._watch_notify = None
        self._watch_context = None
        self._watch_sequence = 0
        self._last_event_sequence = 0
        self._epoch = uuid.uuid4().hex
        self._session = uuid.uuid4().hex
        self._ini_cache = {}
        self._default_cache = {}
        self._prepared = {}
        self._operations = {}
        self._recovering = False
        self._prepared_enable_context = None
        listener = getattr(manager, "add_event_listener", None)
        if callable(listener):
            listener(self._on_library_event)

    def _on_library_event(self, event, **payload):
        """根据库变化使声明缓存和监控身份失效；已准备的启用事件复用冻结上下文。"""
        prepared = self._prepared_enable_context
        if event == "mod_enabled" and prepared is not None and prepared[0] == payload.get("mod_id"):
            self._prepared_enable_context = None
            self._epoch = uuid.uuid4().hex
            self._ini_cache.pop(prepared[0], None)
            self._watch_context = (
                dict(prepared[1], deployment_generation=self._epoch) if prepared[1] is not None else None
            )
            return
        if event in {
            "mod_enabled",
            "mod_disabled",
            "mod_deleted",
            "mod_updated",
            "mod_moved",
            "mod_renamed",
            "index_rebuilt",
            "game_changed",
            "mod_imported",
        }:
            self.invalidate(payload.get("mod_id", ""))

    def invalidate(self, mod_id: str = "") -> None:
        """更新部署世代并清除指定或全部声明缓存，已有监控器时重新冻结采集上下文。"""
        self._prepared_enable_context = None
        self._epoch = uuid.uuid4().hex
        if mod_id:
            self._ini_cache.pop(mod_id, None)
        else:
            self._ini_cache.clear()
        self._watch_context = None
        if self._watcher is not None:
            self._refresh_watch_context()

    def _refresh_watch_context(self, game_id=""):
        """在数据库可访问的调用线程冻结部署身份，再为监控器设置运行时文件路径。"""
        gid = self._game_id(game_id)
        path = self.user_ini_path(gid)
        self._watch_context = (
            {
                "game_id": gid,
                "deployment_generation": self._epoch,
                "mods": tuple(
                    {"mod_id": mod.mod_id, "game_id": gid, "deployment_generation": self._deployment_generation(mod)}
                    for mod in self._capture_mods(gid)
                ),
            }
            if path is not None
            else None
        )
        if self._watcher is not None:
            return self._watcher.watch(path)
        return False

    def set_change_notify(self, cb) -> None:
        """设置文件变更通知接收者；回调会在监控线程收到冻结身份，应自行安排后续线程切换。"""
        self._watch_notify = cb

    def refresh_watch(self, game_id: str = "") -> bool:
        """恢复遗留事务后启动用户配置监控；路径不可用或恢复有问题时返回 False，读取异常继续抛出。"""
        gid = self._game_id(game_id)
        path = self.user_ini_path(gid)
        try:
            if path is None or self.recover_pending(gid):
                if self._watcher is not None:
                    self._watcher.watch(None)
                self._watch_context = None
                return False
            self.load_prefs(gid)
        except (OSError, ValueError):
            self.stop_watch()
            raise
        if self._watcher is None:
            from src.core.user_ini_watcher import UserIniWatcher

            self._watcher = UserIniWatcher(self._on_user_ini_changed)
        return self._refresh_watch_context(gid)

    def stop_watch(self) -> None:
        """停止并释放持有的文件监控器，同时丢弃其冻结上下文。"""
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        self._watch_context = None

    def _on_user_ini_changed(self) -> None:
        """在监控线程发送带递增采集世代的冻结事件，不查询数据库或直接采集配置。"""
        # 监控线程只发送冻结的身份，不查询数据库。
        context = self._watch_context
        if self._watch_notify is None or context is None:
            return
        self._watch_sequence += 1
        event = dict(
            context,
            capture_generation=self._watch_sequence,
            mods=tuple(dict(mod, capture_generation=self._watch_sequence) for mod in context["mods"]),
        )
        self._watch_notify(event)

    def _game_id(self, game_id: str = "") -> str:
        """将空游戏参数解析为管理器当前选择，显式游戏 ID 保持原值。"""
        return game_id or self._manager.current_game_id

    def _migoto_root(self, game_id: str = "") -> Path | None:
        """解析并校验游戏的加载器目录，未配置或目录不可访问时返回 None。"""
        game = self._manager._game_service.get_game(self._game_id(game_id))
        if game is None or not game.migoto_path:
            return None
        root = Path(fs.normal_path(game.migoto_path))
        return root if fs.is_dir(root) else None

    def user_ini_path(self, game_id: str = "") -> Path | None:
        """按加载器配置解析实际用户 INI 路径，无法定位时返回 None。"""
        return user_ini.resolve_user_config_path(self._migoto_root(game_id))

    def prefs_file(self, game_id: str = "") -> Path | None:
        """按游戏资源目录定位私有工作状态文件，不在此处创建或加载文件。"""
        return prefs.prefs_path(self._manager._res_dir(self._game_id(game_id)), get_data_dir())

    def has_deployed_mods(self, game_id: str = "") -> bool:
        """检查索引中是否有启用且记录部署路径的 Mod，不执行部署目录扫描。"""
        return any(
            m.is_enabled and m.deployed_path for m in self._manager._index.get_enabled_mods(self._game_id(game_id))
        )

    def _get_mod(self, mod_id):
        """按 ID 获取当前游戏条目，缺失或属于其他游戏时抛出 ValueError。"""
        mods = self._manager._index.get_mods_by_ids([mod_id])
        mod = next(iter(mods), None)
        if mod is None or mod.game_id != self._game_id():
            raise ValueError(tr("Mod 不存在或不属于当前游戏"))
        return mod

    def _capture_mods(self, game_id):
        """筛选指定游戏中启用且部署目录实际存在的 Mod，作为运行时采集范围。"""
        return [
            mod
            for mod in self._manager._index.get_enabled_mods(game_id)
            if mod.deployed_path and fs.is_dir(mod.deployed_path)
        ]

    def _deploy_root_prefix(self, game_id: str) -> str:
        """计算部署目录相对加载器根目录的规范化命名空间前缀，不在根目录内时返回空串。"""
        root = self._migoto_root(game_id)
        deployment = self._manager._storage.get_deploy_dir()
        if root is None or deployment is None:
            return ""
        try:
            relative = Path(fs.normal_path(deployment)).relative_to(root)
        except ValueError:
            return ""
        return str(relative).replace("/", "\\").strip("\\").casefold()

    def _runtime_mapping_issue(self, game_id: str) -> str:
        """检查加载器和部署目录的相对关系，返回无法映射偏好的原因，无问题时返回空串。"""
        game = self._manager._game_service.get_game(game_id)
        if game is None or not game.migoto_path:
            return tr("未配置 3DMigoto 目录，无法定位外观偏好")
        if self._migoto_root(game_id) is None:
            return tr("3DMigoto 目录不存在或无法访问：{migoto_path}", migoto_path=game.migoto_path)
        if not self._deploy_root_prefix(game_id):
            deployment = self._manager._storage.get_deploy_dir()
            return tr(
                "部署目录 {deployment} 无法相对于 3DMigoto 目录 {migoto_path} 定位外观偏好",
                deployment=deployment,
                migoto_path=game.migoto_path,
            )
        return ""

    @staticmethod
    def _implicit_prefix(mod, deploy_root_prefix: str) -> str:
        """根据实际活动目录名构造隐式变量命名空间，停放目录先还原名称，无有效名称时返回空串。"""
        if not deploy_root_prefix:
            return ""
        raw = mod.folder_name or Path(fs.normal_path(mod.path)).stem
        if mod.deployed_path:
            from src.core.mod_storage import ModStorage

            actual = Path(fs.normal_path(mod.deployed_path))
            raw = (
                ModStorage.active_name_from_parked(actual, mod.mod_id)
                if actual.name.upper().startswith(MIGOTO_DISABLED_PREFIX.upper())
                else actual.name
            )
        name = str(raw).strip()
        if not name or name.upper().startswith(MIGOTO_DISABLED_PREFIX.upper()):
            return ""
        return f"{deploy_root_prefix}\\{name}".casefold()

    def _deployment_generation(self, mod) -> str:
        """摘要会话、资源内容及部署位置，标识采集状态所对应的精确部署身份。"""
        location = self._implicit_prefix(mod, self._deploy_root_prefix(mod.game_id))
        return digest(f"{self._session}|{mod.game_id}|{mod.mod_id}|{mod.content_hash}|{location}".encode())

    def _source_signature(self, mod):
        """组合资源哈希与有效 INI 清单版本，作为声明缓存复用的校验依据。"""
        return (mod.content_hash, inspect_effective_inis(mod.path))

    def build_index(self, game_id: str = "", *, mods=None, force=False) -> AttributionIndex:
        """构建变量归属索引及源文件校验原件；读取期间内容变化或重复路径校验冲突时抛出异常。"""
        gid = self._game_id(game_id)
        mods = self._capture_mods(gid) if mods is None else list(mods)
        prefix = self._deploy_root_prefix(gid)
        sources, checks = [], []
        for mod in mods:
            signature = self._source_signature(mod)
            cached = self._ini_cache.get(mod.mod_id)
            if force or cached is None or cached[0] != signature:
                inventory = signature[1]
                archive = (
                    read_bytes(inventory.path)
                    if not stat.S_ISDIR(inventory.body_version[0]) and inventory.seed_version is None
                    else None
                )
                inis = signature[1].read()
                guards = [IniInventoryGuard.source(inventory)]
                overridden = {rel for rel, _ in inventory.overrides}
                for rel, text in inis:
                    raw = text.encode("utf-8", errors="surrogateescape")
                    if rel in overridden:
                        guards.append(FileChange(inventory.override_root / rel, raw, raw))
                    else:
                        if stat.S_ISDIR(inventory.body_version[0]):
                            guards.append(FileChange(inventory.path / rel, raw, raw))
                        override = override_path_for(mod.path, rel)
                        if override is not None:
                            guards.append(FileChange(override, None, None))
                if archive is not None:
                    guards.append(FileChange(inventory.path, archive, archive))
                # 等长修改可能具有相同时间戳，缓存前核对实际内容；
                # 再次读取仅用于比较，不替换已有基线。
                for guard in guards:
                    if isinstance(guard, FileChange) and read_bytes(guard.path) != guard.before:
                        raise OSError(tr("有效 INI 在读取期间发生变化，请重试：{path}", path=guard.path))
                if self._source_signature(mod) != signature:
                    raise OSError(tr("有效 INI 在索引读取期间发生变化"))
                self._ini_cache[mod.mod_id] = (signature, inis, tuple(guards))
            else:
                inis = cached[1]
            checks.extend(self._ini_cache[mod.mod_id][2])
            if mod.is_enabled and mod.deployed_path:
                # 成员范围来自原始声明输入，避免后续扫描错误接纳部署侧新增内容。
                checks.append(IniInventoryGuard.deployed(mod.deployed_path, (rel for rel, _ in inis)))
                for rel, text in inis:
                    raw = text.encode("utf-8", errors="surrogateescape")
                    checks.append(FileChange(Path(mod.deployed_path) / rel, raw, raw))
            sources.append(ModIniSource(mod.mod_id, inis, self._implicit_prefix(mod, prefix)))
        index = build_attribution_index(sources)
        unique = {}
        for check in checks:
            key = (type(check), os.path.normcase(os.path.abspath(check.path)))
            if key in unique and unique[key] != check:
                raise ValueError(tr("声明源文件存在冲突的读取结果"))
            unique[key] = check
        return ConfigurationIndex(**vars(index), source_checks=tuple(unique.values()))

    def _index_for(self, mod, *, force=False):
        """为目标构建声明索引；目标已启用时纳入其他有效部署，以识别共享命名空间冲突。"""
        mods = self._capture_mods(mod.game_id) if mod.is_enabled else [mod]
        if mod.mod_id not in {item.mod_id for item in mods}:
            mods.append(mod)
        return self.build_index(mod.game_id, mods=mods, force=force)

    def load_prefs(self, game_id: str = ""):
        """读取指定游戏的工作状态；无法定位存储路径时返回 None，格式及读取错误由存储层报告。"""
        path = self.prefs_file(game_id)
        return prefs.load(path) if path is not None else None

    def save_prefs(self, store, game_id: str = "") -> bool:
        """将工作状态交给偏好存储持久化，路径不可用或保存失败时返回 False。"""
        path = self.prefs_file(game_id)
        return path is not None and prefs.save(path, store)

    def _current_preset_id(self, game_id):
        """优先读取索引中的当前预设 ID，缺失时由预设管理器取得当前预设。"""
        current = self._manager._index.get_current_preset_id(game_id)
        if current:
            return current
        return self._manager._preset_manager.get_current(game_id).id

    def _preset_members(self, game_id, preset_id):
        """取得指定游戏预设的启用成员集合，预设不存在或游戏不符时返回空集合。"""
        preset = self._manager._preset_manager.get_preset(preset_id)
        return set(preset.enabled_mod_ids) if preset is not None and preset.game_id == game_id else set()

    def _notify(self, mod_ids, game_id=""):
        """对去重后的 Mod ID 发布变体状态事件，事件携带明确的游戏身份。"""
        emit = getattr(self._manager, "_emit", None)
        if callable(emit):
            for mod_id in dict.fromkeys(mod_ids):
                emit("variant_state_changed", mod_id=mod_id, game_id=self._game_id(game_id))

    def collect(self, *, trigger: str, game_id: str = "", only_if_changed=False, event=None, allow_preset=False):
        """在偏好锁内采集稳定配置并校验事件世代；过期、未变化或事务阻塞时返回 None。"""
        gid = self._game_id(game_id)
        with prefs.PREFS_LOCK:
            if event is not None:
                if (
                    event.get("game_id") != gid
                    or event.get("deployment_generation") != self._epoch
                    or event.get("capture_generation", 0) <= self._last_event_sequence
                ):
                    return None
            pending = getattr(self._manager._preset_manager, "has_pending_apply", None)
            if not allow_preset and callable(pending) and pending(gid):
                return None
            if self.recover_pending(gid):
                return None
            mods = self._capture_mods(gid)
            if event is not None:
                expected = {m.mod_id: self._deployment_generation(m) for m in mods}
                contexts = event.get("mods", ())
                observed = {
                    m.get("mod_id"): m.get("deployment_generation") for m in contexts if m.get("game_id") == gid
                }
                if observed != expected or len(contexts) != len(expected):
                    return None
            path = self.user_ini_path(gid)
            store = self.load_prefs(gid)
            if path is None or store is None:
                return None
            try:
                snapshot = user_ini.read_stable(path)
            except user_ini.UserIniUnstableError as error:
                logger.warning("本次工作状态未采集 [%s]：%s", trigger, error)
                return None
            if snapshot is None:
                return None
            if (
                only_if_changed
                and store.runtime_file.path == str(path)
                and store.runtime_file.last_seen_hash == snapshot.sha256
            ):
                if event is not None:
                    self._last_event_sequence = event["capture_generation"]
                return None
            # 监控回调不得在界面线程打开尚未准备覆盖文件的归档。
            if only_if_changed:
                mods = [m for m in mods if overrides_seeded(m.path)]
            index = self.build_index(gid, mods=mods, force=not only_if_changed)
            report = self._capture_snapshot(snapshot, index, store, mods, gid)
            if event is not None:
                self._last_event_sequence = event["capture_generation"]
            if report.changed:
                self._notify((mod.mod_id for mod in mods), gid)
            return report

    def _capture_snapshot(self, snapshot, index, store, mods, gid):
        """将稳定快照归属到启用成员并记录部署世代，变化持久化失败时抛出 OSError。"""
        sequence = max((s.capture_generation for s in store.mods.values()), default=0) + 1
        report = collect(snapshot, index, store, enabled_mod_ids=[m.mod_id for m in mods], capture_generation=sequence)
        for mod in mods:
            state = store.mods.get(mod.mod_id)
            if state is None:
                continue
            generation = self._deployment_generation(mod)
            state.deployment_generation = generation
            for key in state.confirmed_keys:
                state.sources[key]["deployment_generation"] = generation
        if report.changed and not self.save_prefs(store, gid):
            raise OSError(tr("工作状态保存失败，旧记录保持不变"))
        return report

    def list_variants(self, mod_id):
        """列出当前游戏指定 Mod 的磁盘变体，条目身份或变体文件错误由下层报告。"""
        mod = self._get_mod(mod_id)
        return ModVariantStore(mod.path).list()

    def prepare_task(self, mod_id, **options):
        """在所有者线程采集投影，供独立预览或命令使用。"""
        from src.core.variant_task import VariantTask

        return VariantTask(self._manager, mod_id, **options)

    def references(self, mod_id, variant_id):
        """查询当前游戏中选用指定变体的组合预设，优先使用管理器的引用索引。"""
        pm = self._manager._preset_manager
        getter = getattr(pm, "variant_references", None)
        if callable(getter):
            return getter(mod_id, variant_id)
        return [p for p in pm.get_all_presets(self._game_id()) if p.mod_variants.get(mod_id) == variant_id]

    def _selected(self, mod, store):
        """启用成员优先采用当前组合的变体选择，其余情况读取私有工作状态中的选择。"""
        if mod.is_enabled:
            preset = self._manager._preset_manager.get_preset(self._current_preset_id(mod.game_id))
            if preset is not None and mod.mod_id in preset.enabled_mod_ids:
                return preset.mod_variants.get(mod.mod_id)
        state = store.mods.get(mod.mod_id)
        return state.selected_variant_id if state is not None else None

    def _target_values(self, mod, variant_id, index):
        """将命名变体映射为当前声明的目标值；选择为 None 时解析可用的默认值。"""
        if variant_id is None:
            return default_values(mod, index, self._default_cache)
        variant = ModVariantStore(mod.path).get(variant_id)
        return resolve_variant_values(index, mod.mod_id, variant)

    def get_state(self, mod_id):
        """生成变体展示状态和可保存原因，核对声明、采集及部署身份，并将预期读取异常转为原因文本。"""
        mod = self._get_mod(mod_id)
        state = prefs.ModWorkingState()
        result = {
            "mod_id": mod_id,
            "enabled": bool(mod.is_enabled),
            "selected_variant_id": None,
            "status": "unconfirmed",
            "can_save": False,
            "reason": "",
            "captured_at": "",
            "variable_count": 0,
            "confirmed_count": 0,
            "source_changed": False,
        }
        try:
            store = self.load_prefs(mod.game_id)
            if store is None:
                raise ValueError(tr("未配置游戏资源目录"))
            state = store.mods.get(mod_id, state)
            selected = self._selected(mod, store)
            result["selected_variant_id"] = selected
            index = self._index_for(mod)
            keys = set(index.keys_of(mod_id))
            fingerprint = declaration_fingerprint(index, mod_id)
            result.update(
                variable_count=len(keys),
                confirmed_count=len(keys & set(state.confirmed_keys)),
                captured_at=state.captured_at,
            )
            complete = (
                bool(keys)
                and keys == set(state.confirmed_keys)
                and fingerprint == state.declaration_fingerprint
                and state.deployment_generation == self._deployment_generation(mod)
            )
            result["can_save"] = bool(mod.is_enabled and complete)
            if not keys:
                result["reason"] = tr("此 Mod 没有可保存的持久化数值变量")
            elif not complete:
                result["reason"] = tr("工作状态尚未完整确认；请先在加载器中刷新保存并等待落盘")
            elif selected is None:
                # 加载器采集确认当前值，不能推导任意初始化命令的原始基线。
                result["status"] = "captured"
            else:
                expected = self._target_values(mod, selected, index)
                result["status"] = (
                    "clean"
                    if all(user_ini.values_equal(state.values[k], v) for k, v in expected.items())
                    else "modified"
                )
            if selected:
                variant = ModVariantStore(mod.path).get(selected)
                result["source_changed"] = bool(
                    variant.source_content_hash and variant.source_content_hash != mod.content_hash
                )
                if result["source_changed"]:
                    result["reason"] = tr("资源本体已更新，应用时会重新校验全部变量声明")
        except (ValueError, OSError, RuntimeError) as error:
            result["reason"] = str(error)
        return result

    @contextmanager
    def _command(self, mod_ids, *, allow_preset=False, operation_token="", owner_token=""):
        """在资源保护及文件锁内执行命令，先恢复事务并固定条目访问凭据，冲突时阻止写入。"""
        gid = self._game_id()
        pm = self._manager._preset_manager
        pending = getattr(pm, "has_pending_apply", None)
        if not allow_preset and callable(pending) and pending(gid):
            raise RuntimeError(tr("组合预设正在切换，请等待完成"))
        with (
            self._variant_scope(mod_ids, excluding=(operation_token, owner_token)),
            prefs.PREFS_LOCK,
            VARIANT_STORE_LOCK,
        ):
            problems = self.recover_pending(gid)
            if problems:
                raise RuntimeError(tr("存在待恢复的变体操作：") + "；".join(problems))
            if not allow_preset and callable(pending) and pending(gid):
                raise RuntimeError(tr("组合预设正在切换，请等待完成"))
            checker = getattr(self._manager, "require_mod_operation", None)
            with ExitStack() as stack:
                for mid in mod_ids:
                    if callable(checker):
                        # 变体编辑不修改 info.json。
                        from src.core.operation_preflight import access_file

                        receipt = checker(mid, deployment=True)
                        stack.enter_context(access_file(receipt.path, pin=True))
                        receipt.recheck()
                yield

    def _variant_scope(self, mod_ids, *, paths=(), excluding=()):
        """为源容器、部署、偏好及组合文件创建操作保护上下文，允许排除调用者已持有的令牌。"""
        protection = getattr(self._manager, "_library_protection", None)
        if protection is None:
            return nullcontext()
        mods = [self._get_mod(mid) for mid in mod_ids]
        targets = [path for mod in mods for path in (Path(mod.path).parent, mod.deployed_path)]
        targets.extend(
            (
                self.prefs_file(),
                self.user_ini_path(),
                getattr(self._manager._preset_manager, "_presets_file", ""),
                *paths,
            )
        )
        return protection.variant_operation(
            "variant:" + uuid.uuid4().hex, paths=targets, mod_ids=mod_ids, excluding=excluding
        )

    def _reference_updates(self, mods, selections, *, preset_id=""):
        """生成指定或当前组合中应更新的成员选择，未启用成员仅在显式预设操作中纳入。"""
        pm = self._manager._preset_manager
        current = pm.get_preset(preset_id or self._current_preset_id(self._game_id()))
        if current is None:
            return {}
        updates = {
            mid: selected
            for mid, selected in selections.items()
            if mid in current.enabled_mod_ids and (mods[mid].is_enabled or preset_id)
        }
        return {current.id: updates} if updates else {}

    def _reference_change(self, updates, add_members=None, *, require_unselected=False):
        """基于组合文件原件构造引用变更，成员或预设已变动时拒绝提交；此处不写磁盘。"""
        if not updates:
            return []
        pm = self._manager._preset_manager
        path = getattr(pm, "_presets_file", get_data_dir() / "presets.json")
        before = read_bytes(path)
        if before is None:
            raise OSError(tr("组合预设文件不存在，无法提交变体引用"))
        data = json.loads(before.decode("utf-8-sig"))
        seen = set()
        for record in data["presets"]:
            change = updates.get(record["id"])
            if change is None:
                continue
            references = record.setdefault("mod_variants", {})
            if require_unselected and any(references.get(mid) is not None for mid in change):
                raise ValueError(tr("组合已有命名变体选择，无法跳过外观偏好；原组合已保留：{path}", path=path))
            for mid, node_id in (add_members or {}).get(record["id"], {}).items():
                bucket = record["character_mods"].setdefault(node_id, [])
                if mid not in bucket:
                    bucket.append(mid)
            members = {mid for mids in record["character_mods"].values() for mid in mids}
            if not set(change) <= members:
                raise ValueError(tr("组合成员在变体操作期间发生变化"))
            references.update(change)
            record["updated_at"] = datetime.now(timezone.utc).isoformat()
            seen.add(record["id"])
        if seen != set(updates):
            raise ValueError(tr("组合预设在变体操作期间被删除"))
        return [FileChange(path, before, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))]

    def _publish_references(self):
        """将磁盘已落盘的选择和成员引用同步到预设内存对象，不生成新的持久化写入。"""
        pm = self._manager._preset_manager
        path = getattr(pm, "_presets_file", get_data_dir() / "presets.json")
        raw = read_bytes(path)
        if raw is None:
            return
        for record in json.loads(raw.decode("utf-8-sig")).get("presets", []):
            preset = pm.get_preset(record["id"])
            if preset is not None:
                preset.mod_variants = dict(record.get("mod_variants", {}))
                preset.updated_at = record.get("updated_at", preset.updated_at)
                durable = getattr(pm, "_durable_refs", None)
                if durable is not None and durable.get(preset.id) != record["character_mods"]:
                    durable[preset.id] = copy.deepcopy(record["character_mods"])
                    pm._resolve_references(preset)

    def _state_change(self, store, mods, targets, selections, index, *, source="applied", runtime=None):
        """更新内存工作状态的目标值及来源世代，并以保存前原件构造偏好文件变更。"""
        now = datetime.now(timezone.utc).isoformat()
        for mid, values in targets.items():
            state = store.state_for(mid)
            state.values = dict(values)
            state.selected_variant_id = selections[mid]
            state.declaration_fingerprint = declaration_fingerprint(index, mid)
            state.deployment_generation = self._deployment_generation(mods[mid])
            state.capture_generation += 1
            state.confirmed_keys = tuple(sorted(values))
            state.captured_at = now
            state.sources = {
                key: {
                    "source": source,
                    "captured_at": now,
                    "deployment_generation": state.deployment_generation,
                    "declaration_fingerprint": state.declaration_fingerprint,
                    "capture_generation": state.capture_generation,
                }
                for key in values
            }
        if runtime is not None:
            store.runtime_file = runtime
        path = self.prefs_file()
        return FileChange(
            path, store.before_image(path), json.dumps(store.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
        )

    def _execute(self, mod_ids, changes, *, metadata=None, preset_operation_id=""):
        """为组合事务补充变体资源保护，然后执行带原件校验的文件变更。"""
        scope = (
            self._variant_scope(
                mod_ids, paths=[c.path for c in changes], excluding=("preset-apply", "preset:" + preset_operation_id)
            )
            if preset_operation_id
            else nullcontext()
        )
        with scope:
            return self._execute_files(mod_ids, changes, metadata=metadata, preset_operation_id=preset_operation_id)
