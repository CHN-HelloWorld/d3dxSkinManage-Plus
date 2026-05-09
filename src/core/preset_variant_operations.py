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

    def _execute(self, request):
        """启动预设文件阶段，组织运行补丁回传、部署步骤持久化及失败恢复。"""
        state = request.state
        preparation, plan = state["preparation"], state["plan"]

        def run(worker):
            """执行配置和部署计划并核对提交条件；失败时合并回滚错误，结束时清空补丁接收器。"""
            completed = ()
            preparation.patch_sink = lambda op, raw: worker.call_owner("preset_patch", op, raw)
            try:
                if worker.isInterruptionRequested():
                    raise InterruptedError(tr("操作已取消"))
                preparation.before_io(plan)
                completed = PresetPlanExecutor(preparation.storage).execute(
                    plan,
                    should_cancel=worker.isInterruptionRequested,
                    progress=lambda i, total, action, fraction: worker.progress.emit(
                        action.mod_id, (i - 1 + fraction) / max(1, total)
                    ),
                    step_completed=lambda steps: worker.call_owner("preset_steps", plan.operation_id, steps),
                )
                if worker.isInterruptionRequested():
                    raise InterruptedError(tr("操作已取消"))
                preparation.before_commit(plan)
                return completed
            except Exception as error:
                try:
                    errors = preparation.rollback(plan, completed)
                except Exception as rollback_error:
                    errors = (str(rollback_error),)
                errors += tuple(getattr(error, "rollback_errors", ()))
                raise PresetApplyError(
                    str(error), completed=getattr(error, "completed", completed), rollback_errors=errors
                ) from error
            finally:
                preparation.patch_sink = None

        self._worker(request, "preset_execute", run)

    def settle(self, request, worker):
        """结算预设或变体阶段异常，按是否已提交决定成功附带错误、回滚或失败通知。"""
        try:
            self._settle(request, worker)
        except Exception as error:
            logger.exception("插件预设或变体提交失败")
            if request.state.get("committed"):
                # 预设已经提交后，不因后续通知失败而回滚。
                self.service._complete(request, succeeded=request.state.get("affected", ()), error=str(error))
            elif request.state.get("plan") is not None:
                if request.kind == "preset_execute" and worker.exception is None:
                    self._rollback(request, worker.result or (), error)
                else:
                    self._fail_apply(request, error)
            else:
                self._report_failure(error)
                self.service._complete(request, failed=request.ids, error=str(error))

    def _settle(self, request, worker):
        """按请求阶段推进后台准备、文件执行、所有者提交及提交后清理。

        提交前取消进入失败或回滚路径；提交后的同步和删除清理由必须完成的后台阶段处理。"""
        state, pm = request.state, self.manager.preset_manager
        cancelled = request.task._cancel_requested or self.service._stopping
        if request.kind == "preset_metadata_stage":
            candidate = state["metadata_candidate"]
            if worker.exception is not None or cancelled:
                self.service._complete(request, error=worker.error or tr("操作已取消"))
                return
            preset_id = pm.commit_preset_metadata(
                candidate, copied_from=request.args["preset_id"] if request.operation == "preset_duplicate" else ""
            )
            state["committed"] = True
            self.service._complete(request, preset_id=preset_id)
            return
        if request.kind == "variant":
            task = state["variant_task"]
            task.publish(self.manager)
            # 修改可能已提交而结果读取失败；后续取消不能改变成功的提交凭据。
            if worker.exception is not None and not task._completed:
                self._report_failure(worker.exception)
                self.service._complete(
                    request,
                    failed=() if cancelled else request.ids,
                    cancelled=request.ids if cancelled else (),
                    error=worker.error,
                )
            else:
                self.service._complete(
                    request,
                    succeeded=request.ids,
                    error=worker.error,
                    variant_id=getattr(task.result, "variant_id", task.variant_id),
                )
            return
        if request.kind == "preset_prepare":
            if worker.exception is not None or cancelled:
                self._fail_apply(request, worker.exception or InterruptedError(tr("操作已取消")))
                return
            if self.manager.current_game_id != request.game_id:
                raise ValueError(tr("游戏已切换，请重新发起操作"))
            plan = pm.prepare_preset_apply(request.args["preset_id"], preparation=state["preparation"])
            state["plan"] = plan
            pm.mark_preset_apply_executing(plan.operation_id)
            self._execute(request)
            return
        if request.kind == "preset_execute":
            if worker.exception is not None:
                self._fail_apply(request, worker.exception)
                return
            completed = worker.result or ()
            if cancelled:
                self._rollback(request, completed, InterruptedError(tr("操作已取消")))
                return
            state["affected"] = pm.commit_preset_apply(state["plan"], completed, preparation=state["preparation"])
            state["committed"] = True
            preparation, plan = state["preparation"], state["plan"]
            self._worker(request, "preset_publish", lambda worker: preparation.after_commit(plan), must_finish=True)
            return
        if request.kind == "preset_publish":
            if worker.error:
                logger.warning("预设提交后同步失败: %s", worker.error)
            state["preparation"].publish_ini_refs(self.manager, state["plan"])
            pm.finish_prepared_apply(state["plan"], state["affected"], state["preparation"])
            self.service._complete(request, succeeded=state["affected"], error=worker.error)
            return
        if request.kind == "preset_rollback":
            errors = (worker.error,) if worker.error else tuple(worker.result or ())
            error = PresetApplyError(str(state["failure"]), completed=state["completed"], rollback_errors=errors)
            self._fail_apply(request, error)
            return
        if request.kind == "preset_delete_stage":
            candidate = state["delete_candidate"]
            if worker.exception is not None or cancelled:
                self.service._complete(request, error=worker.error or tr("操作已取消"))
                return
            pm.commit_delete_presets(candidate)
            state["committed"] = True
            from src.core.preset_delete import clean_unused_preset_deployments

            storage, root = self.manager._storage, self.manager._storage.get_deploy_dir()
            self._worker(
                request,
                "preset_delete_cleanup",
                lambda worker: clean_unused_preset_deployments(candidate.cleanup_records, root, storage),
                must_finish=True,
            )
            return
        if request.kind == "preset_delete_cleanup":
            if worker.exception is not None:
                raise worker.exception
            removed, errors = worker.result
            pm.publish_deleted_preset_deployments(removed, request.game_id)
            self.service._complete(request, error=tr("部分未使用的部署副本清理失败") if errors else "")

    def _rollback(self, request, completed, error):
        """保存原始失败与已完成步骤，并启动不可取消的部署恢复阶段。"""
        state = request.state
        state.update(completed=completed, failure=error)
        preparation, plan = state["preparation"], state["plan"]
        self._worker(request, "preset_rollback", lambda worker: preparation.rollback(plan, completed), must_finish=True)

    def _report_failure(self, error):
        """沿异常因果链寻找操作前置检查错误，并转交宿主统一报告。"""
        from src.core.operation_preflight import OperationPreflightError

        cause = error
        while cause is not None and not isinstance(cause, OperationPreflightError):
            cause = cause.__cause__
        if isinstance(cause, OperationPreflightError):
            self.manager.report_operation_failure(cause)

    def _fail_apply(self, request, error):
        """完成预设失败记录与同步引用恢复，合并回滚问题后发布取消或失败结果。"""
        state = request.state
        details = str(error)
        try:
            if state.get("plan") is not None:
                self.manager.preset_manager.fail_preset_apply(state["plan"], error, preparation=state["preparation"])
                state["preparation"].publish_ini_refs(self.manager, state["plan"], notify_variants=False)
        except Exception as failure:
            details += "\n" + str(failure)
        rollback_errors = getattr(error, "rollback_errors", ())
        if rollback_errors:
            details += "\n" + "; ".join(rollback_errors)
        self._report_failure(error)
        cancelled = request.task._cancel_requested and not rollback_errors
        self.service._complete(
            request, failed=() if cancelled else request.ids, cancelled=request.ids if cancelled else (), error=details
        )
