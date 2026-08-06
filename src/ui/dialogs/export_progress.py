"""在后台执行文件导出，由宿主任务中心展示批次及单项结果。

工作线程仅处理复制、压缩和解压，不写入资源库索引。每项开始前复查来源，
通过控制锁协调暂停、终止和结果确认；终止后撤回未确认产物。
控制器本身保持隐藏，使用 QDialog 的接受或拒绝结果通知宿主完成生命周期处理。
"""

from __future__ import annotations

import os
import shutil
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt, QThread, Signal
from PySide6.QtWidgets import QDialog, QMessageBox, QWidget

from src.core.archive_concurrency import effective_archive_workers
from src.core.config import Config
from src.core.mod_exporter import ExportItem, ExportResult, ModExporter
from src.core.mod_package import share_meta_from_mod
from src.ui.widgets.task_center import (
    ST_DONE,
    ST_FAILED,
    ST_QUEUED,
    ST_RUNNING,
)
from src.utils.i18n import t as tr
from src.utils.logger import logger


class _ExportAborted(Exception):
    """在进度回调中报告终止的内部异常；导出器可能忽略回调异常，最终结果仍须经过外层提交检查。"""


class ExportWorkerThread(QThread):
    """执行文件导出的工作线程。

    进度和单项结果通过信号投递；未收到终态的项目由界面在批次结束后标记为未导出，
    包括未开始及被取消的项目。
    """

    progress_updated = Signal(int, int, str)
    item_done = Signal(int, object)
    finished_all = Signal()
    # 以 0~1 的全局比例和文案报告进度，与导入 worker 的 stage_progress 格式一致，
    # 支持显示单个 Mod 解压或压缩过程中的进度变化。
    stage_progress = Signal(float, str)

    def __init__(
        self,
        items: list[ExportItem],
        dest_dir: str,
        fmt: str,
        preserve_tree: bool,
        library_root: str,
        include_meta: bool = False,
        strict_format: bool = False,
        parent=None,
        *,
        max_workers: int = 1,
    ) -> None:
        """保存导出计划和格式选项，创建暂停事件及提交控制锁，并将并发数限制为至少一。"""
        super().__init__(parent)
        self._items = items
        self._dest_dir = dest_dir
        self._fmt = fmt
        self._preserve_tree = preserve_tree
        self._include_meta = include_meta
        self._strict_format = strict_format
        self._max_workers = max(1, int(max_workers))
        self._exporter = ModExporter(library_root=library_root)
        self._paused = threading.Event()
        self._paused.set()  # 初始未暂停
        self._terminated = False
        self._control = threading.Condition(threading.RLock())
        self._item_progress: dict[int, float] = {}

    def run(self):
        """串行或限量并发执行文件导出，停止后不再提交新工作，等待在途文件操作结束再发出批次完成信号。"""
        total = len(self._items)
        if self._max_workers == 1:
            for i, item in enumerate(self._items):
                if self._terminated:
                    break
                self._export_item(i, item, total)
        elif total:
            # 只提交有限数量的工作，停止后不遗留整批排队任务，完成通知前等待文件操作结束。
            workers = min(self._max_workers, total)
            items = iter(enumerate(self._items))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mod-export") as pool:
                pending = set()
                while True:
                    with self._control:
                        while not self._terminated and len(pending) < workers:
                            entry = next(items, None)
                            if entry is None:
                                break
                            i, item = entry
                            pending.add(pool.submit(self._export_item, i, item, total))
                    if not pending:
                        break
                    completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in completed:
                        future.result()

        self.progress_updated.emit(total, total, tr("导出完成") if not self._terminated else tr("导出已终止"))
        self.finished_all.emit()

    def _export_item(self, i: int, item: ExportItem, total: int) -> None:
        """复查源路径后执行单项导出，将异常转成失败结果。

        开始和确认结果均受暂停锁控制；终止后撤回尚未确认的产物，不向界面报告该项成功。
        """
        with self._control:
            self._control.wait_for(self._paused.is_set)
            if self._terminated:
                return
            self.progress_updated.emit(
                i, total, tr("正在导出: {name} ({value}/{total})", name=item.name, value=i + 1, total=total)
            )
        try:
            if Path(item.src_path).exists():
                result = self._exporter.export_one(
                    item,
                    self._dest_dir,
                    self._fmt,
                    self._preserve_tree,
                    self._include_meta,
                    self._strict_format,
                    stage_cb=lambda frac, text, _i=i, _t=total: self._emit_stage(_i, _t, frac, text),
                )
            else:
                logger.warning("导出跳过（开始前复查源已不存在）: %s", item.src_path)
                result = ExportResult(False, item.name, skipped=True, error=tr("源文件已不存在"))
        except Exception as e:  # 回退处理：单项异常不应中断整批
            logger.error("导出工作线程异常 (%s): %s", item.name, e)
            result = ExportResult(False, item.name, error=str(e))

        # 暂停、停止和确认共用一把锁；尚未确认的产物必须撤回，即使底层复制已完成。
        with self._control:
            self._control.wait_for(self._paused.is_set)
            if not self._terminated:
                self.item_done.emit(i, result)
                self._update_stage(i, total, 1.0, item.name)
                return
        self._discard_product(result)
        logger.info("导出已终止，撤回未确认的产物: %s", item.name)

    def _discard_product(self, result) -> None:
        """撤回停止时尚未确认的输出，仅删除本批次创建的目标。

        结果明细与磁盘产物保持一致，底层已完成复制也不能绕过提交确认。
        """
        path_str = str(getattr(result, "out_path", "") or "")
        if not path_str:
            return
        path = Path(path_str)
        try:
            if path.is_dir():
                shutil.rmtree(str(path), ignore_errors=True)
            elif path.is_file():
                path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("丢弃未确认产物失败: %s: %s", path, e)

    def _emit_stage(self, item_index: int, total_items: int, frac: float, text: str) -> None:
        """将各 Mod 内部进度的总和折算为整体进度，并在回调边界响应暂停。

        暂停精度取决于归档引擎的回调频率。导出器会忽略进度回调异常，
        因此终止由外层提交检查处理，未确认的已完成产物会被撤回。
        """
        with self._control:
            self._control.wait_for(self._paused.is_set)
            if self._terminated:
                raise _ExportAborted
            self._update_stage(item_index, total_items, frac, text)

    def _update_stage(self, item_index: int, total_items: int, frac: float, text: str) -> None:
        """在控制锁内调用，保持并发进度通知的顺序。"""
        self._item_progress[item_index] = max(self._item_progress.get(item_index, 0.0), max(0.0, min(1.0, frac)))
        self.stage_progress.emit(sum(self._item_progress.values()) / max(1, total_items), text)

    def pause(self):
        """在控制锁内关闭执行事件，使任务在下一个进度或提交检查点等待。"""
        with self._control:
            self._paused.clear()

    def resume(self):
        """恢复执行事件并唤醒所有等待暂停解除的任务。"""
        with self._control:
            self._paused.set()
            self._control.notify_all()

    def terminate_export(self):
        """标记不可逆的批次终止并解除暂停等待，后续未确认产物由任务撤回。"""
        with self._control:
            self._terminated = True
            self._paused.set()  # 若处于暂停，解除阻塞以尽快退出
            self._control.notify_all()
