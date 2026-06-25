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
