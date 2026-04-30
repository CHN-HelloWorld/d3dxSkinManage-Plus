"""为禁用和预设操作准备独立的文件系统工作。

所有者一次采集数据库投影和配置，工作线程不持有数据库连接或界面模型。
变体采集针对该操作的数据快照执行，发布前持续排除其他修改。
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

from src.core.mod_identity import IdentityConflict, read_deployment_identity
from src.core.mod_variant_manager import ModVariantManager
from src.utils.i18n import t as tr
from src.utils.logger import logger


class DeploymentPreparation:
    """隔离预设与 Mod 的数据快照，并组织部署前校验、文件操作及发布后同步。"""

    def __init__(self, manager, *, trigger: str, mod_ids=None, plan=None):
        """从所有者管理器复制本次操作所需记录，创建不依赖数据库连接的变体服务。"""
        self.game_id = manager.current_game_id
        self.trigger = trigger
        self.storage = manager._storage
        self.sync = manager._ensure_ini_sync()
        self.source_root = manager._get_library_root(self.game_id)
        game = copy.deepcopy(manager._game_service.get_game(self.game_id))
        resource_dir = manager._res_dir(self.game_id)
        pm = manager.preset_manager
        current_id = pm.get_current(self.game_id).id
        self.presets = {p.id: copy.deepcopy(p) for p in pm.get_all_presets(self.game_id)}
        if mod_ids is None:
            if plan is not None:
                mod_ids = {action.mod_id for action in plan.actions}
                for preset_id in (plan.from_preset_id, plan.to_preset_id):
                    preset = self.presets.get(preset_id)
                    if preset is not None:
                        mod_ids.update(preset.enabled_mod_ids)
            else:
                mod_ids = {mid for preset in self.presets.values() for mid in preset.enabled_mod_ids}
                mod_ids.update(m.mod_id for m in manager._index.get_enabled_mods(self.game_id))
        self.mods = [
            SimpleNamespace(
                mod_id=m.mod_id,
                game_id=m.game_id,
                path=m.path,
                deployed_path=m.deployed_path,
                folder_name=m.folder_name,
                is_enabled=m.is_enabled,
                node_id=m.node_id,
                content_hash=m.content_hash,
            )
            for m in manager._index.get_mods_by_ids(mod_ids)
        ]
        # 工作线程更新独立模型时保留采集的数据库状态，供对账比较而无需重新读取全部记录。
        self.reconcile_rows = tuple(
            SimpleNamespace(mod_id=m.mod_id, path=m.path, deployed_path=m.deployed_path, enabled=m.is_enabled)
            for m in self.mods
        )
        self.current_id = current_id
        self.journals = list(manager._index.get_journals())
        self.patches: dict[str, str] = {}
        self.patch_sink = None
        self.ini_refs: dict[str, tuple[str, ...]] = {}
        self.locations = None
        self.restore_outcome = None
        self.validated_operation_id = ""
        mods_by_id = {m.mod_id: m for m in self.mods}
        index = SimpleNamespace(
            get_all_mods=lambda game_id: self.mods,
            get_enabled_mods=lambda game_id: [m for m in self.mods if m.is_enabled],
            get_mods_by_ids=lambda ids: [mods_by_id[mid] for mid in ids if mid in mods_by_id],
            get_mod_ids=lambda game_id: {m.mod_id for m in self.mods},
            get_current_preset_id=lambda game_id: self.current_id,
            update_journal_runtime_patch=self._record_patch,
            get_journals=lambda: (
                [j for j in self.journals if j.operation_id not in self.patches]
                + [SimpleNamespace(operation_id=op, runtime_patch_json=raw) for op, raw in self.patches.items()]
            ),
        )
        snapshot = SimpleNamespace(
            current_game_id=self.game_id,
            _index=index,
            _storage=self.storage,
            _game_service=SimpleNamespace(get_game=lambda game_id: game),
            _res_dir=lambda game_id: resource_dir,
            _get_library_root=lambda game_id: self.source_root,
            _ini_sync=self.sync,
            _library_protection=manager._library_protection,
            _preset_manager=SimpleNamespace(
                _presets_file=pm._presets_file,
                get_all_presets=lambda game_id: list(self.presets.values()),
                get_current=lambda game_id: self.presets[self.current_id],
                get_preset=self.presets.get,
            ),
            _ensure_ini_sync=lambda: self.sync,
        )
        self.variants = ModVariantManager(snapshot)
        self.variants._session = manager.variants._session

    def _record_patch(self, operation_id, patch_json):
        """保存操作的运行时补丁，并在配置回调时将补丁传给持久化接收方。"""
        self.patches[operation_id] = patch_json
        if self.patch_sink is not None:
            self.patch_sink(operation_id, patch_json)

    def prepare(self):
        """确认所有源目录和部署目录身份，保存校验凭据后采集当前变体状态。"""
        from pathlib import Path

        from src.core.mod_identity import DeploymentLocation
        from src.core.operation_preflight import confirm_deployment, confirm_metadata, confirm_source

        self.receipts = {}
        self.locations = {}
        for mod in self.mods:
            receipt = confirm_source(mod.path, mod.mod_id, mod.content_hash)
            confirm_metadata(receipt)
            self.receipts[mod.mod_id] = receipt
            if mod.deployed_path:
                confirm_deployment(mod.mod_id, mod.deployed_path)
                self.locations[mod.mod_id] = DeploymentLocation(mod.mod_id, Path(mod.deployed_path), mod.content_hash)
        self.variants.collect(trigger=self.trigger, game_id=self.game_id)
        return self

    def before_io(self, plan):
        """校验部署计划及变体前置条件，并在实际移动前注销相关 Mod 的 INI 同步。"""
        from src.core.preset_deployment_plan import validate_plan_filesystem

        validate_plan_filesystem(plan)
        self.variants.on_preset_apply_before_io(plan)
        for action in plan.actions:
            if action.action == "noop":
                continue
            if action.action == "park":
                self.sync.reconcile_mod(action.mod_id)
            self.sync.unregister_mod(action.mod_id)

    def before_commit(self, plan):
        """重新核对源凭据和部署标记，恢复目标预设变体后标记已验证的操作编号。"""
        for receipt in self.receipts.values():
            receipt.recheck()
        for action in plan.actions:
            path = action.disabled_path if action.action == "park" else action.active_path
            marker = read_deployment_identity(path)
            if marker is None or marker["mod_id"] != action.mod_id:
                raise IdentityConflict(tr("提交前部署身份已变更：{path}", path=path))
        self.restore_outcome = self.variants.on_preset_apply_before_commit(plan)
        self.validated_operation_id = plan.operation_id

    def _register(self, action):
        """确认活动部署身份后登记源与部署的 INI 同步，并保存待发布的引用。"""
        marker = read_deployment_identity(action.active_path)
        if marker is None or marker["mod_id"] != action.mod_id:
            raise IdentityConflict(tr("INI 同步的部署身份不符：{active_path}", active_path=action.active_path))
        self.sync.register_mod(
            action.mod_id,
            action.source_path,
            action.active_path,
            source_watch_root=self.source_root,
            deploy_watch_root=self.storage.get_deploy_dir(),
        )
        self.ini_refs[action.mod_id] = (action.mod_id,)

    def after_commit(self, plan):
        """更新快照中的当前预设和部署状态，恢复活动成员同步；后续采集错误仅记日志。"""
        self.current_id = plan.to_preset_id
        actions = {a.mod_id: a for a in plan.actions}
        for mod in self.mods:
            action = actions.get(mod.mod_id)
            if action is not None:
                mod.is_enabled = action.action != "park"
                mod.deployed_path = action.active_path if mod.is_enabled else action.disabled_path
        for action in plan.actions:
            if action.action != "park":
                try:
                    self._register(action)
                except Exception as error:
                    logger.warning("登记 ini 同步失败 (%s): %s", action.mod_id, error)
        try:
            self.variants.on_preset_apply_after_commit(plan)
        except Exception as error:
            logger.warning("预设切换后变体采集失败: %s", error)

    def rollback(self, plan, completed):
        """先撤销变体修改，再回滚已完成的部署动作；恢复同步并在全部成功后释放预留。"""
        from src.core.preset_apply_service import PresetPlanExecutor

        errors = self.variants.on_preset_apply_rollback(plan)
        if not errors:
            errors = PresetPlanExecutor(self.storage).rollback(completed)
        for action in plan.actions:
            if action.action == "park":
                try:
                    self._register(action)
                except Exception as error:
                    errors += (str(error),)
        if not errors:
            self.variants.release_preset_operation(plan)
        return errors

    def publish_ini_refs(self, manager, plan, *, notify_variants=True):
        """在所有者线程发布，快照不持有管理器。"""
        for action in plan.actions:
            if action.action != "noop":
                manager._ini_sync_refs.pop(action.mod_id, None)
        manager._ini_sync_refs.update(self.ini_refs)
        manager.variants._publish_references()
        manager.variants.invalidate()
        outcome = self.restore_outcome
        if notify_variants and outcome is not None:
            manager.variants._notify(outcome.applied, plan.game_id)
            if outcome.missing:
                missing = tuple(outcome.missing)
                names_by_id = {m.mod_id: m.name for m in manager._index.get_mods_by_ids(missing)}
                manager._emit(
                    "variant_references_missing",
                    game_id=plan.game_id,
                    preset_id=plan.to_preset_id,
                    mod_ids=missing,
                    names=tuple(names_by_id.get(mid, mid) for mid in missing),
                )
        if notify_variants and outcome is not None and outcome.needs_reload_hint:
            manager._emit(
                "variant_restored",
                mod_id="",
                game_id=plan.game_id,
                applied=len(outcome.applied),
                removed_keys=outcome.removed_keys,
            )
