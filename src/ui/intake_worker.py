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

    def request_cancel(self) -> None:
        """请求取消并解除暂停，使工作线程能够进入回滚和收尾流程。"""
        self._cancel_event.set()
        # 取消时同时解除冻结，允许工作线程退出。
        self._resume_event.set()
        self.requestInterruption()

    def acknowledge(self, *, committed: bool, error: str = "") -> None:
        """主线程接受或拒绝当前条目后释放工作线程。"""
        with self._ack_lock:
            if self._pending is None:
                return
            self._ack_committed = bool(committed)
            self._ack_error = error
            self._ack_event.set()

    def _report(
        self,
        index: int,
        total: int,
        value: float | None,
        text: str,
    ) -> None:
        """将单项进度转换为整批进度。

        并行任务按完成数量与在途分数之和计算，避免按输入索引聚合产生进度倒退。
        计算方式与 ParallelImportWorkerThread 一致。
        """
        if value is None:
            # 纯阶段文案（无百分比）：不参与聚合也不节流，否则阶段提示会被忽略。
            with self._progress_lock:
                current = min(total, self._done_count + 1) if total else 0
            self.progress_updated.emit(None, text, current, total)
            return
        frac = max(0.0, min(1.0, float(value)))
        # 单项进度独立于总进度节流，使明细行保持及时更新。
        self.item_progress.emit(index, frac, text)
        if not total:
            return
        with self._progress_lock:
            self._frac_by_index[index] = frac
            overall = (self._done_count + sum(self._frac_by_index.values())) / total
            overall = max(0.0, min(1.0, overall))
            current = min(total, self._done_count + 1)
            # 限制总进度信号频率，避免并行字节回调占用界面事件队列。
            if overall - self._last_overall < 0.005 and overall < 1.0:
                return
            self._last_overall = overall
        self.progress_updated.emit(overall, text, current, total)

    def _mark_item_done(self, index: int) -> None:
        """准备完成后将进度计入已完成数量，并从在途分数表移除，避免重复累计。"""
        with self._progress_lock:
            self._done_count += 1
            self._frac_by_index.pop(index, None)

    def run(self) -> None:
        """并发准备纳管条目，逐项向界面线程请求提交确认，并汇总批量结果。

        未提交或取消的准备结果由本线程回滚；结束前等待准备线程退出并清理尚未消费的结果，避免遗留已移动来源。"""
        total = len(self._requests)
        committed_count = 0
        failed_count = 0
        duplicate_count = 0
        restored_count = 0
        # 记录消费循环已接收的项目，供取消时清理尚未提交的结果。
        consumed: set[int] = set()

        def process_item(index: int, request: IntakeRequest):
            """线程池任务：只做 prepare（纯文件 IO），DB 提交仍留在主线程。"""
            # 每个准备线程均经过冻结检查，避免确认期间仍有任务继续处理。
            self._wait_if_frozen()
            if self.is_cancel_requested:
                return None

            def report(value: float | None, text: str) -> None:
                """为单项准备进度附加批次索引与总数，再交给统一进度限频逻辑。"""
                self._report(index, total, value, text)

            def gate() -> bool:
                """在准备操作的检查点等待恢复，再判断是否取消，使长时间文件处理能够及时停止。"""
                self._wait_if_frozen()
                return self.is_cancel_requested

            try:
                prepared = self._service.prepare(
                    request,
                    progress=report,
                    should_cancel=gate,
                )
            except Exception as exc:
                # 源是否还在原处只能在这条线程里判断（主线程稍后可能已被清理）。
                return (index, exc, bool(request.move_source and fs.exists(request.source_path)))
            return (index, prepared, False)

        futures: dict[Future, int] = {}
        executor: ThreadPoolExecutor | None = None
        workers = min(self._max_workers, max(1, total))

        def pool_results():
            """按后台准备完成顺序产出结果，记录异常退出任务并继续消费其余任务。"""
            for future in as_completed(futures):
                # 让出结果的位置须位于异常捕获之外，避免忽略生成器关闭信号后继续迭代。
                try:
                    item = future.result()
                except BaseException:
                    logger.exception("后台纳管任务异常退出")
                    continue
                yield item

        if workers > 1:
            executor = ThreadPoolExecutor(max_workers=workers)
            futures = {
                executor.submit(process_item, index, request): index for index, request in enumerate(self._requests)
            }
            results = pool_results()
        else:
            # 并发数为一时直接逐项执行，保留上一项确认后才开始下一项的顺序。
            results = (process_item(index, request) for index, request in enumerate(self._requests))

        try:
            for result in results:
                self._wait_if_frozen()
                if self.is_cancel_requested:
                    break
                if result is None:
                    continue  # 取消后跳过未开始的项，留给残留处置询问
                index, outcome, source_available = result
                consumed.add(index)
                self._mark_item_done(index)

                if isinstance(outcome, Exception):
                    if self.is_cancel_requested:
                        restored_count += int(source_available)
                        break
                    # 重复项不计入失败，但其冗余源内容仍须加入处置清单。
                    is_duplicate = isinstance(outcome, DuplicateIntakeError)
                    if is_duplicate:
                        duplicate_count += 1
                    else:
                        failed_count += 1
                    restored_count += int(source_available)
                    self.item_failed.emit(index, str(outcome), source_available, is_duplicate)
                    continue

                if self.is_cancel_requested:
                    restored_count += int(self._rollback(outcome))
                    break

                # ack 栅栏：只有本线程等 ack，主线程一次只处理一个 commit。
                with self._ack_lock:
                    self._pending = outcome
                    self._ack_committed = False
                    self._ack_error = ""
                    self._ack_event.clear()
                self.item_prepared.emit(index, outcome)
                self._ack_event.wait()

                with self._ack_lock:
                    committed = self._ack_committed
                    commit_error = self._ack_error
                    self._pending = None

                if committed:
                    committed_count += 1
                else:
                    restored = self._rollback(outcome)
                    restored_count += int(restored)
                    if not self.is_cancel_requested:
                        failed_count += 1
                        self.item_failed.emit(
                            index,
                            commit_error or tr("主线程未提交准备结果"),
                            restored,
                            False,
                        )

                if self.is_cancel_requested:
                    break
        finally:
            if executor is not None:
                # wait=True：等正在跑的解压/复制收尾，run 返回后主线程才能安全清理。
                # cancel_futures=True：还没开始的任务直接取消。
                executor.shutdown(wait=True, cancel_futures=True)
                restored_count += self._discard_unconsumed(futures, consumed)

        self.batch_finished.emit(
            IntakeBatchResult(
                total=total,
                committed=committed_count,
                failed=failed_count,
                cancelled=self.is_cancel_requested,
                restored=restored_count,
                duplicates=duplicate_count,
            )
        )

    def _discard_unconsumed(
        self,
        futures: dict[Future, int],
        consumed: set[int],
    ) -> int:
        """回滚已准备但尚未提交的结果，返回恢复的源文件数。

        取消时线程池可能已有未被消费的结果，须释放其容器及在途登记。
        """
        restored = 0
        for future, index in futures.items():
            if index in consumed or future.cancelled() or not future.done():
                continue
            # 单项回滚失败时记录错误并继续其他项目，保证线程退出及批次完成通知能够执行。
            try:
                result = future.result()
                if not result:
                    continue
                _index, outcome, _source_available = result
                if isinstance(outcome, Exception):
                    continue
                restored += int(self._rollback(outcome))
            except BaseException:
                logger.exception("丢弃未提交的纳管准备结果失败: index=%s", index)
        return restored

    def _rollback(self, prepared: PreparedIntake) -> bool:
        """撤销单项纳管准备，并返回已移动来源是否恢复到原路径。"""
        try:
            self._service.rollback(prepared)
        except Exception:
            logger.exception(
                "后台纳管回滚失败: %s",
                getattr(prepared, "original_path", "<invalid prepared result>"),
            )
            return False
        return bool(prepared.source_was_moved and fs.exists(prepared.original_path))
