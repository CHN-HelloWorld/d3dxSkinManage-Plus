"""不依赖 Qt 的宿主 Mod 操作结果。

任务在宿主线程中观察。取消请求在安全边界停止工作，已完成的文件操作仍会提交并计入结果。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class OperationResult:
    """插件操作的最终结果，区分成功、失败和取消的成员及可选预设或变体身份。"""

    game_id: str
    succeeded_ids: tuple[str, ...] = ()
    failed_ids: tuple[str, ...] = ()
    cancelled_ids: tuple[str, ...] = ()
    error: str = ""
    preset_id: str = ""
    variant_id: str | None = None
    node_id: str = ""
    skipped_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    cancel_requested: bool = False

    @property
    def ok(self) -> bool:
        """仅在无失败成员、取消成员和错误信息时报告成功。"""
        return not (self.failed_ids or self.cancelled_ids or self.error)


class OperationTask:
    """由宿主管理的任务，done 为真之前 result 为 None。

    on_finished 接收 OperationResult，任务完成后注册也会收到结果。
    插件停用时移除并禁用其回调。"""

    def __init__(self, cancel=None, error_sink=None) -> None:
        """初始化未完成任务，保存取消回调及回调异常处理入口。"""
        self._result: OperationResult | None = None
        self._callbacks: list[Callable[[OperationResult], None]] = []
        self._cancel = cancel
        self._error_sink = error_sink
        self._callbacks_enabled = True
        self._cancel_requested = False

    @property
    def done(self) -> bool:
        """判断宿主是否已提交最终结果。"""
        return self._result is not None

    @property
    def result(self) -> OperationResult | None:
        """非阻塞地读取最终结果，任务未完成时返回 None。"""
        return self._result

    def on_finished(self, callback: Callable[[OperationResult], None]) -> OperationTask:
        """注册完成回调；任务已完成时立即调用，已分离时忽略注册。"""
        if self._callbacks_enabled:
            if self.done:
                self._call(callback)
            else:
                self._callbacks.append(callback)
        return self

    def cancel(self) -> None:
        """对未完成任务记录取消请求，并调用宿主提供的取消入口。"""
        if not self.done:
            self._cancel_requested = True
            if self._cancel is not None:
                self._cancel()

    def _call(self, callback) -> None:
        """执行单个完成回调，将插件异常转交错误处理入口或日志。"""
        try:
            callback(self._result)
        except Exception as error:
            if not self._callbacks_enabled:
                return
            if self._error_sink is not None:
                self._error_sink(error)
            else:
                logging.getLogger(__name__).exception("Plugin operation callback failed")

    def _detach(self) -> None:
        """禁用并释放全部完成回调，防止已停用插件再次收到通知。"""
        self._callbacks_enabled = False
        self._callbacks.clear()

    def _finish(self, result: OperationResult) -> None:
        """仅首次接受最终结果，释放取消入口并依次通知仍启用的完成回调。"""
        if self.done:
            return
        self._result = result
        self._cancel = None
        callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            if self._callbacks_enabled:
                self._call(callback)
