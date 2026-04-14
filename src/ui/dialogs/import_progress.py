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
