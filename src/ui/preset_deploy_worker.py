"""仅执行预设文件操作的 Qt 工作线程。"""

from __future__ import annotations

from threading import Event
from types import SimpleNamespace

from PySide6.QtCore import QThread, Signal

from src.core.preset_apply_service import PresetPlanExecutor
from src.utils.i18n import t as tr


class DeploymentPhaseWorker(QThread):
    """结束信号到达后才消费结果，不使用提前发出的结果信号。"""

    def __init__(self, work, parent=None):
        """保存部署阶段回调，初始化供所属线程读取的结果和异常。"""
        super().__init__(parent)
        self._work = work
        self.result = None
        self.error = None

    def run(self):
        """执行尚未取消的阶段回调，将返回值或异常保存在工作线程对象中。"""
        try:
            if self.isInterruptionRequested():
                raise InterruptedError("operation cancelled")
            self.result = self._work()
        except Exception as error:
            self.error = error


class PresetCleanupWorker(DeploymentPhaseWorker):
    """在后台清理不再使用的预设部署，并通过进度信号通知界面。"""

    progress = Signal(int, int, str)

    def __init__(self, records, root, storage, parent=None):
        """将预设记录、部署根和存储封装为独立清理任务，转发清理进度。"""
        from src.core.preset_delete import clean_unused_preset_deployments

        super().__init__(lambda: clean_unused_preset_deployments(records, root, storage, self.progress.emit), parent)


class PresetDeployWorker(QThread):
    """执行预设部署计划，提供阶段进度和逐项完成信号，并在失败时协调回滚。"""

    progress = Signal(int, int, object, float)
    step_completed = Signal(object)
    succeeded = Signal(object)
    failed = Signal(object)
    patch_ready = Signal(object)
    phase = Signal(str)

    def __init__(self, plan, storage, parent=None, *, preparation=None) -> None:
        """保存不可变部署计划与准备对象，初始化已完成步骤、补丁请求和进度限频状态。"""
        super().__init__(parent)
        self._plan = plan
        self._storage = storage
        self.preparation = preparation
        self.completed = ()
        self.error = None
        self._last_progress = 0.0
        self.pending_patch = None

    def persist_patch_on_owner(self, operation_id, patch_json):
        """请求所属线程持久化事务补丁并等待确认，持久化错误重新抛给部署流程。"""
        request = SimpleNamespace(operation_id=operation_id, patch_json=patch_json, done=Event(), error=None)
        self.pending_patch = request
        self.patch_ready.emit(request)
        request.done.wait()
        self.pending_patch = None
        if request.error is not None:
            raise request.error

    def _report_progress(self, index, total, action, fraction):
        """直接报告步骤起止进度，并将中间进度信号限制为至多每五十毫秒一次。"""
        import time

        now = time.monotonic()
        if fraction in (0.0, 1.0) or now - self._last_progress >= 0.05:
            self._last_progress = now
            self.progress.emit(index, total, action, float(fraction))

    def run(self) -> None:
        """在后台核验并执行部署计划，再完成提交前验证；取消或异常时回滚准备结果。

        失败信号携带已完成步骤和回滚错误，成功信号携带已执行步骤，数据库发布由所属线程处理。"""
        try:
            if self.isInterruptionRequested():
                raise InterruptedError("preset apply cancelled")
            if self.preparation is not None:
                self.phase.emit(tr("正在校验部署计划…"))
                self.preparation.before_io(self._plan)
            completed = PresetPlanExecutor(self._storage).execute(
                self._plan,
                should_cancel=self.isInterruptionRequested,
                progress=self._report_progress,
                step_completed=self.step_completed.emit,
            )
            self.completed = completed
            if self.isInterruptionRequested():
                raise InterruptedError("preset apply cancelled")
            if self.preparation is not None:
                self.phase.emit(tr("正在验证部署并写回变体…"))
                self.preparation.before_commit(self._plan)
        except Exception as exc:
            from src.core.preset_apply_service import PresetApplyError

            if self.preparation is not None:
                try:
                    errors = self.preparation.rollback(self._plan, self.completed)
                except Exception as rollback_error:
                    errors = (str(rollback_error),)
                errors += tuple(getattr(exc, "rollback_errors", ()))
                exc = PresetApplyError(
                    str(exc), completed=getattr(exc, "completed", self.completed), rollback_errors=errors
                )
            self.error = exc
            self.failed.emit(self.error)
            return
        self.succeeded.emit(completed)
