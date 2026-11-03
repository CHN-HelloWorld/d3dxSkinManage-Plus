"""独立的变体文件工作，由所有者准入并发布预设。"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

from src.core.deployment_preparation import DeploymentPreparation
from src.core.operation_preflight import confirm_deployment, confirm_source
from src.utils.i18n import t as tr
from src.utils.logger import logger


class VariantTask:
    """冻结变体操作所需上下文，协调后台文件修改与所有者线程的状态发布。"""

    def __init__(self, manager, mod_id, *, command="read", variant_id=None, name="", use_defaults=False):
        """验证命令并采集目标和活动成员快照，修改操作取得排他保护并暂停旧监听上下文。"""
        if command not in {"read", "apply", "save", "update", "rename", "copy", "delete"}:
            raise ValueError(tr("未知的变体操作"))
        self.game_id = manager.current_game_id
        self.mod_id, self.command = mod_id, command
        self.variant_id, self.name, self.use_defaults = variant_id, name, use_defaults
        self.result, self.state, self.variants = None, None, []
        self.mutating = command != "read"
        self._started = False
        self._completed = False
        self._token = ""
        self._protection = manager._library_protection
        if self.mutating and manager.preset_manager.has_pending_apply(self.game_id):
            raise RuntimeError(tr("组合预设正在切换，请等待完成"))
        target = manager.variants._get_mod(mod_id)
        mod_ids = {mod_id}
        if target.is_enabled or self.mutating:
            mod_ids.update(mod.mod_id for mod in manager._index.get_enabled_mods(self.game_id))
        self.preparation = DeploymentPreparation(manager, trigger="variant_ui", mod_ids=mod_ids)
        service = self.preparation.variants
        snapshot = service._manager
        deploy_dir = manager._storage.get_deploy_dir()
        # 预览读取可能跨越游戏切换，必须冻结其部署根。
        self.preparation.storage = snapshot._storage = SimpleNamespace(get_deploy_dir=lambda: deploy_dir)
        self._mods = {mod.mod_id: mod for mod in self.preparation.mods}
        self._full_check = getattr(manager, "_operation_trusted", False) != (
            self.game_id,
            str(manager._index.paths.roots.get(self.game_id, "")),
        )
        snapshot.require_mod_operation = self._check_source
        # 进入工作线程前预留共享配置并保护到发布完成；
        # 嵌套命令只使用该预留，不传递活动管理器或数据库。
        snapshot._library_protection = None
        self._epoch = manager.variants._epoch
        self._watch_before = manager.variants._watch_context
        service._epoch = self._epoch
        service._ini_cache = dict(manager.variants._ini_cache)
        service._default_cache = manager.variants._default_cache.copy()
        self._references_before = {pid: dict(preset.mod_variants) for pid, preset in self.preparation.presets.items()}
        if self.mutating:
            token = "variant-ui:" + uuid.uuid4().hex
            paths = [self.preparation.source_root, deploy_dir, snapshot._preset_manager._presets_file]
            self._protection.protect(token, paths=paths, mod_ids=mod_ids, exclusive=True)
            self._token = token
            manager.variants._watch_context = None

    def _check_source(self, mod_id, *, deployment=False):
        """按任务信任级别确认源凭据，并在要求时核对现有部署身份。"""
        mod = self._mods[mod_id]
        receipt = confirm_source(mod.path, mod.mod_id, mod.content_hash, full=self._full_check)
        if deployment and mod.deployed_path:
            confirm_deployment(mod.mod_id, mod.deployed_path)
        return receipt

    def run(self):
        """按命令执行变体读取或修改，随后刷新列表与状态；修改任务结束时重建监听上下文。"""
        self._started = True
        service = self.preparation.variants
        try:
            mid, vid = self.mod_id, self.variant_id
            if self.command == "apply":
                self.result = service.apply_variant(mid, vid)
            elif self.command in {"save", "update"}:
                self.result = service.save_variant(
                    mid, self.name, variant_id=vid if self.command == "update" else None, use_defaults=self.use_defaults
                )
            elif self.command == "rename":
                self.result = service.rename_variant(mid, vid, self.name)
            elif self.command == "copy":
                self.result = service.copy_variant(mid, vid, self.name)
            elif self.command == "delete":
                self.result = service.delete_variant(mid, vid)
            self._completed = True
            self.variants = service.list_variants(mid)
            self.state = service.get_state(mid)
        finally:
            if self.mutating:
                try:
                    service._refresh_watch_context(self.game_id)
                except (ValueError, OSError, RuntimeError) as error:
                    logger.warning("变体操作后监听上下文刷新失败: %s", error)

    def publish(self, manager):
        """发布工作线程的小型结果，不重新读取变体文件。"""
        preparation = self.preparation
        pm = manager.preset_manager
        if Path(pm._presets_file) == Path(preparation.variants._manager._preset_manager._presets_file):
            for pid, saved in preparation.presets.items():
                if saved.mod_variants == self._references_before[pid]:
                    continue
                live = pm.get_preset(pid)
                if live is not None and live.game_id == self.game_id:
                    live.mod_variants = dict(saved.mod_variants)
                    live.updated_at = saved.updated_at
        original = manager.variants
        if manager.current_game_id == self.game_id and original._epoch == self._epoch:
            original._ini_cache = dict(preparation.variants._ini_cache)
            original._default_cache = preparation.variants._default_cache.copy()
            if self.mutating:
                original._epoch = preparation.variants._epoch
                original._watch_context = preparation.variants._watch_context if self._started else self._watch_before
        if self.mutating and self._completed:
            original._notify((self.mod_id,), self.game_id)

    def release(self):
        """释放本任务持有的排他操作令牌，重复释放不会产生额外操作。"""
        if self._token:
            self._protection.release(self._token)
            self._token = ""
