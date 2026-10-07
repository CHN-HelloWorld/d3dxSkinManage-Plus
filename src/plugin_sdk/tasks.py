"""不依赖 Qt 的后台任务接口，工作线程只持有取消和进度上下文。"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, get_ident
from typing import Generic, TypeVar

from src.plugin_sdk._lifecycle import ContextResource
from src.plugin_sdk.disposable import Disposable
from src.plugin_sdk.errors import PermissionDenied, PluginError
from src.utils.i18n import t as tr

_Value = TypeVar("_Value")


@dataclass(frozen=True)
class TaskResult(Generic[_Value]):
    """后台计算的最终值或失败说明，取消不承诺回滚工作函数的副作用。"""

    value: _Value | None = None
    error: str = ""
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        """仅在未失败且未取消时表示成功。"""
        return not self.error and not self.cancelled


@dataclass(frozen=True)
class TaskProgress:
    """后台任务的零到一进度和可选说明。"""

    fraction: float
    message: str = ""


class TaskCancelled(Exception):
    """工作函数在协作边界响应取消请求。"""


class TaskContext:
    """可在工作线程使用的最小上下文，不包含宿主服务或界面对象。"""

    def __init__(self, cancelled: Event, report: Callable[[TaskProgress], None]) -> None:
        """绑定线程安全的取消信号和宿主进度投递入口。"""
        self._cancelled = cancelled
        self._report = report

    @property
    def cancelled(self) -> bool:
        """查询是否收到取消请求。"""
        return self._cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        """在计算批次或文件操作边界协作停止。"""
        if self.cancelled:
            raise TaskCancelled()

    def report_progress(self, fraction: float, message: str = "") -> None:
        """报告有限且位于零到一之间的进度；宿主会合并尚未交付的更新。"""
        self.raise_if_cancelled()
        if type(fraction) not in (int, float) or not math.isfinite(fraction) or not 0 <= fraction <= 1:
            raise ValueError(tr("任务进度必须在 0 到 1 之间"))
        if not isinstance(message, str):
            raise TypeError("message 必须是字符串")
        self._report(TaskProgress(float(fraction), message))


class BackgroundTask(Generic[_Value]):
    """在宿主线程观察结果的任务；不提供阻塞等待方法。"""

    def __init__(self, cancel=None, error_sink=None, *, title: str = "") -> None:
        """初始化观察者、取消入口及创建线程。"""
        self.title = title
        self._thread_id = get_ident()
        self._result: TaskResult[_Value] | None = None
        self._finished: dict[object, Callable] = {}
        self._progress: dict[object, Callable] = {}
        self._cancel = cancel
        self._error_sink = error_sink
        self._attached = True

    def _require_thread(self) -> None:
        """阻止在工作线程注册或执行宿主回调。"""
        if get_ident() != self._thread_id:
            raise PluginError(tr("任务结果只能在宿主线程观察"))

    @property
    def done(self) -> bool:
        """非阻塞地查询任务是否已有最终结果。"""
        self._require_thread()
        return self._result is not None

    @property
    def result(self) -> TaskResult[_Value] | None:
        """读取最终结果，未完成时返回 None。"""
        self._require_thread()
        return self._result

    def cancel(self) -> None:
        """请求协作取消；尚未开始的任务不会再执行。"""
        self._require_thread()
        if self._result is None and self._cancel is not None:
            self._cancel()

    def on_finished(self, callback: Callable[[TaskResult[_Value]], None]) -> Disposable:
        """订阅完成结果；完成后注册立即调用，停用后注册不会交付。"""
        self._require_thread()
        if not callable(callback):
            raise TypeError("callback 必须可调用")
        if self._attached and self._result is not None:
            self._call(callback, self._result, "task.finished")
            return Disposable(lambda: None)
        return self._subscribe(self._finished, callback)

    def on_progress(self, callback: Callable[[TaskProgress], None]) -> Disposable:
        """订阅合并后的进度，结束或停用后的任务不再交付更新。"""
        self._require_thread()
        if not callable(callback):
            raise TypeError("callback 必须可调用")
        if self._result is not None:
            return Disposable(lambda: None)
        return self._subscribe(self._progress, callback)

    def _subscribe(self, listeners: dict, callback: Callable) -> Disposable:
        """给观察者分配独立身份，注销不会影响相同函数的其他订阅。"""
        token = object()
        if self._attached:
            listeners[token] = callback
        return Disposable(lambda: listeners.pop(token, None))

    def _call(self, callback: Callable, value, source: str) -> None:
        """隔离插件回调异常，并仅向仍有效的所属插件报告故障。"""
        try:
            callback(value)
        except Exception as exc:
            if self._attached and self._error_sink is not None:
                self._error_sink(source, exc)
            elif self._attached:
                logging.getLogger(__name__).exception("插件任务回调失败")

    def _report(self, value: TaskProgress) -> None:
        """在宿主线程向仍有效的进度订阅者分发更新。"""
        self._require_thread()
        for token, callback in list(self._progress.items()):
            if self._attached and self._result is None and token in self._progress:
                self._call(callback, value, "task.progress")

    def _finish(self, result: TaskResult[_Value]) -> None:
        """只接受一次最终结果，交付期间发生停用时抑制剩余回调。"""
        self._require_thread()
        if self._result is not None:
            return
        self._result, self._cancel = result, None
        for token, callback in list(self._finished.items()):
            if self._attached and token in self._finished:
                self._call(callback, result, "task.finished")
        self._finished.clear()
        self._progress.clear()

    def _detach(self) -> None:
        """停用时解除所有观察者和错误入口，已完成任务也不能重新订阅。"""
        self._attached = False
        self._finished.clear()
        self._progress.clear()
        self._error_sink = None


class TaskFacade(ContextResource):
    """将有权限的插件后台请求交给宿主的有界工作队列。"""

    def __init__(self, plugin_id: str, runner, granted: frozenset[str]) -> None:
        """绑定插件归属、后台服务及已授予权限。"""
        super().__init__()
        self._plugin_id, self._runner, self._granted = plugin_id, runner, granted

    def run(self, worker: Callable[[TaskContext], _Value], *, title: str = "") -> BackgroundTask[_Value]:
        """提交纯数据计算，进度和完成回调由宿主投递回调用线程。"""
        self._require_open()
        if "background" not in self._granted:
            raise PermissionDenied("background")
        if self._runner is None:
            raise PluginError(tr("宿主未提供后台任务服务"))
        return self._runner.submit(self._plugin_id, worker, title=title)
