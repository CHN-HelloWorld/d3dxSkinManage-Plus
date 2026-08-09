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

    @property
    def is_paused(self) -> bool:
        """返回当前是否请求暂停，不表示所有底层文件操作都已停止。"""
        return not self._paused.is_set()


class ExportProgressDialog(QDialog):
    """导出进度控制器。

    通过主窗口右下角的任务中心面板显示进度和逐项状态，实例自身不显示窗口。
    QDialog 提供 accept/reject 生命周期接口。

    ``panel=None`` 时仅执行任务，工作线程的调度方式相同。
    """

    export_finished = Signal(int, int)  # (成功数, 总数)

    def __init__(
        self,
        mods: list,  # list[Mod]
        dest_dir: str,
        mod_manager,
        parent: QWidget | None = None,
        export_items: list[ExportItem] | None = None,
        panel=None,
    ) -> None:
        """接收宿主提供的任务面板和可选导出计划，读取配置并初始化本批产物及结果记录，不启动导出。"""
        super().__init__(parent)
        self._dest_dir = dest_dir
        self._panel = panel
        self._worker: ExportWorkerThread | None = None
        self._success = 0
        self._failed = 0
        self._skipped = 0
        self._failed_names: list[str] = []
        self._was_terminated = False
        #: 本批已成功写入磁盘的产物路径（「取消全部导出」要按这份清单回滚）。
        self._exported_paths: list[str] = []
        #: 结束后是否自动打开导出目录（选「取消全部导出」时置 False）。
        self._open_dir_on_finish = True
        #: 终止时是否回滚已导出的产物（由「取消全部导出」置 True）。
        self._purge_on_terminate = False
        #: 已上报过终态（成功/失败/跳过）的项下标。收尾时其余的一律标「未导出」。
        self._settled: set[int] = set()
        self._overall_progress = 0.0

        # 上层若已构建带 rel_dir 的导出计划（节点作根、保留结构）则直接采用；
        # 未提供导出项时从 Mod 构建，并按 preserve_tree 计算相对目录。
        if export_items is not None:
            self._items = list(export_items)
        else:
            self._items = [
                ExportItem(
                    name=m.display_name,
                    src_path=m.path,
                    meta=share_meta_from_mod(m),
                    preview_paths=list(getattr(m, "preview_images", []) or []),
                )
                for m in mods
            ]
        self._total = len(self._items)
        self._library_root = str(mod_manager._get_library_root())
        self._fmt = Config().get("export.format", "zip")
        self._preserve_tree = Config().get("export.preserve_tree", True)
        self._include_meta = bool(Config().get("export.include_metadata", False))
        self._strict_format = bool(Config().get("export.strict_format", False))

        self._setup_ui()

    def _setup_ui(self):
        """使控制器窗口保持隐藏，并连接宿主任务面板的控制信号、初始化逐项排队状态。"""
        self.setWindowTitle(tr("导出 Mod"))
        # 导出状态由任务中心展示，控制器窗口始终保持隐藏。
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        if self._panel is None:
            return
        self._panel.pause_requested.connect(self._on_pause)
        self._panel.resume_requested.connect(self._on_resume)
        self._panel.stop_requested.connect(self._on_terminate)
        title = tr("正在导出 {total} 个 Mod", total=self._total)
        self._panel.start_batch(
            self._total,
            title,
            has_pause=True,
            # 共用任务面板的默认文案用于纳管，导出需提供独立的终止提示。
            stop_tooltip=tr("停止导出"),
            cancelled_title=tr("已停止导出"),
            done_text=tr("已导出"),
            pause_tooltip=tr("暂停导出"),
            resume_tooltip=tr("继续导出"),
        )
        self._panel.update_progress(0.0, title, tr("准备中…"))
        for index, item in enumerate(self._items):
            self._panel.update_item(index, item.name, ST_QUEUED)

    def _release_panel(self) -> None:
        """断开与浮层的连线，避免下一批导出收到本批的回调。"""
        if self._panel is None:
            return
        for sig, slot in (
            (self._panel.pause_requested, self._on_pause),
            (self._panel.resume_requested, self._on_resume),
            (self._panel.stop_requested, self._on_terminate),
        ):
            try:
                sig.disconnect(slot)
            except TypeError:
                pass
        self._panel = None

    def start_export(self):
        """按配置创建导出线程并连接面板更新信号，空批次直接进入正常收尾。"""
        if self._total == 0:
            self._on_finished()
            return
        self._worker = ExportWorkerThread(
            self._items,
            self._dest_dir,
            self._fmt,
            self._preserve_tree,
            self._library_root,
            self._include_meta,
            strict_format=self._strict_format,
            parent=self,
            max_workers=effective_archive_workers(Config().get("export.parallel_workers")),
        )
        self._worker.progress_updated.connect(self._on_progress)
        self._worker.item_done.connect(self._on_item_done)
        self._worker.finished_all.connect(self._on_finished)
        # 细化进度（单个 Mod 内部阶段），与导入 worker 的 stage_progress 同格式
        self._worker.stage_progress.connect(self._on_stage_progress)
        self._worker.start()

    # worker → 浮层

    def _on_progress(self, current: int, total: int, detail: str):
        """每个 Mod 起步的粗粒度进度；Mod 内部阶段由 _on_stage_progress 细化。"""
        if self._panel is None:
            return
        if 0 <= current < len(self._items):
            self._panel.update_item(current, self._items[current].name, ST_RUNNING, tr("正在导出…"))
        self._panel.update_progress(
            self._overall_progress * 100.0, tr("正在导出 {total} 个 Mod", total=self._total), detail
        )

    def _on_stage_progress(self, overall: float, text: str):
        """单个 Mod 内部阶段进度（overall 0~1 全局小数）。"""
        self._overall_progress = max(self._overall_progress, max(0.0, min(1.0, overall)))
        if self._panel is None:
            return
        self._panel.update_progress(
            self._overall_progress * 100.0, tr("正在导出 {total} 个 Mod", total=self._total), text
        )

    def _on_item_done(self, index: int, result: ExportResult):
        """在界面线程记录单项成功、失败或源缺失，并登记已确认产物路径供取消全部导出时清理。"""
        if result.skipped:
            self._skipped += 1
            status, detail = ST_FAILED, tr("已跳过·{value}", value=result.error or tr("源缺失"))
        elif result.ok:
            self._success += 1
            if result.out_path:
                self._exported_paths.append(str(result.out_path))
            status, detail = ST_DONE, tr("已导出")
        else:
            self._failed += 1
            self._failed_names.append(result.name)
            status, detail = ST_FAILED, result.error or tr("失败")
        self._settled.add(index)
        done = self._success + self._failed + self._skipped
        self._overall_progress = max(self._overall_progress, done / max(1, self._total))
        if self._panel is None:
            return
        name = self._items[index].name if 0 <= index < len(self._items) else result.name
        self._panel.update_item(index, name, status, detail)
        self._panel.update_progress(
            self._overall_progress * 100.0, tr("正在导出 {total} 个 Mod", total=self._total), f"{done}/{self._total}"
        )

    def _mark_unsettled_items(self) -> None:
        """将未收到终态的明细行标记为未导出。

        包括中途取消及尚未开始的项目，不增加成功或失败计数。
        """
        if self._panel is None:
            return
        for index, item in enumerate(self._items):
            if index not in self._settled:
                self._panel.update_item(index, item.name, ST_FAILED, tr("未导出"))

    def _on_finished(self):
        """正常完成时结算面板、报告成功数并接受控制器；已终止批次交由终止流程收尾。"""
        if self._was_terminated:
            return  # 已在 _on_terminate 中处理收尾
        self._finish_panel(cancelled=False)
        self.export_finished.emit(self._success, self._total)
        self.accept()

    def _finish_panel(self, *, cancelled: bool) -> None:
        """补齐未结算明细、显示批次摘要并断开面板信号；允许且有成功产物时打开目标目录。"""
        self._mark_unsettled_items()
        detail = self._summary_detail()
        if self._panel is not None:
            if cancelled:
                self._panel.finish_cancelled(detail)
            else:
                self._panel.finish(
                    tr("已导出 {success}/{total} 个 Mod", success=self._success, total=self._total),
                    detail,
                    ok=not (self._failed or self._skipped),
                )
        logger.info("导出结束（%s）: %s → %s", "已终止" if cancelled else "完成", detail, self._dest_dir)
        self._release_panel()
        # 导出完成且有实际产物时自动打开目标目录；取消全部导出时跳过。
        if self._open_dir_on_finish and self._success > 0:
            self._open_dest_dir()

    def _summary_detail(self) -> str:
        """生成批次结果说明，确保各状态数量之和等于总数。"""
        parts = [tr("成功 {success}", success=self._success)]
        if self._failed:
            parts.append(tr("失败 {failed}", failed=self._failed))
        if self._skipped:
            parts.append(tr("跳过 {skipped}（源缺失）", skipped=self._skipped))
        remaining = self._total - (self._success + self._failed + self._skipped)
        if remaining > 0:
            parts.append(tr("未导出 {remaining}", remaining=remaining))
        return "，".join(parts) + f" · {self._dest_dir}"

    def _open_dest_dir(self) -> None:
        """使用当前平台的系统文件管理器打开导出目录，失败仅记录警告。"""
        try:
            if os.name == "nt":
                os.startfile(self._dest_dir)  # type: ignore[attr-defined]
            else:
                import subprocess

                subprocess.Popen(["xdg-open", self._dest_dir])
        except Exception as e:
            logger.warning("打开导出目录失败: %s", e)

    # 浮层 → worker

    def _on_pause(self):
        """请求后台协作暂停，并在任务面板显示恢复或停止操作提示。"""
        if self._worker:
            self._worker.pause()
        if self._panel is not None:
            self._panel.set_paused(True)
            self._panel.update_progress(None, tr("导出已暂停"), tr("选择“继续导出”恢复任务，或选择“停止导出”。"))

    def _on_resume(self):
        """恢复后台执行事件并解除任务面板的暂停状态。"""
        if self._worker:
            self._worker.resume()
        if self._panel is not None:
            self._panel.set_paused(False)

    def _on_terminate(self):
        """先暂停工作线程，再询问保留、撤销或继续导出。

        确认框会运行嵌套事件循环，暂停必须先于显示，以免确认期间继续创建产物。
        """
        if not self._worker or not self._worker.isRunning():
            return
        was_paused = self._worker.is_paused
        self._worker.pause()
        if self._panel is not None:
            self._panel.set_paused(True)
        # 先派发本控制器已排队的单项完成通知，使确认框计数与明细一致。
        # 限定接收者可避免 processEvents 引入无关输入事件的重入。
        QCoreApplication.sendPostedEvents(self, 0)

        remaining = max(0, self._total - (self._success + self._failed + self._skipped))
        # 确认框须以可见主窗口为父对象，不能使用隐藏的导出控制器窗口。
        host = self.parent()
        box = QMessageBox(host.window() if host is not None else None)
        box.setWindowTitle(tr("停止导出"))
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            tr(
                "已导出 {success} 个 Mod，还有 {remaining} 个 Mod 未导出，请选择操作：",
                success=self._success,
                remaining=remaining,
            )
        )
        keep_btn = box.addButton(tr("保留已导出的Mod"), QMessageBox.ButtonRole.AcceptRole)
        purge_btn = box.addButton(tr("取消全部导出"), QMessageBox.ButtonRole.DestructiveRole)
        resume_btn = box.addButton(tr("继续导出"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(resume_btn)
        box.exec()
        clicked = box.clickedButton()

        # 仅在明确选择保留或撤销时停止；关闭确认框时继续导出。
        # 停止后不会自动补做剩余项目，未明确选择停止时恢复此前执行状态。
        if clicked is purge_btn:
            self._open_dir_on_finish = False
            self._do_terminate(purge=True)
            return
        if clicked is keep_btn:
            self._do_terminate(purge=False)
            return
        if not was_paused:
            self._on_resume()

    def _do_terminate(self, *, purge: bool) -> None:
        """请求终止并在工作线程结束后处理结果，按选择回滚已导出的产物。"""
        if not self._worker:
            return
        self._was_terminated = True
        self._purge_on_terminate = purge
        self._worker.terminate_export()  # 内部会 _paused.set 解除阻塞
        if self._panel is not None:
            self._panel.set_paused(False)
            self._panel.set_stop_enabled(False)
            self._panel.update_progress(None, tr("正在停止导出…"), tr("正在等待后台任务结束"))
        if self._worker.isRunning():
            self._worker.finished.connect(self._do_terminate_finish)
        else:
            self._do_terminate_finish()

    def _do_terminate_finish(self):
        """在线程终止后按用户决定保留或清理本批已确认产物，再结算面板并报告结果。"""
        if getattr(self, "_purge_on_terminate", False):
            self._purge_exported()
        self._finish_panel(cancelled=True)
        self.export_finished.emit(self._success, self._total)
        self.reject()

    def _purge_exported(self) -> None:
        """仅清理本批记录的输出路径，不扫描或删除目标目录中的其他内容。"""
        removed = 0
        for raw in self._exported_paths:
            path = Path(raw)
            try:
                if path.is_dir():
                    shutil.rmtree(str(path), ignore_errors=True)
                    removed += 1
                elif path.is_file():
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError as e:
                logger.warning("回滚导出产物失败: %s: %s", path, e)
        self._exported_paths.clear()
        self._success = 0
        logger.info("已按用户选择撤销本批导出产物: %d 个", removed)

    def closeEvent(self, event):
        """运行中禁止关闭（本控制器不显示窗口，此处只作回退处理）。"""
        if self._worker and self._worker.isRunning():
            event.ignore()
        else:
            super().closeEvent(event)
