"""在后台准备文件系统 Mod 导入。"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, Signal

from src.core.mod_intake import DuplicateIntakeError, IntakeRequest, PreparedIntake
from src.utils import fs
from src.utils.i18n import t as tr
from src.utils.logger import logger

if TYPE_CHECKING:
    from src.core.mod_intake import ModIntakeService


@dataclass(frozen=True)
class IntakeBatchResult:
    """汇总批量纳管的总数、提交、失败、重复、取消及来源恢复情况。"""

    total: int
    committed: int
    failed: int
    cancelled: bool
    restored: int
    # 重复项单独计数；其源内容仍位于资源库，须进入后续处置流程。
    duplicates: int = 0


class IntakeWorker(QThread):
    """并行准备文件，再由主线程逐项提交数据库的纳管工作线程。

    线程池执行解压、复制、压缩和哈希；数据库连接只在主线程使用。
    消费循环等待每项提交确认，文件准备任务无需等待数据库确认。
    """

    progress_updated = Signal(object, str, int, int)
    item_prepared = Signal(int, object)
    # 传递条目编号、错误说明、源路径状态及重复标记，供界面区分失败与重复。
    item_failed = Signal(int, str, bool, bool)
    batch_finished = Signal(object)
    #: 单项进度：(index, 该项自身 0~1 进度, 阶段文案)，供明细行显示自己的百分比。
    item_progress = Signal(int, float, str)

    def __init__(
        self,
        requests: Iterable[IntakeRequest],
        service: ModIntakeService,
        max_workers: int = 1,
        parent=None,
    ) -> None:
        """复制纳管请求，配置准备并发度，并创建取消、暂停及主线程提交确认的同步状态。"""
        super().__init__(parent)
        self._requests = list(requests)
        self._service = service
        self._max_workers = max(1, int(max_workers or 1))
        self._cancel_event = threading.Event()
        # 确认停止前先冻结文件处理，避免确认框的嵌套事件循环期间继续导入。
        # 冻结可恢复，取消不可恢复，两者使用独立状态。
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._ack_event = threading.Event()
        self._ack_lock = threading.Lock()
        self._ack_committed = False
        self._ack_error = ""
        self._pending: PreparedIntake | None = None
        # 并行进度聚合：{index: 该项内部 0~1 分数}。总进度 =
        # (已完成数 + Σ在途分数) / total。多线程回调，读写都在 _progress_lock 内。
        self._progress_lock = threading.Lock()
        self._frac_by_index: dict[int, float] = {}
        self._done_count = 0
        self._last_overall = -1.0

    def freeze(self) -> None:
        """从下一检查点暂停处理，直至恢复或收到取消请求。

        取消后拒绝再次冻结，避免线程在退出途中重新进入等待。
        """
        if self._cancel_event.is_set():
            return
        self._resume_event.clear()

    def unfreeze(self) -> None:
        """解除准备与结果消费阶段的暂停等待。"""
        self._resume_event.set()

    @property
    def is_frozen(self) -> bool:
        """返回批量纳管是否处于暂停状态。"""
        return not self._resume_event.is_set()

    def _wait_if_frozen(self) -> None:
        """冻结期间等待恢复，并定期检查取消状态。

        带超时等待可处理解冻与重新冻结交错，确保取消仍能结束等待。
        """
        while not self._resume_event.is_set():
            if self._cancel_event.is_set():
                return
            self._resume_event.wait(0.05)

    @property
    def is_cancel_requested(self) -> bool:
        """返回线程安全的批量取消标记。"""
        return self._cancel_event.is_set()
