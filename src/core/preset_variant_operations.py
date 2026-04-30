"""在串行操作队列中处理预设与变体请求。

活动管理器只由所有者线程访问；工作线程使用独立准备结果、计划和文件原语。
日志写入与所有者协作，关闭期间无需处理额外界面事件也可完成。
"""

from __future__ import annotations

from src.core.preset_apply_service import PresetApplyError, PresetPlanExecutor
from src.utils.i18n import t as tr
from src.utils.logger import logger


class PresetVariantOperations:
    """将插件预设与变体请求拆分为后台准备、所有者提交和必要的恢复阶段。"""

    def __init__(self, service, manager):
        """绑定串行操作服务与宿主 Mod 管理器。"""
        self.service, self.manager = service, manager

    def release(self, request):
        """释放变体任务和预设保护，清理尚未提交的元数据或删除暂存候选。"""
        state = request.state
        task = state.pop("variant_task", None)
        if task is not None:
            task.release()
        if state.pop("protected", False):
            self.manager.release_library_operation("preset-apply")
        token = state.pop("metadata_token", "")
        if token:
            self.manager.release_library_operation(token)
        candidate = state.get("metadata_candidate") or state.get("delete_candidate")
        if candidate is not None and not candidate.committed:
            from src.core.preset_delete import discard_prepared_preset_delete

            try:
                discard_prepared_preset_delete(candidate)
            except OSError as error:
                # 工作线程结束后，临时文件占用不能继续阻塞操作或保留库保护。
                logger.warning("预设暂存文件清理失败 (%s): %s", candidate.temporary, error)

    def start(self, request):
        """验证预设归属并分派创建、复制、编辑、应用或删除操作，元数据操作先获取排他保护。"""
        if request.operation.startswith("variant_"):
            self._start_variant(request)
            return
        pm, args = self.manager.preset_manager, request.args
        operation = request.operation
        preset = None
        if operation != "preset_create":
            preset = pm.get_preset(args.get("preset_id"))
            if preset is None or preset.game_id != request.game_id:
                raise ValueError(tr("预设不存在或不属于当前游戏"))
        self.service.started.emit(operation, request.ids)
        if self.service._cancel_before_mutation(request):
            return
        if operation != "preset_apply":
            import uuid

            token = "plugin-preset:" + uuid.uuid4().hex
            self.manager.protect_library_operation(token, paths=(pm._presets_file,), exclusive=True)
            request.state["metadata_token"] = token
        if operation in {"preset_create", "preset_duplicate", "preset_rename", "preset_update_description"}:
            candidate = pm.prepare_preset_metadata(request.game_id, operation.removeprefix("preset_"), **args)
            request.state["metadata_candidate"] = candidate
            from src.core.preset_delete import write_prepared_preset_delete

            self._worker(request, "preset_metadata_stage", lambda worker: write_prepared_preset_delete(candidate))
        elif operation == "preset_apply":
            self._start_apply(request, preset)
        elif operation == "preset_delete":
            candidate = pm.prepare_delete_presets((preset.id,), request.game_id)
            request.state["delete_candidate"] = candidate
            from src.core.preset_delete import write_prepared_preset_delete

            self._worker(request, "preset_delete_stage", lambda worker: write_prepared_preset_delete(candidate))
        else:
            raise ValueError(tr("未知操作: {operation}", operation=operation))

    def _start_variant(self, request):
        """验证 Mod 游戏归属并准备独立变体任务，随后交由后台执行。"""
        args = dict(request.args)
        mod_id = args.pop("mod_id")
        if self.manager._index.get_mod_game_id(mod_id) != request.game_id:
            raise ValueError(tr("Mod 不存在或不属于当前游戏"))
        command = request.operation.removeprefix("variant_")
        command = "copy" if command == "duplicate" else command
        self.service.started.emit(request.operation, request.ids)
        if self.service._cancel_before_mutation(request):
            return
        task = self.manager.variants.prepare_task(mod_id, command=command, **args)
        request.state["variant_task"] = task

        def run(worker):
            """启动前检查取消，更新变体时先保留现有名称，再运行已准备的任务。"""
            if worker.isInterruptionRequested():
                raise InterruptedError(tr("操作已取消"))
            if command == "update":
                # 保留已有名称，不在界面线程读取变体文件。
                variants = task.preparation.variants.list_variants(mod_id)
                task.name = next((v.name for v in variants if v.variant_id == task.variant_id), "")
            task.run()

        self._worker(request, "variant", run)

    def _worker(self, request, kind, run, *, must_finish=False):
        """记录当前请求阶段并启动相应后台任务，可指定收尾阶段必须完成。"""
        request.kind = kind
        self.service._start_worker(request, run, must_finish=must_finish, check_cancel=False)

    def _start_apply(self, request, preset):
        """采集前后预设及活动成员的部署快照，建立独立存储服务并保护操作范围。"""
        manager = self.manager
        current = manager.preset_manager.get_current(request.game_id)
        ids = set(preset.enabled_mod_ids) | set(current.enabled_mod_ids)
        ids.update(m.mod_id for m in manager._index.get_enabled_mods(request.game_id))
        preparation = manager.create_deployment_preparation(trigger="preset_apply_before_io", mod_ids=ids)
        # 工作期间切换游戏可能替换部署根，当前请求仍使用冻结的目标和命名空间。
        from src.core.mod_storage import ModStorage

        storage = ModStorage()
        storage._migoto_mods_dir = manager._storage.get_deploy_dir()
        preparation.storage = preparation.variants._manager._storage = storage
        request.ids = tuple(sorted(ids))
        request.state["preparation"] = preparation
        manager.protect_library_operation(
            "preset-apply",
            exclusive=True,
            mod_ids=tuple(m.mod_id for m in preparation.mods),
            paths=tuple(p for m in preparation.mods for p in (m.path, m.deployed_path)),
        )
        request.state["protected"] = True
        self._worker(request, "preset_prepare", lambda worker: preparation.prepare())
