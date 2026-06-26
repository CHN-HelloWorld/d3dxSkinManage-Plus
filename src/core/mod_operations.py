"""串行执行插件写操作，共用宿主的部署原语。

工作线程只执行文件读写；规划、数据库、预设、事件和完成回调在创建服务的界面线程处理。
"""

from __future__ import annotations

import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from src.core.config import Config
from src.plugin_sdk.operations import OperationResult, OperationTask
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.qt_timers import call_later


class _IOWorker(QThread):
    """执行插件操作的后台文件任务，并通过信号请求所有者线程处理持久状态。"""

    progress = Signal(str, float)
    owner_call = Signal(object)

    def __init__(self, run_io, parent):
        """绑定文件任务并初始化结果、错误、所有者调用和取消控制状态。"""
        super().__init__(parent)
        self._run_io = run_io
        self.result = None
        self.error = ""
        self.exception = None
        self.pending_call = None
        self.must_finish = False
        self.cancelled_on_start = False

    def call_owner(self, operation, *args):
        """发送需在所有者线程执行的操作并等待确认，将所有者异常传回工作线程。"""
        from types import SimpleNamespace

        call = SimpleNamespace(operation=operation, args=args, done=threading.Event(), error=None)
        self.pending_call = call
        self.owner_call.emit(call)
        call.done.wait()
        self.pending_call = None
        if call.error is not None:
            raise call.error

    def run(self):
        """执行后台文件任务，启动前取消及运行异常均保存为任务失败信息。"""
        try:
            if self.cancelled_on_start:
                raise InterruptedError(tr("操作已取消"))
            self.result = self._run_io(self)
        except Exception as error:
            self.exception = error
            self.error = str(error)
            logger.exception("插件后台文件操作失败")


@dataclass
class _Request:
    """保存插件操作的所有者、游戏、参数、成员和跨阶段工作状态。"""

    owner: str
    operation: str
    game_id: str
    task: OperationTask
    args: dict
    ids: tuple[str, ...] = ()
    worker: _IOWorker | None = None
    kind: str = ""
    entries: list = field(default_factory=list)
    state: dict = field(default_factory=dict)


class ModOperationService(QObject):
    """在宿主线程串行调度插件写操作，协调后台文件任务和所有者状态提交。"""

    activity_changed = Signal(bool)
    started = Signal(str, object)
    finished = Signal(object)
    progress = Signal(str, float)

    def __init__(self, manager, parent=None, *, host_busy=None, error_sink=None):
        """绑定管理器、忙碌检查和错误接收器，初始化串行队列及预设变体执行服务。"""
        super().__init__(parent)
        self._m = manager
        self._host_busy = host_busy or (lambda: False)
        self._error_sink = error_sink
        self._thread_id = threading.get_ident()
        self._queue = deque()
        self._active = None
        self._stopping = False
        self._owned_tasks = {}
        from src.core.preset_variant_operations import PresetVariantOperations

        self._preset_variants = PresetVariantOperations(self, manager)

    @property
    def busy(self):
        """判断服务是否有正在执行或等待调度的请求。"""
        return self._active is not None or bool(self._queue)

    @property
    def game_id(self):
        """返回当前活动或首个排队请求所属游戏，无请求时返回空字符串。"""
        request = self._active or (self._queue[0] if self._queue else None)
        return request.game_id if request else ""

    def _assert_thread(self):
        """要求调用发生在服务创建线程，跨线程请求直接抛出运行错误。"""
        if threading.get_ident() != self._thread_id:
            raise RuntimeError("Mod operations must be requested on the host thread")

    def submit(self, owner: str, operation: str, **args) -> OperationTask:
        """创建可取消任务并加入串行队列；宿主退出期间立即返回取消结果。"""
        self._assert_thread()
        task = OperationTask(error_sink=(lambda error: self._error_sink(owner, error)) if self._error_sink else None)
        ids = (args["mod_id"],) if args.get("mod_id") else ()
        request = _Request(owner, operation, self._m.current_game_id, task, args, ids)
        self._owned_tasks.setdefault(owner, weakref.WeakSet()).add(task)
        task._cancel = lambda: self._cancel_request(request)
        if self._stopping:
            task._finish(OperationResult(request.game_id, cancelled_ids=ids, error=tr("宿主正在退出")))
            return task
        was_busy = self.busy
        self._queue.append(request)
        if not was_busy:
            self.activity_changed.emit(True)
        call_later(0, self, self._drain)
        return task

    def _cancel_request(self, request):
        """向可中断的活动工作线程发送取消请求，必须完成的收尾任务继续运行。"""
        self._assert_thread()
        if request.worker is not None and not request.worker.must_finish:
            request.worker.requestInterruption()

    def cancel_owner(self, owner: str):
        """分离指定插件的任务回调，并取消其排队和活动请求。"""
        self._assert_thread()
        for task in self._owned_tasks.pop(owner, ()):
            task._detach()
        for request in list(self._queue) + ([self._active] if self._active else []):
            if request.owner == owner:
                request.task._detach()
                request.task.cancel()

    def cancel_all(self):
        """向全部排队和活动请求发出取消请求。"""
        self._assert_thread()
        for request in list(self._queue) + ([self._active] if self._active else []):
            request.task.cancel()

    def _complete(self, request, *, succeeded=(), failed=(), cancelled=(), error="", preset_id=None, variant_id=None):
        """释放请求预留并发布结果，先刷新宿主信号再完成插件回调，随后继续调度。"""
        self._preset_variants.release(request)
        result = OperationResult(
            request.game_id,
            tuple(succeeded),
            tuple(failed),
            tuple(cancelled),
            error,
            preset_id if preset_id is not None else request.args.get("preset_id", ""),
            variant_id if variant_id is not None else request.args.get("variant_id"),
        )
        if self._active is request:
            self._active = None
        # 插件回调可能打开嵌套事件循环并完成其他请求，须先刷新宿主反馈，避免旧提示覆盖新任务。
        self.finished.emit(result)
        request.task._finish(result)
        if not self.busy:
            self.activity_changed.emit(False)
        call_later(0, self, self._drain)

    def _drain(self):
        """取出下一个请求并检查取消、游戏与宿主占用状态，启动失败时发布失败结果。"""
        if self._active is not None or not self._queue:
            return
        request = self._queue.popleft()
        self._active = request
        if request.task._cancel_requested or self._stopping:
            self._complete(request, cancelled=request.ids, error=tr("操作已取消"))
            return
        try:
            if not request.game_id or request.game_id != self._m.current_game_id:
                raise ValueError(tr("游戏已切换，请重新发起操作"))
            if self._host_busy() or self._m.preset_manager.has_pending_apply(request.game_id):
                raise ValueError(tr("宿主正在处理其他 Mod 任务，请稍后重试"))
            self._start(request)
        except Exception as error:
            logger.warning("插件操作未完成: %s", error)
            from src.core.operation_preflight import OperationPreflightError

            if isinstance(error, OperationPreflightError):
                self._m.report_operation_failure(error)
            self._complete(request, failed=request.ids, error=str(error))
