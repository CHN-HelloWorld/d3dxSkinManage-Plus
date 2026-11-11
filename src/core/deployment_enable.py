"""独立准备启用配置，数据库与界面发布由所有者线程完成。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from src.core.deployment_preparation import DeploymentPreparation
from src.core.mod_package import overrides_seeded, seed_ini_overrides
from src.core.mod_variant_manager import ModVariantManager
from src.core.operation_preflight import confirm_deployment, confirm_metadata, confirm_source
from src.core.variant_operation import FileChange, VariantOperation, read_bytes
from src.utils.i18n import t as tr


class _EnableVariants(ModVariantManager):
    """在变体文件事务成功执行后保留操作记录，供启用失败时逆序恢复。"""

    def _execute_files(self, *args, **kwargs):
        """执行父类文件事务并保存返回的操作对象，使启用过程能够回滚。"""
        operation = super()._execute_files(*args, **kwargs)
        self.enable_operations.append(operation)
        return operation


class EnablePreparation:
    """在之前的启用操作发布后采集快照，再由工作线程处理配置文件。"""

    def __init__(self, manager, mod_id, deploy_path, *, stage_only=False, source_receipt=None):
        """采集目标 Mod 及已启用成员的独立快照，绑定部署路径、源凭据和变体版本。"""
        self.game_id = manager.current_game_id
        self.mod_id = mod_id
        self.deploy_path = str(deploy_path)
        self.stage_only = stage_only
        self.source_receipt = source_receipt
        self.completed = False
        self.published = False
        self.outcome = None
        self.records = ()
        self.epoch = manager.variants._epoch
        ids = {mod_id, *(m.mod_id for m in manager._index.get_enabled_mods(self.game_id))}
        self.snapshot = DeploymentPreparation(manager, trigger="mod_enable", mod_ids=ids)
        self.mod = next(m for m in self.snapshot.mods if m.mod_id == mod_id)
        service = _EnableVariants(self.snapshot.variants._manager)
        service._session = manager.variants._session
        service.enable_operations = []
        # 宿主串行发布配置，部署预留在所有者发布期间继续保护当前 Mod。
        service._manager._library_protection = None
        service._manager.require_mod_operation = self._check_source
        self.variants = service

    def _check_source(self, mod_id, *, deployment=False):
        """确认请求只涉及当前 Mod，重新核验源凭据，并按需验证部署身份。"""
        if mod_id != self.mod_id:
            raise ValueError(tr("启用请求包含其他 Mod"))
        self.source_receipt.recheck()
        if deployment:
            confirm_deployment(mod_id, self.deploy_path)
        return self.source_receipt

    def run(self):
        """验证源与部署身份，恢复停放目录并准备变体和同步配置。

        成功后保存待发布的预设记录；配置准备失败时回滚已执行的变体文件事务并重新抛出异常。"""
        mod, storage = self.mod, self.snapshot.storage
        self.source_receipt = self.source_receipt or confirm_source(mod.path, mod.mod_id, mod.content_hash)
        self.source_receipt.recheck()
        confirm_metadata(self.source_receipt)
        confirm_deployment(mod.mod_id, self.deploy_path)
        if not overrides_seeded(mod.path) and not seed_ini_overrides(mod.path):
            raise OSError(tr("无法读取完整的 Mod 配置，请检查压缩包和文件访问权限后重试。"))
        storage.prepare_nested_deployment(self.deploy_path, mod.mod_id)
        if self.stage_only:
            name = storage.active_name_from_parked(self.deploy_path, mod.mod_id)
            restored = storage.restore_parked_deployment(self.deploy_path, name)
            if restored is None:
                raise OSError(tr("无法启用已准备好的部署文件"))
            self.deploy_path = str(restored)
        mod.deployed_path, mod.is_enabled = self.deploy_path, True
        try:
            self.snapshot.sync.register_mod(
                mod.mod_id,
                mod.path,
                self.deploy_path,
                source_watch_root=self.snapshot.source_root,
                deploy_watch_root=storage.get_deploy_dir(),
            )
            self.outcome = self.variants.restore([mod.mod_id], include_members=True)
            self.source_receipt.recheck()
            self.variants._refresh_watch_context(self.game_id)
            raw = read_bytes(self.variants._manager._preset_manager._presets_file)
            self.records = tuple(json.loads(raw.decode("utf-8-sig")).get("presets", ())) if raw else ()
            self.completed = True
        except Exception:
            self.rollback()
            raise
        return self

    def validate(self, manager, mod_id, deploy_path):
        """核对完成状态、游戏、Mod、部署路径和预设版本，拒绝重复或过期发布。"""
        if (
            not self.completed
            or self.published
            or self.game_id != manager.current_game_id
            or mod_id != self.mod_id
            or Path(deploy_path) != Path(self.deploy_path)
            or self.epoch != manager.variants._epoch
            or self.snapshot.current_id != manager.preset_manager.get_current(self.game_id).id
        ):
            raise RuntimeError(tr("启用准备结果已经过期，请重试"))

    def publish(self, manager, mod):
        """在所有者线程中同时提交部署记录和内存引用。"""
        pm = manager.preset_manager
        before = [
            (
                preset,
                copy.deepcopy(preset.character_mods),
                dict(preset.mod_variants),
                preset.updated_at,
                copy.deepcopy(pm._durable_refs.get(preset.id)),
                copy.deepcopy(pm._resolved_refs.get(preset.id)),
            )
            for preset in pm.get_all_presets(self.game_id)
        ]
        old_refs = manager._ini_sync_refs.get(self.mod_id)
        old_context = manager.variants._prepared_enable_context
        connection = manager._index._db()
        try:
            with manager._index.transaction():
                manager._persist_mod_deployment_state(mod, commit=False)
                for record in self.records:
                    preset = pm.get_preset(record["id"])
                    if preset is None or preset.game_id != self.game_id:
                        continue
                    preset.mod_variants = dict(record.get("mod_variants", {}))
                    preset.updated_at = record.get("updated_at", preset.updated_at)
                    pm._durable_refs[preset.id] = copy.deepcopy(record["character_mods"])
                pm.refresh_references(self.game_id, (self.mod_id,))
                manager._ini_sync_refs[self.mod_id] = (self.mod_id,)
                manager.variants._prepared_enable_context = (self.mod_id, self.variants._watch_context)
        except Exception:
            # SQLite 提交失败后事务可能仍未关闭；工作线程移除或停放文件前，
            # 先恢复数据库和内存中的状态。
            try:
                if connection.in_transaction:
                    connection.rollback()
            finally:
                for preset, members, variants, updated, durable, resolved in before:
                    preset.character_mods = members
                    preset.mod_variants = variants
                    preset.updated_at = updated
                    for mapping, value in ((pm._durable_refs, durable), (pm._resolved_refs, resolved)):
                        if value is None:
                            mapping.pop(preset.id, None)
                        else:
                            mapping[preset.id] = value
                if old_refs is None:
                    manager._ini_sync_refs.pop(self.mod_id, None)
                else:
                    manager._ini_sync_refs[self.mod_id] = old_refs
                manager.variants._prepared_enable_context = old_context
            raise
        self.published = True
        if self.outcome.skipped:
            manager._emit("variant_restore_skipped", mod_id=self.mod_id, game_id=self.game_id)
        if self.outcome.needs_reload_hint:
            manager._emit(
                "variant_restored",
                mod_id=self.mod_id,
                game_id=self.game_id,
                applied=len(self.outcome.applied),
                removed_keys=self.outcome.removed_keys,
            )

    def rollback(self):
        """通过比较原值撤销尚未发布的偏好写入，保留外部修改。"""
        if self.published:
            return ()
        errors = []
        for original in reversed(self.variants.enable_operations):
            changes = [FileChange(c.path, c.after, c.before) for c in original.changes if c.before != c.after]
            if not changes:
                continue
            operation = VariantOperation(original.journal_path.parent, self.game_id, (self.mod_id,), changes)
            try:
                operation.prepare()
                operation.apply()
                operation.commit()
            except (OSError, ValueError, RuntimeError) as error:
                errors.append(str(error))
        self.variants.enable_operations.clear()
        self.snapshot.sync.unregister_mod(self.mod_id)
        return tuple(errors)
