"""在后台执行批量禁用的文件操作。

主线程准备作业单并决定停放或删除；后台仅操作文件和具有锁保护的 INI 同步器。
索引、预设、界面及事件由主线程在批次提交时更新。
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from src.utils.logger import logger


class DisableBatchWorker(QThread):
    """逐个执行禁用作业单的后台线程。

    item_started(index, display_name)：某项开始（供浮层把该行标成「进行中」）。
    item_finished(index, result)：某项结束，result 是 ``DisableStepResult``。
    finished_batch(results, cancelled)：整批结束；results 与传入 entries 等长同序，
        被取消而没跑到的项为 ``None``。
    """

    item_started = Signal(int, str)
    item_finished = Signal(int, object)
    finished_batch = Signal(object, bool)

    def __init__(self, entries, mod_manager, parent=None) -> None:
        """复制停用条目并保存管理器，初始化供关闭流程补交的结果与提交标记。"""
        super().__init__(parent)
        self._entries = list(entries)
        self._manager = mod_manager
        # 保存可直接读取的结果，供窗口关闭时补充提交尚未投递的完成信号。
        # 关闭流程可能不再处理排队事件，但文件结果仍须同步到数据库。
        self.results: list | None = None
        self.cancelled = False
        self.committed = False

    def run(self) -> None:
        """逐项执行停用并发出开始与完成信号，单项异常转为失败结果。

        取消请求在条目之间生效，尚未执行的位置保留 None；完整结果同时保存到实例，便于窗口关闭后提交。"""
        results: list[object | None] = [None] * len(self._entries)
        cancelled = False
        for index, entry in enumerate(self._entries):
            if self.isInterruptionRequested():
                cancelled = True
                break
            self.item_started.emit(index, entry.display_name)
            try:
                result = self._manager.disable_batch_step(entry)
            except Exception as e:
                logger.error("禁用作业单执行异常 (%s): %s", entry.display_name, e)
                from src.core.mod_manager import DisableStepResult

                result = DisableStepResult(mod_id=entry.mod_id, error=str(e))
            results[index] = result
            self.item_finished.emit(index, result)
        self.results = results
        self.cancelled = cancelled
        self.finished_batch.emit(results, cancelled)
