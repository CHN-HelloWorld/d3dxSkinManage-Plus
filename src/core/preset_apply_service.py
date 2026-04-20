"""预设计划的文件执行与回滚。"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from src.core.mod_identity import IdentityConflict, read_deployment_identity
from src.core.mod_storage import ModStorage
from src.core.preset_deployment_plan import DeploymentAction, PresetDeploymentPlan
from src.utils.logger import logger


@dataclass(frozen=True)
class CompletedStep:
    """记录已完成部署动作的 Mod 身份和源目标路径，用于进度持久化与回滚。"""

    mod_id: str
    action: str
    source_path: str
    target_path: str


class PresetApplyError(RuntimeError):
    """携带已完成步骤及回滚错误的预设文件操作异常。"""

    def __init__(
        self, message: str, *, completed: tuple[CompletedStep, ...], rollback_errors: tuple[str, ...] = ()
    ) -> None:
        """保存原始失败说明、已完成动作和尚未恢复的错误信息。"""
        super().__init__(message)
        self.completed = completed
        self.rollback_errors = rollback_errors


class PresetPlanExecutor:
    """仅执行文件操作，可完全运行于工作线程。"""

    def __init__(self, storage: ModStorage) -> None:
        """绑定部署存储服务，供计划执行和反向恢复调用。"""
        self.storage = storage

    def execute(
        self,
        plan: PresetDeploymentPlan,
        *,
        should_cancel=None,
        progress: Callable[[int, int, DeploymentAction, float], None] | None = None,
        step_completed: Callable[[tuple[CompletedStep, ...]], None] | None = None,
    ) -> tuple[CompletedStep, ...]:
        """按计划执行停放、恢复或物化部署，并回调进度与已完成步骤。

        执行前后核对源身份，逐项响应取消；任一失败会逆序回滚并抛出包含恢复结果的预设应用异常。"""
        completed: list[CompletedStep] = []
        physical = [a for a in plan.actions if a.action != "noop"]
        total = len(physical)
        try:
            from src.core.operation_preflight import confirm_source

            receipts = {a.mod_id: confirm_source(a.source_path, a.mod_id) for a in plan.actions}
            for index, action in enumerate(physical, 1):
                if should_cancel is not None and should_cancel():
                    raise InterruptedError("preset apply cancelled")
                if progress:
                    progress(index, total, action, 0.0)
                receipts[action.mod_id].recheck()
                if action.action in ("park", "restore"):
                    owned = action.active_path if action.action == "park" else action.disabled_path
                    self._check_identity(owned, action.mod_id)
                if action.action == "park":
                    result = self.storage.park_deployment(action.active_path, action.mod_id)
                    if result is None or self._key(result) != self._key(action.disabled_path):
                        raise OSError(f"park failed: {action.active_path} -> {action.disabled_path}")
                    step = CompletedStep(action.mod_id, "park", action.active_path, action.disabled_path)
                elif action.action == "restore":
                    result = self.storage.restore_parked_deployment(action.disabled_path, Path(action.active_path).name)
                    if result is None or self._key(result) != self._key(action.active_path):
                        raise OSError(f"restore failed: {action.disabled_path} -> {action.active_path}")
                    step = CompletedStep(action.mod_id, "restore", action.disabled_path, action.active_path)
                else:
                    result = self.storage.materialize_deployment(
                        action.source_path,
                        action.mod_id,
                        Path(action.active_path).name,
                        active=True,
                        should_cancel=should_cancel,
                        progress_cb=(lambda frac, i=index, a=action: progress(i, total, a, frac)) if progress else None,
                    )
                    step = CompletedStep(action.mod_id, "materialize_active", action.partial_path, str(result))
                completed.append(step)
                if action.action == "materialize_active" and self._key(step.target_path) != self._key(
                    action.active_path
                ):
                    raise IdentityConflict("deployment target changed while applying preset")
                if step_completed:
                    step_completed(tuple(completed))
                if progress:
                    progress(index, total, action, 1.0)
            for receipt in receipts.values():
                receipt.recheck()
            return tuple(completed)
        except Exception as exc:
            failed = action if "action" in locals() else None
            logger.error(
                "预设文件操作失败 game=%s op=%s mod=%s path=%s: %s",
                plan.game_id,
                plan.operation_id,
                failed.mod_id if failed else "",
                failed.active_path if failed else "",
                exc,
            )
            rollback_errors = self.rollback(tuple(completed))
            raise PresetApplyError(
                str(exc),
                completed=tuple(completed),
                rollback_errors=rollback_errors,
            ) from exc

    def rollback(self, completed: tuple[CompletedStep, ...]) -> tuple[str, ...]:
        """逆序核对已完成动作的部署身份，删除新部署或恢复原位置，并汇总每项错误。"""
        errors: list[str] = []
        for step in reversed(completed):
            try:
                source = Path(step.source_path)
                target = Path(step.target_path)
                self._check_identity(target, step.mod_id)
                if step.action == "materialize_active":
                    if not self.storage.remove_deploy_path(target):
                        raise OSError(f"cannot remove new deployment: {target}")
                else:
                    if self.storage._deploy_path_exists(source):
                        raise FileExistsError(f"rollback target exists: {source}")
                    if not self.storage._deploy_path_exists(target):
                        raise FileNotFoundError(str(target))
                    os.replace(str(target), str(source))
            except Exception as exc:
                logger.error(
                    "部署回滚失败 mod=%s action=%s path=%s: %s", step.mod_id, step.action, step.target_path, exc
                )
                errors.append(f"{step.mod_id}:{step.action}: {exc}")
        return tuple(errors)

    @staticmethod
    def _check_identity(path, mod_id):
        """要求目录标记与目标 Mod 身份一致，否则抛出身份冲突异常。"""
        marker = read_deployment_identity(path)
        if marker is None or marker["mod_id"] != mod_id:
            raise IdentityConflict(f"deployment identity mismatch: {path}")

    @staticmethod
    def _key(path: str | Path) -> str:
        """将部署路径转换为按平台规范大小写的绝对比较键。"""
        return os.path.normcase(os.path.abspath(str(path)))
