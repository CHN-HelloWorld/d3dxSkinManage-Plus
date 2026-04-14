"""协调后台文件准备、界面线程索引提交及导入批次的暂停和清理。

工作线程接收已规划的 IntakeRequest，通过结果信号与主线程逐项确认提交；
并行准备完成后仍串行交接数据库操作，控制器使用宿主的任务中心展示状态。
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QMessageBox,
    QWidget,
)

from src.core.archive_concurrency import effective_archive_workers
from src.core.mod_intake import (
    DuplicateIntakeError,
    IntakeRequest,
    IntakeTargetMissingError,
    ModIntakeService,
    PreparedIntake,
)
from src.models.mod import Mod
from src.ui.widgets.task_center import (
    ST_DONE,
    ST_FAILED,
    ST_QUEUED,
    ST_ROLLED_BACK,
    ST_RUNNING,
)
from src.utils import fs
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_data_dir
from src.utils.qt_timers import call_later

# 限制可传递到导入计划的附加参数，避免无效键导致整个批次失败。
_PLAN_EXTRA_KEYS = frozenset(
    {
        "source_name",
        "target_format",
        "strict_format",
        "auto_preview",
        "excluded_previews",
        "gamebanana_metadata",
        "force_age_rating",
        "context",
    }
)


def _item_label(item, source) -> str:
    """选择任务明细使用的显示名称。

    优先使用独立的显示字段，避免展示下载缓存文件名；该字段不写入 Mod 的正式名称。
    """
    try:
        if len(item) >= 6 and item[5]:
            return str(item[5])
        if len(item) >= 2 and item[1]:
            return str(item[1])
    except TypeError:
        pass
    return Path(str(source)).name


def _wait_for_worker(worker) -> bool:
    """协作式暂停点；终止会唤醒 Event 并返回 False。"""
    if not worker._paused.is_set():
        worker._paused.wait()
    return not worker._terminated


def _require_intake_requests(items) -> list[IntakeRequest]:
    """将输入具体化为请求列表，包含非 IntakeRequest 元素时抛出 TypeError。"""
    requests = list(items)
    if any(not isinstance(item, IntakeRequest) for item in requests):
        raise TypeError("import workers accept IntakeRequest items only")
    return requests


def _rollback_prepared(service, result) -> None:
    """仅回滚尚未提交的准备结果，忽略其他结果类型，并记录回滚异常而不阻断终止收尾。"""
    if not isinstance(result, PreparedIntake) or result.committed:
        return
    try:
        service.rollback(result)
    except Exception:
        logger.exception("取消导入时回滚准备结果失败")


class _CreatedImportArtifacts:
    """线程安全地登记本批实际创建的容器和预览缓存，使清理范围有明确依据。"""

    def __init__(self) -> None:
        """创建由同一互斥锁保护的容器与预览缓存路径集合。"""
        self._lock = threading.Lock()
        self._containers: set[str] = set()
        self._preview_caches: set[str] = set()

    def add_container(self, path: str | Path) -> None:
        """规范化并登记本批新建容器路径，可由多个准备线程并发调用。"""
        with self._lock:
            self._containers.add(fs.normal_path(path))

    def add_preview_cache(self, path: str | Path) -> None:
        """规范化并登记本批新建预览缓存路径，使用锁避免并发修改集合。"""
        with self._lock:
            self._preview_caches.add(fs.normal_path(path))

    def containers(self) -> list[Path]:
        """在锁内取得容器路径快照，返回独立列表供完成后的清理使用。"""
        with self._lock:
            return [Path(path) for path in self._containers]

    def preview_caches(self) -> list[Path]:
        """在锁内取得预览缓存路径快照，返回值不共享内部集合。"""
        with self._lock:
            return [Path(path) for path in self._preview_caches]


def _remove_path_reliably(path: str | Path) -> bool:
    """删除文件/目录并验证结果；Windows 长路径始终走扩展长度 API。"""
    target = Path(fs.normal_path(path))

    def _on_error(func, failed_path, _exc_info):
        """对删除失败路径尝试添加写权限后重试原操作，重试失败由外层最终存在性检查判断。"""
        try:
            import stat

            os.chmod(failed_path, stat.S_IWRITE)
            func(failed_path)
        except OSError:
            pass

    try:
        if fs.is_dir(target):
            fs.rmtree(target, ignore_errors=False, onerror=_on_error)
        elif fs.exists(target):
            fs.unlink(target, missing_ok=True)
    except OSError as e:
        logger.warning("删除导入残留失败 (%s): %s", target, e)
    return not fs.exists(target)


class ImportWorkerThread(QThread):
    """顺序准备导入文件，并逐项等待界面线程确认数据库处理。

    file_prepared 发送原索引及 PreparedIntake 或异常；progress_updated 和 stage_progress
    报告批次进度，item_progress 报告单项进度，finished_all 表示准备循环结束。
    """

    progress_updated = Signal(int, int, str)
    file_prepared = Signal(int, object)
    finished_all = Signal()
    # 报告全局小数进度及当前处理说明，反映单个 Mod 内部操作的推进。
    stage_progress = Signal(float, str)
    #: 单项进度：(item_index, 该项自身 0~1 进度, 阶段文案)。
    #: 任务中心据此更新对应明细行，避免并行任务的全局进度丢失单项对应关系。
    item_progress = Signal(int, float, str)

    def __init__(
        self,
        items: list[IntakeRequest],
        *,
        intake_service: ModIntakeService,
        parent=None,
    ):
        """校验请求和必要的纳管服务，初始化暂停、终止、数据库确认事件及产物记录；无服务时抛出 TypeError。"""
        super().__init__(parent)
        self._items = _require_intake_requests(items)
        if intake_service is None:
            raise TypeError("import workers require a ModIntakeService")
        self._intake_service = intake_service
        self._paused = threading.Event()
        self._paused.set()  # 初始状态允许继续执行。
        self._terminated = False
        self._db_done = threading.Event()
        self._db_done.set()  # 首次迭代无需等待确认。
        self._artifacts = _CreatedImportArtifacts()

    def run(self):
        """逐项准备文件并将结果或异常投递给主线程，等待数据库确认后继续；终止会解除等待并停止后续处理。"""
        total = len(self._items)
        for i, request in enumerate(self._items):
            # 检查暂停请求。
            self._paused.wait()

            # 检查终止请求。
            if self._terminated:
                break

            name = request.display_name or request.source_path.name
            try:
                result = self._prepare_request(
                    request,
                    item_index=i,
                    total_items=total,
                )
            except Exception as exc:
                result = exc

            detail = tr("正在导入: {name} ({value}/{total})", name=name, value=i + 1, total=total)
            self.progress_updated.emit(i, total, detail)

            # 等待上一次DB操作完成
            self._db_done.wait()
            if self._terminated:
                break

            # 通知主线程执行SQLite操作
            self._db_done.clear()
            self.file_prepared.emit(i, result)
            # 等待主线程完成DB写入
            self._db_done.wait()
            if self._terminated:
                _rollback_prepared(self._intake_service, result)
                break

        if not self._terminated:
            _wait_for_worker(self)
        self.progress_updated.emit(total, total, tr("导入完成") if not self._terminated else tr("导入已终止"))
        self.finished_all.emit()

    def _emit_stage(self, item_index: int, total_items: int, frac: float, text: str) -> None:
        """将条目索引及单项进度折算为 0 到 1 的整体进度。"""
        if total_items <= 0:
            return
        if not _wait_for_worker(self):
            return
        overall = (item_index + max(0.0, min(1.0, frac))) / total_items
        try:
            self.stage_progress.emit(overall, text)
            # 同时报告单项进度，供明细行显示。
            self.item_progress.emit(item_index, max(0.0, min(1.0, frac)), text)
        except RuntimeError:
            pass  # 控件已销毁

    def _prepare_request(
        self,
        request,
        *,
        item_index: int = 0,
        total_items: int = 1,
    ):
        """调用纳管服务准备文件并登记实际创建的容器和缓存，返回准备结果；服务异常交由调用方处理。"""
        if self._intake_service is None:
            raise RuntimeError("planned intake requires a ModIntakeService")

        last_progress = 0.0

        def report(value, text: str) -> None:
            """保留单项已报告的最大进度，空值只更新说明，再折算为批次进度。"""
            nonlocal last_progress
            if value is not None:
                last_progress = max(last_progress, float(value))
            self._emit_stage(
                item_index,
                total_items,
                last_progress,
                text,
            )

        prepared = self._intake_service.prepare(
            request,
            progress=report,
            should_cancel=lambda: not _wait_for_worker(self),
        )
        if isinstance(prepared, PreparedIntake):
            self._artifacts.add_container(prepared.container_path)
            if prepared.preview_cache_path is not None:
                self._artifacts.add_preview_cache(prepared.preview_cache_path)
        return prepared

    def get_created_paths(self) -> list[Path]:
        """返回本批实际登记的容器路径快照，供界面在后台操作结束后按所有权清理。"""
        return self._artifacts.containers()

    def get_created_preview_caches(self) -> list[Path]:
        """返回本批创建的预览缓存路径快照，调用方需排除已提交项目仍引用的缓存。"""
        return self._artifacts.preview_caches()

    def acknowledge_db_done(self):
        """由主线程在结果处理完成后设置确认事件，允许后台继续处理后续项目。"""
        self._db_done.set()

    def pause(self):
        """请求协作暂停，使后续准备和进度检查点等待执行事件。"""
        self._paused.clear()

    def resume(self):
        """解除暂停事件，允许等待中的后台文件准备继续。"""
        self._paused.set()

    def terminate_import(self):
        """标记终止并释放暂停及数据库确认等待，由后续收尾处理未提交产物。"""
        self._terminated = True
        self._paused.set()  # 释放暂停等待。
        self._db_done.set()  # 释放数据库确认等待。

    @property
    def is_paused(self) -> bool:
        """返回执行事件是否处于暂停状态，不保证当前文件调用已经返回。"""
        return not self._paused.is_set()


class ParallelImportWorkerThread(QThread):
    """在线程池中并行准备文件，按完成顺序串行交接界面线程的索引提交。

    file_prepared 保留原项目索引，逐项等待数据库处理确认；并发进度在锁内聚合，
    finished_all 仅在线程池已关闭且在途文件操作结束后发送。
    """

    progress_updated = Signal(int, int, str)
    file_prepared = Signal(int, object)
    finished_all = Signal()
    # 全局进度采用零到一的小数及阶段文案，与顺序线程共用界面处理入口。
    stage_progress = Signal(float, str)
    #: 单项进度：(item_index, 该项自身 0~1 进度, 阶段文案)。见 ImportWorkerThread 同名信号。
    #: 并行导入按原索引区分各项的阶段和进度。
    item_progress = Signal(int, float, str)

    def __init__(
        self,
        items: list[IntakeRequest],
        *,
        intake_service: ModIntakeService,
        max_workers: int = 3,
        parent=None,
    ):
        """校验请求和服务，保存线程池并发数并初始化受锁保护的进度与产物记录。"""
        super().__init__(parent)
        self._items = _require_intake_requests(items)
        if intake_service is None:
            raise TypeError("import workers require a ModIntakeService")
        self._max_workers = max_workers
        self._intake_service = intake_service
        self._paused = threading.Event()
        self._paused.set()
        self._terminated = False
        self._db_done = threading.Event()
        self._db_done.set()
        self._completed_count = 0
        self._lock = threading.Lock()
        # 并行进度聚合——记录每个在途 Mod 的内部完成分数(0~1)。总进度 =
        # (已完成数 + Σ在途分数) / total。读写都在 self._lock 内（多 worker 并发回调）。
        self._frac_by_index: dict[int, float] = {}
        self._last_emitted_overall = -1.0  # 节流：分数变化过小不发信号，避免信号风暴
        self._executor: ThreadPoolExecutor | None = None
        self._artifacts = _CreatedImportArtifacts()

    def _emit_stage(self, item_index: int, total_items: int, frac: float, text: str) -> None:
        """在锁内累计并行任务的完成数量和在途分数，计算总体进度。

        总进度变化不足阈值时不发出信号，限制界面更新频率。
        """
        if total_items <= 0:
            return
        if not _wait_for_worker(self):
            return
        frac = max(0.0, min(1.0, frac))
        # 单项进度不参与全局节流，避免总体增量不足 0.005 时抑制局部任务的可见进展。
        try:
            self.item_progress.emit(item_index, frac, text)
        except RuntimeError:
            pass
        with self._lock:
            self._frac_by_index[item_index] = frac
            overall = (self._completed_count + sum(self._frac_by_index.values())) / total_items
            overall = max(0.0, min(1.0, overall))
            if overall - self._last_emitted_overall < 0.005 and overall < 1.0:
                return
            self._last_emitted_overall = overall
        try:
            self.stage_progress.emit(overall, text)
        except RuntimeError:
            pass  # 控件已销毁

    def run(self):
        """在线程池并行准备请求，按完成顺序逐项等待主线程提交确认；退出前关闭线程池并等待在途操作。"""
        total = len(self._items)

        def process_item(index, request):
            """在准备前响应暂停和终止，返回原索引、显示名称及准备结果或异常；未开始即终止时返回空值。"""
            # 检查暂停和终止请求。
            self._paused.wait()
            if self._terminated:
                return None

            name = request.display_name or request.source_path.name
            try:
                prepared = self._prepare_request(
                    request,
                    item_index=index,
                    total_items=total,
                )
            except Exception as exc:
                prepared = exc
            return (
                index,
                name,
                prepared,
            )

        # 使用线程池并行处理
        executor = ThreadPoolExecutor(max_workers=self._max_workers)
        self._executor = executor
        try:
            # 提交所有任务
            futures = {executor.submit(process_item, i, item): i for i, item in enumerate(self._items)}

            # 按完成顺序处理结果
            for future in as_completed(futures):
                if not _wait_for_worker(self):
                    break
                if self._terminated:
                    # 取消尚未开始的任务
                    for f in futures:
                        f.cancel()
                    break

                try:
                    result = future.result()
                    if result is None:
                        continue

                    index, name, prepared_result = result

                    # 更新进度
                    with self._lock:
                        self._completed_count += 1
                        current = self._completed_count
                        # 该 Mod 已完成，从在途分数表移除（其分数已并入 completed_count，
                        # 否则会被重复计入总和）
                        self._frac_by_index.pop(index, None)

                    detail = tr("正在导入: {name} ({current}/{total})", name=name, current=current, total=total)
                    # 单项完成只推进总进度，不以粗粒度索引覆盖已有细分进度。
                    self.stage_progress.emit(current / total, detail)
                    self.progress_updated.emit(current, total, detail)

                    # 等待上一次DB操作完成
                    self._db_done.wait()
                    if self._terminated:
                        break

                    # 通知主线程执行SQLite操作
                    self._db_done.clear()
                    self.file_prepared.emit(index, prepared_result)
                    # 等待主线程完成DB写入
                    self._db_done.wait()
                    if self._terminated:
                        _rollback_prepared(self._intake_service, prepared_result)
                        break

                except Exception as e:
                    logger.error("并行导入处理失败: %s", e)
                    with self._lock:
                        self._completed_count += 1
        finally:
            # wait=True: 等待正在运行的 copy_mod/compress_to_zip 完成
            # cancel_futures=True: 取消尚未开始的任务
            # 这样确保 run 返回时所有文件 IO 都已结束，主线程可安全清理
            executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None

        if not self._terminated:
            _wait_for_worker(self)
        self.progress_updated.emit(total, total, tr("导入完成") if not self._terminated else tr("导入已终止"))
        self.finished_all.emit()

    def _prepare_request(
        self,
        request,
        *,
        item_index: int = 0,
        total_items: int = 1,
    ):
        """在池线程准备单项文件并线程安全地登记产物，将暂停与终止检查交给服务回调。"""
        if self._intake_service is None:
            raise RuntimeError("planned intake requires a ModIntakeService")
        last_progress = 0.0

        def report(value, text: str) -> None:
            """使当前任务的进度单调递增，并交给带锁的批次聚合器发送进度信号。"""
            nonlocal last_progress
            if value is not None:
                last_progress = max(last_progress, float(value))
            self._emit_stage(item_index, total_items, last_progress, text)

        prepared = self._intake_service.prepare(
            request,
            progress=report,
            should_cancel=lambda: not _wait_for_worker(self),
        )
        if isinstance(prepared, PreparedIntake):
            self._artifacts.add_container(prepared.container_path)
            if prepared.preview_cache_path is not None:
                self._artifacts.add_preview_cache(prepared.preview_cache_path)
        return prepared

    def get_created_paths(self) -> list[Path]:
        """返回所有准备线程登记的容器路径快照，供线程池结束后清理未提交项目。"""
        return self._artifacts.containers()

    def get_created_preview_caches(self) -> list[Path]:
        """返回并行准备创建的预览缓存路径快照，不在读取时删除任何缓存。"""
        return self._artifacts.preview_caches()

    def acknowledge_db_done(self):
        """由主线程确认当前结果已经处理，释放串行提交交接的等待。"""
        self._db_done.set()

    def pause(self):
        """关闭共享执行事件，使并行任务在各自的协作检查点暂停。"""
        self._paused.clear()

    def resume(self):
        """设置共享执行事件，唤醒等待中的并行准备任务。"""
        self._paused.set()

    def terminate_import(self):
        """请求整个批次停止并解除暂停和数据库等待，线程池收尾负责等待仍在运行的文件操作。"""
        self._terminated = True
        self._paused.set()
        self._db_done.set()

    @property
    def is_paused(self) -> bool:
        """返回共享执行事件是否关闭，表示批次请求的暂停状态。"""
        return not self._paused.is_set()


class _NoopView:
    """无面板时的空视图。

    导入控制器通过共用任务中心面板显示进度。调用方未提供面板时，空视图将渲染调用
    处理为空操作，使业务路径无需逐次检查面板是否存在。

    仅允许调用约定的渲染方法，其他属性访问抛出 AttributeError，避免掩盖缺少面板的问题。
    """

    #: 允许静默忽略的渲染方法（与 TaskCenterPanel 的对外 API 对齐）
    _RENDER_METHODS = frozenset(
        {
            "start_batch",
            "update_progress",
            "update_item",
            "finish",
            "finish_cancelled",
            "queue_info",
            "set_paused",
            "set_stop_enabled",
            "reposition",
            "retheme",
        }
    )

    def __getattr__(self, name):
        """为约定的面板渲染方法返回空操作，其他属性抛出 AttributeError 以暴露错误的面板依赖。"""
        if name in self._RENDER_METHODS:

            def _noop(*_args, **_kwargs):
                """接收并忽略渲染调用的参数，返回空值且不保存界面状态。"""
                return None

            return _noop
        raise AttributeError(
            tr(
                "_NoopView 没有 {name!r}：导入控制器未注入任务中心面板（构造时传 panel=），因此无法访问面板内部状态。",
                name=name,
            )
        )


class ImportProgressDialog(QDialog):
    """导入进度控制器。

    通过主窗口右下角的共用任务中心面板显示进度，导入期间用户可以继续浏览。
    QDialog 提供 accept/reject 生命周期接口；控制器实例自身不调用 show/exec。

    控制器负责工作线程调度、主线程 SQLite 提交、产物清理和终止处理。
    """

    import_finished = Signal(list)  # 记录条目索引及可选的 Mod 对象。

    def __init__(
        self,
        items: list,
        node_id: str,
        mod_manager,
        parent: QWidget | None = None,
        panel=None,
    ) -> None:
        """在主线程捕获目标目录及原有名称，创建纳管服务和批次结果记录，并绑定可选任务面板。"""
        super().__init__(parent)
        self._items = items
        self._character_id = node_id
        self._mod_manager = mod_manager
        self._intake_service = ModIntakeService(mod_manager)
        self._worker: ImportWorkerThread | None = None
        self._completed_results: list = []
        self._was_terminated = False
        self._skipped_duplicates: int = 0
        # 单独记录失败项索引；重复项和失败项均可能返回空结果，不能由提交数量反推。
        self._failed_indexes: set[int] = set()
        self._ui_paused = False
        self._display_progress_value = 0
        self._pending_progress_value = 0
        self._pending_progress_text = ""
        self._pending_prepared_results: list[tuple[int, object]] = []
        # 已成功登记项的预览缓存路径（终止清理时须豁免，见 _cleanup_uncommitted_preview_caches）
        self._committed_preview_caches: set[str] = set()
        self._finish_pending = False
        self._cleanup_done = False
        # 预先在主线程中计算目标目录（涉及SQLite查询）
        self._dest_parent = ""
        try:
            self._dest_parent = str(mod_manager._get_character_library(node_id, create=False))
        except KeyError:
            # 目标缺失由任务面板报告，未捕获目标时不允许清理文件。
            pass
        # 记录导入前目录中已有的文件/文件夹名称，终止时不误删
        self._pre_existing_names: set[str] = set()
        dest_path = Path(self._dest_parent)
        if self._dest_parent and fs.is_dir(dest_path):
            self._pre_existing_names = {item.name for item in fs.list_children(dest_path)}
        self._panel = panel
        self._view = panel if panel is not None else _NoopView()
        self._setup_ui()

    def _setup_ui(self):
        """保持控制器窗口隐藏，绑定宿主任务中心的控制信号并初始化批次明细。"""
        # 本控制器自身永不显示：所有反馈都渲染到主窗口右下角的共用浮层。
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        if self._panel is None:
            return

        self._panel.pause_requested.connect(self._on_pause)
        self._panel.resume_requested.connect(self._on_resume)
        self._panel.stop_requested.connect(self._on_stop_clicked)

        self._panel.start_batch(
            len(self._items),
            tr("正在导入 {count} 个 Mod", count=len(self._items)),
            has_pause=True,
            stop_tooltip=tr("停止导入"),
            cancelled_title=tr("已停止导入"),
            done_text=tr("已导入"),
            pause_tooltip=tr("暂停导入"),
            resume_tooltip=tr("继续导入"),
        )
        self._panel.update_progress(0.0, tr("正在导入 {count} 个 Mod", count=len(self._items)), tr("准备中…"))
        for index, item in enumerate(self._items):
            source = item[0] if isinstance(item, (list, tuple)) and item else ""
            display = _item_label(item, source)
            self._panel.update_item(index, str(display), ST_QUEUED)

    def _release_panel(self) -> None:
        """断开与共用浮层的连线，避免下一批导入/纳管收到本批的回调。"""
        if self._panel is None:
            return
        for sig, slot in (
            (self._panel.pause_requested, self._on_pause),
            (self._panel.resume_requested, self._on_resume),
            (self._panel.stop_requested, self._on_stop_clicked),
        ):
            try:
                sig.disconnect(slot)
            except TypeError:
                pass
        self._panel = None
        self._view = _NoopView()

    def start_import(self):
        """在主线程规划请求后按配置选择串行或并行准备；目标已失效时为全部项目记录失败并结算。"""
        from src.core.config import Config

        parallel_workers = effective_archive_workers(Config().get("import.parallel_workers"))
        try:
            planned_items = self._plan_intake_requests()
        except IntakeTargetMissingError as exc:
            # 确认导入到启动 worker 之间目标也可能被删除；所有项共用这个目标。
            for index in range(len(self._items)):
                self._on_file_prepared(index, exc)
            self._on_finished()
            return

        # 多项导入统一可并行：直拷、解压和格式转换都属于独立文件 IO。
        use_parallel = len(planned_items) > 1 and parallel_workers > 1

        if use_parallel:
            self._worker = ParallelImportWorkerThread(
                planned_items,
                intake_service=self._intake_service,
                max_workers=parallel_workers,
                parent=self,
            )
        else:
            self._worker = ImportWorkerThread(
                planned_items,
                intake_service=self._intake_service,
                parent=self,
            )

        self._worker.progress_updated.connect(self._on_progress)
        self._worker.file_prepared.connect(self._on_file_prepared)
        self._worker.finished_all.connect(self._on_finished)
        self._worker.stage_progress.connect(self._on_stage_progress)
        # 将单项进度和当前步骤写入明细行。
        if hasattr(self._worker, "item_progress"):
            self._worker.item_progress.connect(self._on_item_progress)
        self._worker.start()

    def _on_item_progress(self, index: int, frac: float, text: str) -> None:
        """将单项处理说明及百分比写入对应明细行。"""
        if self._ui_paused:
            return
        pct = int(max(0.0, min(1.0, frac)) * 100)
        # 明细行已显示 Mod 名称，移除处理说明中的重复名称，为百分比保留空间。
        stage = (text or "").split(":", 1)[0].strip()
        label = f"{stage} {pct}%" if stage else f"{pct}%"
        self._mark_view_item(index, ST_RUNNING, label)

    def _plan_intake_requests(self) -> list[IntakeRequest]:
        """在主线程将输入元组转换为固定游戏和节点的纳管请求，仅透传允许的附加参数；目标失效异常交由启动流程处理。"""
        requests: list[IntakeRequest] = []
        for item in self._items:
            source_path = item[0]
            display_name = item[1] if len(item) >= 2 else ""
            selected_preview = item[2] if len(item) >= 3 else ""
            age_rating = item[3] if len(item) >= 4 else "G"
            # 可选第五元素提供允许透传的规划参数，使商店安装复用后台导入，
            # 同时携带 GameBanana 元数据、自动预览设置及强制年龄分级。
            extras = item[4] if len(item) >= 5 and isinstance(item[4], dict) else {}
            extras = {k: v for k, v in extras.items() if k in _PLAN_EXTRA_KEYS}
            requests.append(
                self._intake_service.plan_request(
                    source_path=source_path,
                    game_id=self._mod_manager.current_game_id,
                    node_id=self._character_id,
                    display_name=display_name,
                    selected_preview_name=selected_preview,
                    age_rating=(age_rating.value if hasattr(age_rating, "value") else str(age_rating)),
                    **extras,
                )
            )
        return requests

    def _relabel_stage(self, text: str) -> str:
        """在单项批次中，将阶段文案中的源文件名替换为明细行的显示名称。

        商店下载使用包含 MD5 和文件标识的缓存文件名，显示时改用 Mod 名称；
        普通导入保留源文件名。
        """
        if len(self._items) != 1 or ":" not in (text or ""):
            return text
        item = self._items[0]
        try:
            source = item[0] if item else ""
        except TypeError:
            return text
        head = text.split(":", 1)[0]
        return f"{head}: {_item_label(item, source)}"

    def _on_stage_progress(self, overall: float, text: str):
        """将任务的全局进度映射为 0 到 1000 的量程，并显示当前步骤。"""
        text = self._relabel_stage(text)
        val = max(0, min(1000, int(overall * 1000)))
        self._set_progress_value(val, text=text)
        if self._ui_paused:
            return
        self._view.update_progress(
            self._display_progress_value / 10.0,
            tr("正在导入… {value}%", value=self._display_progress_value // 10),
            text,
        )

    def _on_progress(self, current: int, total: int, detail: str):
        """把批次计数折算为单调总体进度，暂停时仅缓存；并行计数不用于推断单项索引。"""
        # 每个 Mod 起步 / 收尾的粗粒度进度（0~1000 量程）；阶段细节由 _on_stage_progress 驱动。
        value = int(current / total * 1000) if total > 0 else 0
        self._set_progress_value(value, text=detail)
        if self._ui_paused:
            return
        pct = self._display_progress_value // 10
        self._view.update_progress(
            self._display_progress_value / 10.0,
            tr("正在导入 {current}/{total} ({pct}%)", current=current, total=total, pct=pct),
            detail,
        )
        # 注意：并行 worker 的 current 是**完成计数**（_completed_count），不是条目索引，
        # 乱序完成时 current-1 会指到无关的行上。逐项行状态一律由 _on_file_prepared
        # 使用工作线程提供的条目索引更新状态。

    def _mark_view_item(self, index: int, status: str, text: str = "") -> None:
        """更新面板明细行；名称沿用 _setup_ui 登记时的显示名。"""
        if index < 0 or index >= len(self._items):
            return
        item = self._items[index]
        source = item[0] if isinstance(item, (list, tuple)) and item else ""
        display = _item_label(item, source)
        self._view.update_item(index, str(display), status, text)

    def _set_progress_value(self, value: int, *, text: str = "", force: bool = False) -> None:
        """进度只允许单调前进；暂停期间仅缓存，不重绘。"""
        value = max(0, min(1000, int(value)))
        if not force:
            value = max(self._display_progress_value, value)
        if self._ui_paused:
            self._pending_progress_value = max(self._pending_progress_value, value)
            if text:
                self._pending_progress_text = text
            return
        self._display_progress_value = value
        self._pending_progress_value = max(self._pending_progress_value, value)

    def _on_file_prepared(self, index: int, result):
        """在界面线程处理文件准备结果并确认数据库交接。

        暂停时缓存结果且不确认，终止后只释放等待；正常处理区分成功、重复和失败，再允许后台继续。
        """
        # 终止后仍可能收到已排队的 file_prepared 信号，此时禁止登记数据库，
        # 避免索引指向已清理的文件，或遗漏终止决策需要处理的已提交项。
        if self._was_terminated:
            if self._worker:
                self._worker.acknowledge_db_done()
            return
        # 暂停操作与已排队的文件完成信号可能交错，暂停期间先缓存结果，
        # 不提交数据库或发送确认；恢复后按原顺序提交并释放工作线程。
        if self._ui_paused:
            self._pending_prepared_results.append((index, result))
            return
        mod = None
        if result is not None:
            try:
                mod = self._register_mod_in_db(result)
            except DuplicateIntakeError as exc:
                result = exc
            except IntakeTargetMissingError as exc:
                result = exc
                logger.warning("导入跳过（目标目录已不存在）: %s", exc)
        self._completed_results.append((index, mod))
        # 逐项明细终态：登记成功 → 已纳管；重复 → 已回滚；其余异常 → 失败。
        if mod is not None:
            # 在明细中显示元数据降级，区别于源文件未提供信息的情况。
            notice = getattr(result, "metadata_notice", "")
            if notice:
                logger.warning("导入降级: %s", notice)
            self._mark_view_item(index, ST_DONE, tr("已导入·无元数据") if notice else tr("已导入"))
        elif isinstance(result, DuplicateIntakeError):
            self._mark_view_item(index, ST_ROLLED_BACK, tr("重复，已跳过"))
        else:
            self._failed_indexes.add(int(index))
            detail = tr("已跳过·目标目录已不存在") if isinstance(result, IntakeTargetMissingError) else tr("失败")
            self._mark_view_item(index, ST_FAILED, detail)
        # 完成一个 Mod → 进度推进到「已完成数 / 总数」（0~1000 量程）
        total = max(1, len(self._items))
        self._set_progress_value(int(len(self._completed_results) / total * 1000))
        # 通知工作线程可以继续下一个
        if self._worker:
            self._worker.acknowledge_db_done()

    def _register_mod_in_db(self, result: object) -> Mod | None:
        """提交有效准备结果并登记需保留的预览缓存。

        准备错误和普通提交失败返回空值，提交时的重复或目标失效异常继续抛出供明细区分。
        """
        if isinstance(result, DuplicateIntakeError):
            self._skipped_duplicates += 1
            logger.info("跳过重复 Mod: %s", result)
            return None
        if isinstance(result, Exception):
            logger.error("统一纳管准备失败: %s", result)
            return None
        if not isinstance(result, PreparedIntake):
            logger.error("导入 worker 返回了无效结果: %r", type(result).__name__)
            return None
        try:
            mod = self._intake_service.commit(result)
        except DuplicateIntakeError as exc:
            self._skipped_duplicates += 1
            logger.info("跳过重复 Mod: %s", exc)
            raise  # 保留实际结果，用于明细展示和失败计数。
        except IntakeTargetMissingError:
            raise  # 保留失败原因，交给逐项明细展示；commit 已完成回滚。
        except Exception as exc:
            logger.error("统一纳管提交失败: %s", exc)
            return None
        # 记录已提交项目的预览缓存路径，终止清理时保留这些仍被资源引用的文件。
        if mod is not None and result.preview_cache_path is not None:
            try:
                self._committed_preview_caches.add(fs.normal_path(result.preview_cache_path))
            except Exception:
                pass
        return mod

    def _on_finished(self):
        """结算正常批次并清理未提交产物，暂停时延后结算，终止批次由单独流程收尾。

        同时尝试清除应用数据目录下的 temp 目录；面板完成状态依据实际提交、重复及失败记录生成。
        """
        if self._ui_paused and not self._was_terminated:
            self._finish_pending = True
            return
        # 结束时将进度设为量程上限，避免浮点取整导致未满。
        self._set_progress_value(1000, force=True)
        # 清理临时目录（仅在全部导入完成后清理一次）
        temp_dir = get_data_dir() / "temp"
        if fs.is_dir(temp_dir):
            try:
                fs.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass
        if self._was_terminated:
            return  # 由终止处理统一完成。
        # 正常结束也清一次本轮未成功登记的产物（例如 DB 写入失败）。只依据 worker
        # 精确记录的本轮路径，不扫描/误删用户并发放进目录的其他文件。
        self._cleanup_orphaned_files(self._completed_keep_names())
        self._cleanup_uncommitted_preview_caches()
        # 显示跳过的重复 Mod 数量
        imported_count = self._committed_count()
        failed_count = len(self._failed_indexes)
        # 顶层状态须反映明细中的失败，避免失败批次显示成功并自动收起。
        if failed_count:
            parts = [
                tr("已导入 {imported_count} 个", imported_count=imported_count),
                tr("{failed_count} 个失败", failed_count=failed_count),
            ]
            if self._skipped_duplicates > 0:
                parts.insert(1, tr("跳过 {skipped_duplicates} 个重复", skipped_duplicates=self._skipped_duplicates))
            self._view.finish(tr("导入完成，但有失败项"), "，".join(parts), ok=False)
        elif self._skipped_duplicates > 0:
            self._view.finish(
                tr("导入完成"),
                tr(
                    "已导入 {imported_count} 个，跳过 {skipped_duplicates} 个重复",
                    imported_count=imported_count,
                    skipped_duplicates=self._skipped_duplicates,
                ),
                ok=True,
            )
        else:
            self._view.finish(
                tr("导入完成"), tr("已导入 {imported_count} 个 Mod", imported_count=imported_count), ok=True
            )
        self._release_panel()
        self.import_finished.emit(self._completed_results)
        self.accept()

    def _committed_count(self) -> int:
        """返回已成功登记入库的数量，供任务面板和终止提示共同使用。"""
        return sum(1 for _idx, mod in self._completed_results if mod is not None)

    def _on_pause(self):
        """冻结界面提交和进度展示，再请求工作线程暂停，确保确认期间已提交数量稳定。"""
        self._ui_paused = True
        if self._worker:
            self._worker.pause()
        self._view.set_paused(True)
        done = self._committed_count()
        self._view.update_progress(
            self._display_progress_value / 10.0,
            tr("导入已暂停 · 已导入 {done}/{count} 个", done=done, count=len(self._items)),
            tr("选择“继续导入”恢复任务，或选择“停止导入”。"),
        )

    def _on_resume(self):
        """先处理暂停期间缓存的准备结果，再恢复后台执行，并补发挂起的批次完成处理。"""
        self._ui_paused = False
        self._set_progress_value(self._pending_progress_value)
        # 后台仍保持暂停，先处理缓存的主线程登记结果；确认只释放数据库等待。
        # 缓存处理完成后再解除暂停事件，恢复文件操作。
        pending, self._pending_prepared_results = self._pending_prepared_results, []
        for index, result in pending:
            self._on_file_prepared(index, result)
        if self._worker:
            self._worker.resume()
        self._view.set_paused(False)
        self._view.update_progress(
            self._display_progress_value / 10.0,
            tr("正在导入… {value}%", value=self._display_progress_value // 10),
            self._pending_progress_text or "",
        )
        if self._finish_pending:
            self._finish_pending = False
            self._on_finished()

    def _on_stop_clicked(self):
        """先暂停工作线程，再显示终止确认。

        QMessageBox 使用嵌套事件循环；暂停后可避免 file_prepared 在确认期间修改
        _completed_results，使弹窗前计算的 imported_count 与实际提交状态保持一致。
        """
        if not self._ui_paused:
            self._on_pause()
        self._on_terminate()

    def _on_terminate(self):
        """取得保留或撤销已导入项目的决定后请求终止，并安排延后清理；选择继续则恢复任务。"""
        if not self._worker:
            return

        imported_count = self._committed_count()
        total_count = len(self._items)
        remaining = total_count - imported_count

        # 先获取用户的保留或撤销决策，再调用 terminate_import，使取消确认不改变任务状态。
        cancel_all = False
        if imported_count == 0:
            cancel_all = True
        else:
            # 确认框使用可见宿主作为父窗口，保证居中和窗口堆叠关系正确。
            msg = QMessageBox(self.parentWidget())
            msg.setWindowTitle(tr("终止导入"))
            msg.setText(
                tr(
                    "已导入 {imported_count} 个 Mod，还有 {remaining} 个未导入。\n\n请选择操作：",
                    imported_count=imported_count,
                    remaining=remaining,
                )
            )
            cancel_all_btn = msg.addButton(tr("取消全部导入"), QMessageBox.ButtonRole.DestructiveRole)
            msg.addButton(tr("保留已导入的Mod"), QMessageBox.ButtonRole.AcceptRole)
            resume_btn = msg.addButton(tr("继续导入"), QMessageBox.ButtonRole.RejectRole)
            msg.setDefaultButton(resume_btn)
            msg.exec()
            clicked = msg.clickedButton()
            if clicked is resume_btn:
                # 取消停止操作后恢复工作线程。
                if self._ui_paused:
                    self._on_resume()
                return
            cancel_all = clicked is cancel_all_btn

        self._was_terminated = True
        self._worker.terminate_import()

        # 存储决策，延迟到工作线程结束后执行
        self._terminate_cancel_all = cancel_all
        # 暂停期间已经复制完成但尚未登记的结果不属于“已导入”；终止后统一按未提交产物清理。
        self._pending_prepared_results.clear()

        # 更新 UI 为"正在停止"状态
        self._view.set_stop_enabled(False)
        self._view.update_progress(
            self._display_progress_value / 10.0,
            tr("正在停止导入…"),
            tr("正在等待后台任务结束…"),
        )

        # 工作线程结束后才能清理文件。先连接完成信号，再主动检查一次结束状态，
        # 覆盖线程在确认对话框的嵌套事件循环中已经完成的情况。
        # finished 发出与 isRunning 更新可能不同步，清理方法通过 _cleanup_done 保证幂等。
        self._worker.finished.connect(self._do_deferred_cleanup)
        if not self._worker.isRunning():
            self._do_deferred_cleanup()
        else:
            # 五秒后再次尝试幂等清理，处理工作线程结束信号未到达的情况。
            call_later(5000, self, self._do_deferred_cleanup)

    def _do_deferred_cleanup(self):
        """幂等处理终止后的保留或撤销决定并报告结果；调用方须确保后台文件操作已经结束。"""
        if self._cleanup_done:
            return
        self._cleanup_done = True
        cancel_all = getattr(self, "_terminate_cancel_all", False)

        if cancel_all:
            # 删除已导入的Mod（DB记录 + 物理文件）
            removed = 0
            for idx, mod in self._completed_results:
                if mod:
                    try:
                        self._mod_manager.delete_mod(mod.mod_id)
                        removed += 1
                    except Exception:
                        pass
            self._cleanup_orphaned_files(set())
            self._cleanup_uncommitted_preview_caches()
            self._view.finish_cancelled(
                tr("已取消全部导入，回滚 {removed} 个已导入的 Mod", removed=removed)
                if removed
                else tr("已取消全部导入，未提交内容已清理")
            )
            self._release_panel()
            self.import_finished.emit([])
        else:
            keep_paths = self._completed_keep_names()
            self._cleanup_orphaned_files(keep_paths)
            self._cleanup_uncommitted_preview_caches()
            kept = self._committed_count()
            self._view.finish_cancelled(
                tr("已停止导入，保留已导入的 {kept} 个 Mod", kept=kept) if kept else tr("已停止导入，未提交内容已清理")
            )
            self._release_panel()
            self.import_finished.emit(self._completed_results)

        self.reject()

    def _completed_keep_names(self) -> set[str]:
        """收集已提交项目应保留的顶层名称，有容器标记时保留父容器名，否则保留内容自身名称。"""
        keep_paths: set[str] = set()
        for _idx, mod in self._completed_results:
            if not mod or not mod.path:
                continue
            body = Path(fs.normal_path(mod.path))
            try:
                from src.core.mod_package import MOD_CONTAINER_MARKER

                if fs.is_file(body.parent / MOD_CONTAINER_MARKER):
                    keep_paths.add(body.parent.name)
                    continue
            except Exception:
                pass
            keep_paths.add(body.name)
        return keep_paths

    def _cleanup_orphaned_files(self, keep_names: set[str]) -> None:
        """仅清理工作线程登记的本批容器路径。

        跳过导入前已有或明确保留的名称，拒绝目标目录外的路径；目标未捕获或已不存在时不执行清理。
        """
        if not self._dest_parent:
            return
        dest = Path(fs.normal_path(self._dest_parent))
        if not fs.is_dir(dest):
            return
        # 合并：导入前已存在的 + 明确要保留的
        safe_names = self._pre_existing_names | keep_names
        candidates = self._worker.get_created_paths() if self._worker is not None else ()
        for item in candidates:
            item = Path(fs.normal_path(item))
            if item.name in safe_names:
                continue
            # 即使任务返回异常路径，也不允许越出本次目标目录。
            try:
                if os.path.commonpath([fs.native_path(dest), fs.native_path(item)]) != fs.native_path(dest):
                    logger.warning("拒绝清理目标目录外路径: %s", item)
                    continue
            except ValueError:
                continue
            if _remove_path_reliably(item):
                logger.info("已清理未提交的导入产物: %s", item)
            else:
                logger.warning("导入残留仍存在: %s", item)

    def _cleanup_uncommitted_preview_caches(self) -> None:
        """清理未提交项的预览缓存。

        保留路径来自提交时记录的 PreparedIntake.preview_cache_path，
        不根据名称推导或调用带目录创建副作用的路径辅助函数。
        """
        get_caches = getattr(self._worker, "get_created_preview_caches", None)
        if not callable(get_caches):
            return
        keep = self._committed_preview_caches
        for cache in get_caches():
            if not fs.exists(cache):
                continue
            if fs.normal_path(cache) in keep:
                continue  # 已成功登记 —— 用户保留的 Mod 靠它显示预览图
            if _remove_path_reliably(cache):
                logger.info("已清理未提交的预览缓存: %s", cache)
            else:
                logger.warning("预览缓存残留仍存在: %s", cache)

    def closeEvent(self, event):
        """后台线程仍运行时拒绝窗口关闭，任务结束后交由 Qt 处理。"""
        if self._worker and self._worker.isRunning():
            event.ignore()
        else:
            super().closeEvent(event)
