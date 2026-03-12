"""GameBanana 商店页面。

浏览视图与未收录提示共用堆叠布局，错误提示和右侧详情使用独立浮层。
详情的位置与尺寸由 _reposition_detail_view 管理，不占用浏览网格的布局空间。
主窗口通过 set_game(local_game_id) 同步当前游戏。
"""

from __future__ import annotations

import asyncio
import threading
import weakref
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path

import shiboken6
from PySide6.QtCore import QEvent, QMetaMethod, QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.core.config import Config
from src.core.game_config_store import GameConfigStore
from src.core.mod_index import ModIndex
from src.models.mod import AgeRating
from src.services.gamebanana.cache import ImageCache
from src.services.gamebanana.category_service import GbCategoryService
from src.services.gamebanana.client import GameBananaClient
from src.services.gamebanana.constants import (
    gb_override_key,
    official_name_from_records,
    pick_gb_game_match,
    resolve_gb_game_id_by_name,
)
from src.services.gamebanana.downloader import GbDownloadManager
from src.services.gamebanana.errors import friendly_error
from src.services.gamebanana.image_loader import ImageLoader
from src.services.gamebanana.install_resolver import InstallTargetResolver
from src.services.gamebanana.models import (
    GbCategory,
    GbInstallMetadata,
    GbModFile,
    GbModProfile,
    GbModRecord,
)
from src.services.gamebanana.pending_install_store import (
    PendingInstall,
    PendingInstallStore,
    RestoredFile,
    RestoredProfile,
)
from src.ui import anim, theme
from src.ui.widgets.gb_browse_view import GbBrowseView
from src.ui.widgets.gb_download_flyout import GbDownloadFlyout
from src.ui.widgets.gb_lightbox import GbLightBox
from src.ui.widgets.gb_mod_card import GbModCard
from src.ui.widgets.gb_mod_detail_view import GbModDetailView
from src.ui.widgets.gb_unknown_game_view import GbUnknownGameView
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.qt_timers import call_later


def _is_shutdown_noise(exc: BaseException | None) -> bool:
    """识别关闭连接池和事件循环时产生的预期异常。

    客户端关闭后，尚未结束的任务可能因资源释放或取消而失败；
    异常处理器据此区分预期关闭结果与需要保留堆栈的错误。
    """
    if exc is None:
        return False
    name = type(exc).__name__
    if name in {"ClosedResourceError", "CancelledError", "GeneratorExit"}:
        return True
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        if "closed" in msg or "loop is closed" in msg:
            return True
    return False


class _AsyncWorker:
    """在单个守护线程的事件循环中复用 httpx 连接池。

    关闭时先取消并等待所有待处理任务，再关闭客户端和事件循环。
    异常处理器负责收集关闭期间未被任务调用方消费的异常。
    """

    def __init__(self) -> None:
        """创建商店共享事件循环线程，并等待循环就绪后允许提交请求。"""
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._shutting_down = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        """在线程中绑定事件循环与异常处理器，通知就绪后持续处理异步任务。"""
        asyncio.set_event_loop(self._loop)
        # 统一处理 asyncio 未消费的异常和待处理任务告警；关闭期间的预期异常静默处理，
        # 其他异常保留默认处理器的堆栈输出。
        self._loop.set_exception_handler(self._on_loop_exception)
        self._ready.set()
        self._loop.run_forever()

    def _on_loop_exception(self, loop, context: dict) -> None:
        """忽略关闭期间的预期异常，其他未消费异常交给默认处理器保留堆栈。"""
        exc = context.get("exception")
        if self._shutting_down or _is_shutdown_noise(exc):
            # 关闭期间的取消和资源释放异常无需记录。
            return
        # 其他异常交给默认处理器，保留 asyncio 的堆栈信息。
        loop.default_exception_handler(context)

    def submit(self, coro_factory: Callable[[], Awaitable]) -> Future:
        """在共享事件循环内创建并运行协程，返回供外部线程等待或取消的 Future。"""

        async def _runner():
            """在所属事件循环中调用协程工厂并转发结果。"""
            return await coro_factory()

        return asyncio.run_coroutine_threadsafe(_runner(), self._loop)

    def shutdown(self) -> None:
        """设置关闭状态并限时取消在途任务，再请求停止线程和关闭事件循环。"""
        # 先设置关闭标志，再取消并等待任务，最后停止事件循环。
        # 此顺序确保取消引起的 ClosedResourceError 按关闭期间的预期异常处理。
        self._shutting_down = True
        if self._loop.is_closed():
            return

        # 在事件循环内取消其他任务，并等待取消流程结束。
        async def _drain() -> None:
            """取消共享循环中除当前清理任务之外的协程，并收集其结束结果。"""
            current = asyncio.current_task()
            tasks = [t for t in asyncio.all_tasks() if t is not current]
            for t in tasks:
                t.cancel()
            if tasks:
                # 收集各任务取消产生的异常，避免异常未被消费的 asyncio 告警。
                await asyncio.gather(*tasks, return_exceptions=True)

        try:
            fut = asyncio.run_coroutine_threadsafe(_drain(), self._loop)
            try:
                fut.result(timeout=3.0)
            except Exception:
                # 等待取消超时或失败时，仍继续停止事件循环。
                pass
        except RuntimeError:
            # 提交取消任务前事件循环已关闭。
            pass

        # 停止事件循环并等待工作线程退出。
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            pass
        self._thread.join(timeout=2.0)
        try:
            self._loop.close()
        except Exception:
            pass


class _AsyncRunner(QObject):
    """将后台 Future 转换为界面线程信号，通过退场状态阻止销毁或取消后的迟到结果。"""

    finished = Signal(object)
    failed = Signal(str)
    retired = Signal()
    _completed = Signal(object)

    # 关闭过程中的预期网络异常只记录简要信息。
    # 使用异常类名避免界面调度器直接依赖底层网络实现。
    _SHUTDOWN_EXPECTED_EXC_NAMES = frozenset(
        {
            "ClosedResourceError",  # anyio：关闭后的数据流仍被访问。
            "CancelledError",  # asyncio：事件循环停止时取消任务。
            "ReadError",  # httpx：连接中断。
            "WriteError",  # httpx：写入中断。
            "PoolTimeout",  # httpx：连接池关闭后超时
            "RemoteProtocolError",  # httpx：服务端在握手中断开
        }
    )

    def __init__(self, worker: _AsyncWorker, parent: QObject | None = None) -> None:
        """绑定共享异步工作器，初始化结果与退场状态，并通过排队连接接收完成通知。"""
        super().__init__(parent)
        self._worker = worker
        self._future = None
        # 页面关闭前设置此标志，将随后到达的预期任务异常记录为调试信息。
        self._silent_on_failure = False
        self._retired = False
        self._completed.connect(self._on_completed, Qt.ConnectionType.QueuedConnection)

    def run(self, coro_factory: Callable[[], Awaitable]) -> None:
        """提交协程并登记弱引用完成回调，对象销毁时取消任务，避免后台结果访问无效 Qt 对象。"""
        if self._retired:
            return
        future = self._worker.submit(coro_factory)
        self._future = future
        runner_ref = weakref.ref(self)

        def _on_destroyed(_=None) -> None:
            """对象销毁时标记退场并取消对应 Future，阻止后续结果发布。"""
            runner = runner_ref()
            if runner is not None:
                runner._retired = True
                runner._future = None
            future.cancel()

        self.destroyed.connect(_on_destroyed)

        def _on_done(fut: Future) -> None:
            """后台 Future 完成后仅向仍有效的运行器发送排队信号。"""
            # 异步结果可能晚于任务对象和页面的销毁。
            runner = runner_ref()
            if runner is None or runner._retired or (not shiboken6.isValid(runner)):
                return
            try:
                runner._completed.emit(fut)
            except RuntimeError:
                # 对象可能在弱引用检查后立即销毁。
                pass

        future.add_done_callback(_on_done)

    def _retire(self) -> bool:
        """仅执行一次退场，清除 Future 引用并通知页面更新请求计数，再延后销毁对象。"""
        if self._retired:
            return False
        self._retired = True
        self._future = None
        self.retired.emit()
        self.deleteLater()
        return True

    def _on_completed(self, future: Future) -> None:
        """在界面线程先使运行器退场，再根据关闭、取消和异常状态选择发布结果。"""
        # 界面投递时重新检查取消状态，先撤销请求再由处理器读取加载计数。
        if not self._retire():
            return
        if self._silent_on_failure or self._worker._shutting_down or future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            if type(exc).__name__ not in self._SHUTDOWN_EXPECTED_EXC_NAMES:
                logger.error("Async error in _AsyncRunner", exc_info=(type(exc), exc, exc.__traceback__))
            self.failed.emit(friendly_error(exc))
            return
        self.finished.emit(future.result())

    def cancel(self) -> None:
        """导航切换时取消在途请求，释放客户端的并发名额并停止重试。

        run_coroutine_threadsafe 返回的 Future 将取消请求传播到事件循环内的任务。
        协程在 await 处收到 CancelledError，随后关闭请求并释放 semaphore；取消不作为错误上报。
        """
        self._silent_on_failure = True
        fut = getattr(self, "_future", None)
        self._retire()
        if fut is not None:
            try:
                fut.cancel()
            except Exception:
                pass


class _DetailDragHandle(QFrame):
    """详情浮层左侧的宽度拖动手柄。

    手柄宽六像素，悬停时使用水平调整光标，拖动时报告宽度增量。
    释放信号通知上层保存宽度，或在宽度过小时收起浮层。
    """

    drag_delta = Signal(int)  # 实时拖动；正值 = 浮窗变宽
    drag_released = Signal()  # 拖动结束（释放鼠标）

    HANDLE_WIDTH = 6

    def __init__(self, parent=None) -> None:
        """创建透明的详情宽度调整手柄，设置水平拖动光标并初始化拖动状态。"""
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        # 手柄保持透明，仅通过水平调整光标提示拖动能力。
        self.setStyleSheet("background-color: transparent;")
        self.setFixedWidth(self.HANDLE_WIDTH)
        self._dragging = False
        self._last_global_x = 0

    def enterEvent(self, event):
        """保留基类鼠标进入处理，使透明手柄继续使用水平调整光标。"""
        # 保留透明外观及水平调整光标。
        super().enterEvent(event)

    def leaveEvent(self, event):
        """非拖动状态下恢复手柄透明样式，再交由基类处理离开事件。"""
        if not self._dragging:
            self.setStyleSheet("background-color: transparent;")
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        """左键按下时记录全局横坐标并开始详情宽度拖动。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._last_global_x = int(event.globalPosition().x())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """将全局横向位移转换为详情宽度增量，向左拖动时增加宽度。"""
        if self._dragging:
            cur = int(event.globalPosition().x())
            dx = cur - self._last_global_x
            self._last_global_x = cur
            # 手柄位于详情左侧，向左拖动时增加详情宽度。
            self.drag_delta.emit(-dx)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """左键释放时结束拖动并通知宿主结算宽度，必要时恢复手柄样式。"""
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            # 拖动期间不处理 leaveEvent，释放后需根据鼠标位置恢复手柄状态。
            if not self.underMouse():
                self.setStyleSheet("background-color: transparent;")
            self.drag_released.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class GameBananaStorePage(QWidget):
    """组合浏览视图、右侧详情浮层与未收录提示的商店页面。"""

    VIEW_MAIN = 0
    VIEW_UNKNOWN = 1

    DETAIL_WIDTH = 480
    # 拖动低于此宽度时收起详情。阈值不得小于 GbModDetailView 的最小宽度 360，
    # 否则 Qt 的最小尺寸约束会阻止触发收起条件。
    DETAIL_MIN_WIDTH = 360

    open_settings_requested = Signal()  # 由主窗口切换到游戏设置。
    open_mod_in_overview_requested = Signal(int)  # 传递在线 Mod 编号，由主窗口在总览中定位。
    open_local_mod_in_overview_requested = Signal(str)
    # 下载完成且目标角色已确定后，由主窗口通过后台导入流程安装。
    # payload 在 _after_download 中组装；宿主完成后须回调 apply_install_result。
    mod_install_requested = Signal(object)

    def __init__(
        self,
        *,
        client: GameBananaClient,
        image_loader: ImageLoader,
        local_game_id: str | None = None,
        mod_index: ModIndex | None = None,
        download_manager: GbDownloadManager | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """接入在线查询、图片和下载服务，恢复待安装引用，构建浏览与详情浮层并连接异步生命周期。"""
        super().__init__(parent)
        self._client = client
        self._loader = image_loader
        self._mod_index = mod_index
        self._download_manager = download_manager or GbDownloadManager()
        self._config = Config()
        self._game_store = GameConfigStore()
        self._cat_service = GbCategoryService(client)

        self._gb_game_id: int | None = None
        # 记录本次运行已刷新过的游戏；首次进入时清除 API 缓存，后续切换复用缓存。
        # 手动刷新始终重新获取数据，图像磁盘缓存独立管理。
        self._session_freshed_games: set[int] = set()
        self._local_game_name: str = ""
        # 下载期间切换游戏时暂存安装请求，返回原游戏后继续。
        # 持久化安装所需的标量信息，支持重启后恢复。
        self._pending_installs = PendingInstallStore()
        for pending in self._pending_installs.records():
            if Path(pending.archive_path).is_file():
                self._retain_pending_install(pending)
        # 记录已连接信号的 file_id。队列复用同一任务对象，重复连接会触发多次安装。
        self._wired_download_tasks: set[int] = set()
        # 当前请求代次内已补全下载量的 Mod 编号，用于合并重复请求。
        self._dl_asked: set[int] = set()
        self._dl_asked_gen: int = -1
        self._display_name: str = ""
        self._path: list[GbCategory] = []
        self._mode = "category"  # "category" | "search"
        self._search_query = ""
        self._sort = self._config.get("gamebanana.default_sort", "best_match")
        self._nsfw_policy = self._config.get("gamebanana.nsfw_policy", "blur")
        self._page = 1
        self._has_more = False
        self._backfill_attempts = 0
        self._backfill_max = int(self._config.get("gamebanana.infinite_scroll_max_backfill", 5))
        self._min_per_load = int(self._config.get("gamebanana.infinite_scroll_min_per_load", 10))
        self._request_gen = 0
        # 连续点击或切换标签可能重复触发 set_game / _set_path_and_query。
        # 通过未完成请求数判断加载状态，避免页面在没有待处理请求时仍显示加载中。
        self._in_flight = 0
        # 保留未过滤的记录；NSFW 策略（remove/blur/show）变更时据此重建网格，
        # 使已被 remove 策略过滤的记录也能在切换回 show 时恢复显示。
        self._all_records: list[GbModRecord] = []
        # 一次全库扫描同时缓存卡片徽章所需的 mod_id 集合，以及详情文件行所需的
        # (mod_id, file_id) / (mod_id, md5) 集合，避免逐卡查询数据库。
        # 安装或删除事件将缓存置为 None；按需创建卡片时重新计算，确保读取当前安装状态。
        self._installed_cache: tuple[set, set, set] | None = None
        # 保存选中的 Mod 编号，供详情关闭、游戏切换及卡片重建时同步选中状态。
        self._selected_mod_id: int | None = None
        # 初始游戏请求延迟至商店页 showEvent，确保主线程已完成 _init_data 和信号连接。
        self._pending_initial_game_id: str | None = None
        self._async_worker = _AsyncWorker()
        self._runners: list[_AsyncRunner] = []
        self._shutdown_started = False
        page_ref = weakref.ref(self)
        worker = self._async_worker

        def _on_store_destroyed(_=None) -> None:
            """页面销毁时关闭其资源；Python 页面对象已回收时直接关闭仍保留的异步工作器。"""
            page = page_ref()
            if page is not None:
                page.shutdown()
            else:
                worker.shutdown()

        self.destroyed.connect(_on_store_destroyed)

        # 浏览视图与未收录提示共用堆叠布局。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # 错误提示以页面浮层显示，不参与布局分配；五秒后自动收起。
        self.error_bar = QFrame(self)
        self.error_bar.setObjectName("gbErrorBar")
        self.error_bar.setStyleSheet(
            f"#gbErrorBar {{ background-color: {theme.rgba('bg.card', 0.97)};"
            f" border: 1px solid {theme.color('stroke.card')}; border-radius: 10px; }}"
            f"#gbErrorBar QLabel {{ color: {theme.color('warning')}; background: transparent; }}"
        )
        self.error_bar.setFixedHeight(40)
        err_layout = QHBoxLayout(self.error_bar)
        err_layout.setContentsMargins(16, 6, 16, 6)
        self.error_label = QLabel("")
        self.error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        err_layout.addWidget(self.error_label, 1)
        self.error_bar.hide()
        self._error_hide_timer = QTimer(self)
        self._error_hide_timer.setSingleShot(True)
        self._error_hide_timer.timeout.connect(self._roll_up_error_bar)

        self.stack = QStackedWidget()
        # 浏览视图占满页面，详情浮层覆盖其上，不参与布局尺寸分配。
        self.browse_view = GbBrowseView()
        # 未收录提示占满页面。
        self.unknown_view = GbUnknownGameView()
        self.stack.addWidget(self.browse_view)
        self.stack.addWidget(self.unknown_view)
        outer.addWidget(self.stack, 1)

        # 详情作为页面子控件独立于布局，保持在堆叠视图上方。
        # 页面尺寸变化或手柄拖动时，由 _reposition_detail_view 更新几何位置。
        self.detail_view = GbModDetailView(
            image_loader=image_loader,
            has_local_md5=self._has_local_md5,
            # 按具体文件判断安装状态，同一 Mod 的其他下载文件仍可单独安装。
            is_file_installed=self._is_gb_file_installed,
            # 详情视图切到「更新」Tab 且 profile.updates 为空时，由本回调异步拉取
            # apiv11 /Updates 端点。
            updates_fetcher=self._fetch_updates_for_profile,
            parent=self,
        )
        self.detail_view.hide()  # 选择卡片后显示详情。

        # 手柄与详情互为兄弟控件，紧贴详情左边缘。
        self._detail_handle = _DetailDragHandle(self)
        self._detail_handle.hide()
        self._detail_handle.drag_delta.connect(self._on_detail_resize_drag)
        self._detail_handle.drag_released.connect(self._on_detail_drag_released)

        # 启动时恢复保存的浮层宽度，并限制为有效最小值；拖动低于最小值时收起浮层。
        self._detail_width = max(
            self.DETAIL_MIN_WIDTH, int(Config.instance().get("ui.store_detail_width", self.DETAIL_WIDTH))
        )
        self._detail_min_width = self.DETAIL_MIN_WIDTH
        # 拖动期间暂停外部点击检测，避免将鼠标释放误判为收起详情的操作。
        self._detail_resizing = False

        # 公开子控件入口，供主窗口访问搜索框等控件。
        self.filter_bar = self.browse_view.filter_bar
        self.breadcrumb = self.browse_view.breadcrumb
        self.grid = self.browse_view.grid
        self.empty_state = self.browse_view.empty_state
        self.loading_state = self.browse_view.loading_state

        # 独立灯箱和下载浮窗以主窗口为父对象，避免 Qt 将页面祖先提升为原生窗口，
        # 导致控件层次与系统窗口命中顺序不一致。
        self.lightbox = GbLightBox(image_loader=image_loader, parent=self.window())
        self.lightbox.hide()
        self.download_flyout = GbDownloadFlyout(self._download_manager, parent=self.window())
        self.download_flyout.hide()

        # 连接交互信号。
        self.filter_bar.search_submitted.connect(self._on_search)
        self.filter_bar.sort_changed.connect(self._on_sort_changed)
        self.filter_bar.nsfw_policy_changed.connect(self._on_nsfw_policy_changed)
        self.filter_bar.dl_manager_btn.clicked.connect(self._toggle_flyout)
        self.filter_bar.refresh_requested.connect(self.refresh)
        self.breadcrumb.path_changed.connect(self._on_breadcrumb_path_changed)
        self.breadcrumb.children_requested.connect(self._on_children_requested)
        self.grid.request_next_page.connect(self._fetch_next_page)
        # 浏览网格滚动时节流更新详情背景的模糊快照。
        self._detail_backdrop_timer = QTimer(self)
        self._detail_backdrop_timer.setSingleShot(True)
        self._detail_backdrop_timer.timeout.connect(self._refresh_detail_backdrop)
        try:
            self.grid.scroll_area.verticalScrollBar().valueChanged.connect(lambda _v: self._kick_detail_backdrop())
        except Exception:
            pass
        self.detail_view.back_requested.connect(self._on_back_to_browse)
        self.detail_view.file_download_requested.connect(self._on_download_requested)
        self.detail_view.file_install_requested.connect(self._on_install_requested)
        self.detail_view.preview_clicked.connect(self._show_lightbox)
        self.unknown_view.open_settings_requested.connect(self.open_settings_requested.emit)
        self.unknown_view.open_gamebanana_requested.connect(self._open_gamebanana_website)
        self.download_flyout.mod_open_requested.connect(self.open_mod_in_overview_requested.emit)
        self.download_flyout.file_open_requested.connect(self._open_downloaded_file)
        self.download_flyout.can_open_mod = self._can_open_downloaded_mod
        self.download_flyout._refresh()

        # 下载按钮显示完成度最低的活动任务进度，队列和任务变化时重新计算。
        self._download_manager.queue_changed.connect(self._update_dl_button_progress)
        self._download_manager.task_added.connect(self._on_dl_task_added)
        self._download_manager.task_removed.connect(lambda _fid: self._update_dl_button_progress())
        # 任务删除后移除连接记录，以便再次下载该文件时连接新任务的信号。
        self._download_manager.task_removed.connect(self._on_download_task_removed)
        for _t in self._download_manager.active():
            self._hook_dl_task_signals(_t)
        self._update_dl_button_progress()

        # 初始网络加载延迟至页面首次可见，避免与主窗口初始化竞争或提前请求未使用的页面。
        if local_game_id:
            self._pending_initial_game_id = local_game_id

    # 首次可见时启动
    def prime_initial_game(self, local_game_id: str) -> None:
        """记录启动恢复的游戏，在商店首次可见时再加载，避免提前请求或覆盖已有页面状态。"""
        if local_game_id and self._gb_game_id is None:
            self._pending_initial_game_id = local_game_id

    def showEvent(self, event):
        """首次可见时延后加载待选游戏，重定位详情并安装页面可见期间的外部点击过滤器。"""
        super().showEvent(event)
        # 处理启动恢复或页面隐藏期间切换游戏留下的待加载请求。
        # 远程游戏编号为空时才加载，避免标签往返切换重复发送请求。
        if self._pending_initial_game_id is not None and self._gb_game_id is None:
            pending, self._pending_initial_game_id = self._pending_initial_game_id, None
            # 延迟到下一次事件循环执行，使页面先完成首次绘制。
            call_later(0, self, lambda gid=pending: self.set_game(gid))
        # 重新显示页面时，按当前尺寸定位仍可见的浮层。
        try:
            if self.detail_view.isVisible():
                self._reposition_detail_view()
        except RuntimeError:
            pass
        # 商店页面可见时监听外部点击，以便收起详情浮层。
        try:
            QApplication.instance().installEventFilter(self)
        except Exception:
            pass

    def resizeEvent(self, event):
        """页面尺寸变化后重定位可见详情和错误浮层。"""
        super().resizeEvent(event)
        # 页面尺寸变化时重新定位可见浮层，保持边缘和宽度一致。
        try:
            if self.detail_view.isVisible():
                self._reposition_detail_view()
        except RuntimeError:
            pass
        self._reposition_error_bar()

    def eventFilter(self, obj, event):
        """处理点击详情浮层外部的鼠标事件。

        浮层不可见、正在拖动或点击浮层及手柄时不处理。
        点击 Mod 卡片由卡片切换内容，其他页面内区域的点击收起浮层。
        """
        try:
            if (
                event.type() != QEvent.Type.MouseButtonPress
                or not self.detail_view.isVisible()
                or self._detail_resizing
            ):
                return super().eventFilter(obj, event)
            try:
                gpos = event.globalPosition().toPoint()
            except AttributeError:
                gpos = event.globalPos()
            # 独立图片查看器、灯箱、菜单及对话框中的点击不属于页面外部点击，避免误收起详情。
            try:
                w_at = QApplication.widgetAt(gpos)
                if w_at is not None and w_at.window() is not self.window():
                    return super().eventFilter(obj, event)
            except Exception:
                pass
            # 浮窗自身
            d_rect = QRect(self.detail_view.mapToGlobal(QPoint(0, 0)), self.detail_view.size())
            if d_rect.contains(gpos):
                return super().eventFilter(obj, event)
            # 拖动手柄内部的点击。
            h_rect = QRect(self._detail_handle.mapToGlobal(QPoint(0, 0)), self._detail_handle.size())
            if h_rect.contains(gpos):
                return super().eventFilter(obj, event)
            # 卡片点击会切换详情内容，无需先隐藏浮层，以免闪烁。
            try:
                w = QApplication.widgetAt(gpos)
                while w is not None:
                    if isinstance(w, GbModCard):
                        return super().eventFilter(obj, event)
                    w = w.parent() if hasattr(w, "parent") else None
            except Exception:
                pass
            # 点击其他页面区域时收起浮层。
            self._hide_detail_floating()
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def hideEvent(self, event):
        """离开商店页时收起下载和详情浮层，并移除页面的全局点击过滤器。"""
        # 顶层下载窗口不会随父控件自动隐藏，页面切走时须显式收起。
        try:
            if self.download_flyout.isVisible():
                self.download_flyout.hide()
        except RuntimeError:
            pass
        # 页面隐藏时同时收起详情浮层。
        try:
            if self.detail_view.isVisible():
                self._hide_detail_floating()
        except RuntimeError:
            pass
        # 卸载全局事件过滤器。
        try:
            QApplication.instance().removeEventFilter(self)
        except Exception:
            pass
        super().hideEvent(event)

    # 公开操作接口。
    def refresh(self) -> None:
        """重新匹配未解析的游戏，或获取当前分类、搜索结果的第一页。

        同时清空筛选用的原始记录，避免混用刷新前后的数据。
        """
        if self._gb_game_id is None:
            if self._local_game_name:
                self.set_game(self._local_game_name, force=True)
            return
        self._error_hide_timer.stop()
        self.error_bar.hide()
        # 手动刷新先使分类、详情和更新缓存失效，随后从网络获取最新数据。
        try:
            self._client.invalidate_game_caches(self._gb_game_id)
        except Exception as exc:
            logger.debug("刷新时清缓存失败（忽略）: %s", exc)
        # 递增请求代次，使旧分页响应失效。
        self._request_gen += 1
        self._cancel_inflight()  # 取消旧请求并释放并发名额，使刷新请求能够立即执行。
        self._page = 1
        self._backfill_attempts = 0
        self.grid.clear()
        self._all_records = []
        self.browse_view.show_loading()
        # 分类树未加载时先补取根分类，再执行分页请求，避免只显示加载状态。
        if not self._path:
            self._kick_roots(self._gb_game_id)
        else:
            self._fetch_next_page()

    def reset_to_empty(self) -> None:
        """删除或取消激活游戏时清空商店数据和卡片，并使在途响应失效。"""
        self._request_gen += 1
        self._cancel_inflight()  # 取消在途请求，避免已清空页面被旧响应重新填充。
        self._gb_game_id = None
        self._local_game_name = ""
        self._pending_initial_game_id = None
        self._display_name = ""
        self._path = []
        self._error_hide_timer.stop()
        self.error_bar.hide()
        try:
            self._hide_detail_floating()
        except Exception:
            pass
        self.grid.clear()
        self._all_records = []
        try:
            self.breadcrumb.set_path([])
            _sb = self.filter_bar.search_box
            _sb.blockSignals(True)
            _sb.clear()
            _sb.blockSignals(False)
        except (RuntimeError, AttributeError):
            pass
        self.stack.setCurrentIndex(self.VIEW_MAIN)
        self.browse_view.show_empty()

    def set_game(self, local_game_id: str, force: bool = False) -> None:
        """切换商店面向的游戏，并使旧请求失效。

        已匹配游戏加载浏览视图及分类；未匹配游戏显示未收录提示。
        同一游戏正在加载或已经加载时复用当前状态，force=True 可强制重新解析。
        以用户配置的英文名查询已保存的映射；名称改变后重新在线匹配。
        """
        overrides = self._game_store.get_gamebanana_overrides()
        name_en = self._game_name_en(local_game_id)
        gb_id = resolve_gb_game_id_by_name(name_en, local_game_id, overrides)
        # 同一游戏正在加载或已经加载时不重复请求，避免游戏切换与显示事件同时触发。
        # force=True 用于修改游戏信息后重新解析。
        if (
            not force
            and gb_id is not None
            and local_game_id == self._local_game_name
            and gb_id == self._gb_game_id
            and self._in_flight > 0
        ):
            return
        self._local_game_name = local_game_id
        self._invalidate_installed_cache()
        self._request_gen += 1
        self._cancel_inflight()  # 取消旧游戏请求，避免重试长期占用并发名额。
        self._path = []
        # 切换游戏时清空旧面包屑和搜索词，避免显示前一游戏的数据。
        try:
            self.breadcrumb.set_path([])
            _sb = self.filter_bar.search_box
            _sb.blockSignals(True)
            _sb.clear()
            _sb.blockSignals(False)
        except (RuntimeError, AttributeError):
            pass
        self.detail_view._profile = None
        # 切换游戏时收起详情并清除选中状态。
        self._hide_detail_floating()
        self._selected_mod_id = None
        # 同步下载浮窗的游戏筛选条件。
        try:
            self.download_flyout.set_game_filter(local_game_id or None)
            for row in self.download_flyout._row_widgets.values():
                row._sync_with_state()
        except Exception:
            pass
        # 切回游戏后恢复挂起的安装，包括上次运行保留的任务。延迟执行以等待
        # _on_game_changed 完成对账、角色树重建、目录监听和预设应用，并留出主窗口绘制时间。
        # 安装调度先于页面可见性检查，使商店页隐藏时也能恢复当前游戏的安装任务。
        if local_game_id:
            call_later(800, self, lambda gid=local_game_id: self._drain_pending_installs(gid))

        # 商店隐藏时记录待加载游戏，不发送请求；清空远程游戏编号，使在途分页提前结束。
        if not self.isVisible():
            self._gb_game_id = None
            self.grid.clear()
            self._all_records = []
            self._pending_initial_game_id = local_game_id or None
            # 提前设置加载状态，避免首次显示时短暂出现上一游戏的空网格。
            self.browse_view.show_loading()
            logger.debug("商店页不可见，推迟加载游戏「%s」到下次可见", local_game_id)
            return

        if gb_id is None:
            # 未保存当前名称的映射时，使用 NameMatch 接口在线查询。
            self._try_online_match_game(local_game_id)
            return
        self._load_known_game(
            gb_id,
            local_game_id,
            name_en or local_game_id,
        )

    def _game_name_en(self, local_game_id: str) -> str:
        """读取用于 GameBanana 匹配的游戏英文名；不存在时返回空字符串。"""
        try:
            from src.core.game_service import GameService

            g = GameService.instance().get_game(local_game_id)
            if g and (g.name_en or "").strip():
                return g.name_en.strip()
        except Exception:
            pass
        return ""

    def _load_known_game(
        self,
        gb_id: int,
        local_game_id: str,
        display_name: str,
    ) -> None:
        """按确定的游戏编号加载分类，面包屑使用配置或查询返回的名称。"""
        self._gb_game_id = gb_id
        self._display_name = display_name
        # 本次运行首次进入该游戏时清除 API 缓存，后续切换复用；手动刷新另行处理。
        if gb_id not in self._session_freshed_games:
            self._session_freshed_games.add(gb_id)
            try:
                self._client.invalidate_game_caches(gb_id)
            except Exception as exc:
                logger.debug("首次进入清缓存失败（忽略）: %s", exc)
        self.stack.setCurrentIndex(self.VIEW_MAIN)
        self.breadcrumb.set_path(
            [
                GbCategory(id=0, name=display_name, parent_id=None, item_count=0, icon_url=None),
                GbCategory(id=-1, name="Mods", parent_id=None, item_count=0, icon_url=None),
            ]
        )
        self.grid.clear()
        self._all_records = []
        self.browse_view.show_loading()
        self._kick_roots(gb_id)

    def _try_online_match_game(self, local_game_id: str) -> None:
        """在线查询游戏编号，匹配成功后保存映射并加载分类。"""
        self._gb_game_id = None
        # 优先使用游戏英文名查询，缺失时使用本地游戏编号。
        query_name = self._game_name_en(local_game_id) or local_game_id

        self.stack.setCurrentIndex(self.VIEW_MAIN)
        self.grid.clear()
        self._all_records = []
        self.breadcrumb.set_path([])
        self._error_hide_timer.stop()
        self.error_bar.hide()
        self.browse_view.show_loading()

        gen = self._request_gen
        runner = self._make_runner()
        runner.finished.connect(
            lambda records, lid=local_game_id, q=query_name, g=gen: self._on_game_match_result(records, lid, q, g)
        )
        runner.failed.connect(lambda message, lid=local_game_id, g=gen: self._on_game_match_failed(lid, g, message))
        runner.run(lambda q=query_name: self._client.match_game(q))

    def _on_game_match_result(
        self,
        records: list,
        local_game_id: str,
        query_name: str,
        gen: int,
    ) -> None:
        """只处理当前代次的游戏匹配结果，保存英文名对应关系并加载在线游戏，未匹配时显示提示。"""
        if gen != self._request_gen:
            return  # 切换游戏后丢弃过期匹配结果。
        gb_id = pick_gb_game_match(records, query_name)
        if gb_id is None:
            self._show_unknown_game(local_game_id)
            return
        # 按英文名保存匹配结果，后续同名查询直接复用；改名后重新匹配。
        overrides = self._game_store.get_gamebanana_overrides()
        cache_key = gb_override_key(self._game_name_en(local_game_id), local_game_id)
        if overrides.get(cache_key) != gb_id:
            overrides[cache_key] = gb_id
            self._game_store.set_gamebanana_overrides(overrides)
            logger.info(
                "在线匹配 GameBanana 游戏成功：%s（key=%s）→ id=%s（已写入 overrides）",
                local_game_id,
                cache_key,
                gb_id,
            )
        display_name = official_name_from_records(records, gb_id) or query_name or local_game_id
        self._load_known_game(gb_id, local_game_id, display_name)

    def _on_game_match_failed(self, local_game_id: str, gen: int, message: str) -> None:
        """当前代次匹配失败时回到浏览视图并显示错误，忽略旧游戏的失败结果。"""
        if gen != self._request_gen:
            return
        logger.warning("在线匹配 GameBanana 游戏失败：%s（网络/接口异常）", local_game_id)
        self.stack.setCurrentIndex(self.VIEW_MAIN)
        self._on_error(message)

    def _show_unknown_game(self, local_game_id: str) -> None:
        """切到「GameBanana 未收录」占位视图。"""
        self._gb_game_id = None
        # 显示当前游戏名称，优先英文名，其次中文名，最后使用内部编号。
        display = local_game_id
        try:
            from src.core.game_service import GameService

            g = GameService.instance().get_game(local_game_id)
            if g:
                display = (g.name_en or "").strip() or (g.name or "").strip() or local_game_id
        except Exception:
            pass
        self.unknown_view.set_game_name(display)
        self.stack.setCurrentIndex(self.VIEW_UNKNOWN)
        self.grid.clear()
        self._all_records = []
        self.breadcrumb.set_path([])

    # 分类加载。
    def _kick_roots(self, gb_id: int) -> None:
        """异步请求游戏根分类，将成功结果与发起时的请求代次绑定。"""
        runner = self._make_runner()
        gen = self._request_gen
        runner.finished.connect(lambda roots, g=gen: self._on_roots_loaded(roots, g))
        runner.failed.connect(self._on_error)
        runner.run(lambda: self._cat_service.roots(gb_id))

    def _on_roots_loaded(self, roots: list[GbCategory], gen: int) -> None:
        """为当前代次缓存根分类面包屑，并进入默认分类；没有可用分类时显示空状态。"""
        if gen != self._request_gen:
            return
        # 缓存根分类的面包屑子项，并选择默认分类。
        self.breadcrumb.set_children(-1, roots)
        target = self._pick_default(roots)
        if target is None:
            self.browse_view.show_empty()
            return
        new_path = [
            GbCategory(
                id=0, name=self._display_name or self._local_game_name, parent_id=None, item_count=0, icon_url=None
            ),
            GbCategory(id=-1, name="Mods", parent_id=None, item_count=0, icon_url=None),
            target,
        ]
        self._set_path_and_query(new_path)

    @staticmethod
    def _pick_default(cats: list[GbCategory]) -> GbCategory | None:
        """挑选默认展开的根分类。

        优先选择名称包含 skin 或 character 的分类，再按内容数量确定结果。
        没有名称匹配时选择内容数量最多的根分类。
        """
        if not cats:
            return None
        keywords = ("skin", "character")
        priority = [c for c in cats if any(kw in c.name.lower() for kw in keywords)]
        if priority:
            return max(priority, key=lambda c: c.item_count)
        return max(cats, key=lambda c: c.item_count)

    # 面包屑
    def _on_breadcrumb_path_changed(self, new_path: list[GbCategory]) -> None:
        """只对包含实际分类的面包屑路径启动新的分类查询。"""
        if len(new_path) <= 2:
            return
        self._set_path_and_query(new_path)

    def _on_children_requested(self, parent_id: int) -> None:
        """异步加载有效父分类的子项，虚拟根节点直接提供空子项结果。"""
        if parent_id <= 0:
            self.breadcrumb.set_children(parent_id, [])
            return
        runner = self._make_runner()
        gen = self._request_gen
        runner.finished.connect(lambda kids, p=parent_id, g=gen: self._on_children_loaded(p, kids, g))
        runner.failed.connect(lambda error, p=parent_id, g=gen: self._on_children_error(p, error, g))
        runner.run(lambda: self._cat_service.children(parent_id))

    def _on_children_error(self, parent_id: int, error: str, gen: int) -> None:
        """仅将当前请求代次的子分类错误交给对应面包屑显示。"""
        if gen != self._request_gen:
            return
        self.breadcrumb.set_children_error(parent_id, error)

    def _on_children_loaded(self, parent_id: int, kids: list[GbCategory], gen: int) -> None:
        """仅将当前代次的子分类结果写入面包屑缓存。"""
        if gen != self._request_gen:
            return
        self.breadcrumb.set_children(parent_id, kids)

    def _set_path_and_query(self, new_path: list[GbCategory]) -> None:
        """切换分类时重置搜索与分页并使旧请求失效，同一分类尚在加载时合并重复选择。"""
        # 同一分类仍在加载时忽略重复选择，避免连续递增请求代次使全部回调过期。
        if self._path and new_path and new_path[-1].id == self._path[-1].id and self._in_flight > 0:
            return
        self._path = new_path
        self.breadcrumb.set_path(new_path)
        self._mode = "category"
        self._search_query = ""
        self._page = 1
        self._backfill_attempts = 0
        self._request_gen += 1
        self.grid.clear()
        self._all_records = []
        self.browse_view.show_loading()
        self._fetch_next_page()

    # 筛选栏。
    def _on_search(self, query: str) -> None:
        """在已匹配游戏中切换搜索或分类模式，重置分页与记录后加载第一页。"""
        if self._gb_game_id is None:
            return
        self._search_query = query
        self._mode = "search" if query else "category"
        self._page = 1
        self._backfill_attempts = 0
        self._request_gen += 1
        self.grid.clear()
        self._all_records = []
        self.browse_view.show_loading()
        self._fetch_next_page()

    def _on_sort_changed(self, sort: str) -> None:
        """保存排序偏好，并在当前分类或搜索上下文中重新加载记录。"""
        self._sort = sort
        self._config.set("gamebanana.default_sort", sort)
        if self._gb_game_id is None:
            return
        if self._path or self._mode == "search":
            self._page = 1
            self._backfill_attempts = 0
            self._request_gen += 1
            self.grid.clear()
            self._all_records = []
            self.browse_view.show_loading()
            self._fetch_next_page()

    def _on_nsfw_policy_changed(self, policy: str) -> None:
        """保存成人内容显示策略，策略变化时按未过滤的缓存记录重新构建可见结果。"""
        old = self._nsfw_policy
        self._nsfw_policy = policy
        self._config.set("gamebanana.nsfw_policy", policy)
        if old == policy:
            return
        # 按原始记录重建筛选结果，使策略切换不依赖卡片是否已创建或已被过滤。
        self._rerender_from_cache()

    def _rerender_from_cache(self) -> None:
        """按原始记录和当前内容策略重建网格，保留网络请求及分页状态。"""
        if not self._all_records:
            return
        self.grid.clear()
        filtered = self._apply_nsfw(list(self._all_records))
        if not filtered:
            self.browse_view.show_empty()
            return

        # 下载量仍为零的记录标记为待补全。
        pending = {r.mod_id for r in filtered if not r.download_count}

        self.browse_view.show_grid()
        # 策略切换时一次提交全部过滤结果，避免连续切换产生交错的分批调度。
        # self._make_card 按虚拟滚动需求创建卡片，并读取当前状态。
        self.grid.append_items(
            filtered,
            has_more=self._has_more,
            card_factory=self._make_card,
            card_binder=self._bind_card,
            image_prefetcher=self._prefetch_thumbnail,
            image_prefetch_canceller=self._cancel_thumbnail_prefetch,
        )
        # 重新请求尚未补全的下载量，并复用接口缓存。
        if pending:
            self._fetch_download_counts(list(pending), self._request_gen)

    # 分页加载。
    def _fetch_next_page(self) -> None:
        """固化当前搜索或分类条件后提交下一页请求，并登记分页与网格加载状态。"""
        if self._gb_game_id is None:
            return
        leaf = self._path[-1] if self._path else None
        page = self._page
        # 确定请求内容后再创建 runner，确保 _in_flight 只计入实际提交的请求。
        if self._mode == "search" and self._search_query:
            query = self._search_query
            sort = self._sort
            gid = self._gb_game_id
            coro_factory = lambda: self._client.search(query, game_id=gid, page=page, sort=sort)  # noqa: E731
        elif leaf is not None and leaf.id > 0:
            cat_id = leaf.id
            sort = self._sort
            coro_factory = lambda: self._client.mods_by_category(cat_id, page=page, sort=sort)  # noqa: E731
        else:
            return
        runner = self._make_runner()
        gen = self._request_gen
        runner.finished.connect(lambda result, g=gen: self._on_page_loaded(result, g))
        runner.failed.connect(self._on_error)
        runner.run(coro_factory)
        self._page += 1
        self.grid.mark_loading()

    def _on_page_loaded(self, result, gen: int) -> None:
        """按请求代次和在途状态接收分页结果，缓存原始条目并按内容策略进入统计补全与渲染。"""
        # 有其他请求在途时丢弃过期结果；若已无待处理请求，则使用此结果结束加载状态，
        # 避免重复触发被幂等检查拦截后，页面缺少可用于完成加载的回调。
        if gen != self._request_gen and self._in_flight > 0:
            return
        records, has_more = result
        raw_records = list(records)  # 保留包含 NSFW 项的原始记录，用于下载量补全与筛选策略切换。
        self._all_records.extend(records)
        records = self._apply_nsfw(records)
        if not records:
            if self.grid.count() == 0:
                self.browse_view.show_empty()
            return

        # apiv11 列表不返回下载量，因此先并发补全统计，再渲染卡片。
        # 超过两秒仍未返回的项使用待加载占位，后台完成后更新。
        self._enrich_then_render(raw_records, records, has_more, gen)

    def _enrich_then_render(
        self,
        raw_records: list[GbModRecord],
        records: list[GbModRecord],
        has_more: bool,
        gen: int,
    ) -> None:
        """补全下载量后渲染卡片，使各项统计尽量同时显示。

        两秒后先使用已完成结果，未完成项显示待加载占位；后台请求完成后更新对应卡片。
        """
        mod_ids = [r.mod_id for r in raw_records if r.mod_id]
        if not mod_ids:
            self._render_records(records, has_more, gen)
            return

        runner = self._make_runner()

        def _apply(counts: object) -> set[int]:
            """将已获取的下载量写回记录，并返回尚未完成的 Mod 编号。"""
            if not isinstance(counts, dict):
                return set(mod_ids)
            for r in raw_records:
                if r.mod_id in counts:
                    r.download_count = int(counts[r.mod_id])
            return {mid for mid in mod_ids if mid not in counts}

        def _on_done(counts) -> None:
            """应用已返回的下载量后显示记录，对仍缺少统计的条目继续后台补全。"""
            pending = _apply(counts)
            self._render_records(records, has_more, gen)
            if pending:
                # 后台继续请求超时项，完成后替换占位文本。
                self._fetch_download_counts(list(pending), gen)

        def _on_fail(_msg) -> None:
            """下载量查询失败时仍显示记录，并在后台补充统计。"""
            # 请求失败时仍显示卡片，并在后台重试下载量补全。
            self._render_records(records, has_more, gen)
            self._fetch_download_counts(mod_ids, gen)

        runner.finished.connect(_on_done)
        runner.failed.connect(_on_fail)
        runner.run(lambda mids=mod_ids: self._client.bulk_download_counts(mids, timeout=2.0))

    def _render_records(
        self,
        records: list[GbModRecord],
        has_more: bool,
        gen: int,
    ) -> None:
        """分批提交卡片记录，供分页加载和下载量补全流程共用。"""
        # 卡片由 self._make_card 创建，读取当前安装状态、选中状态和记录下载量；
        # 卡片按 record.download_count 推断下载量是否待补全，避免闭包冻结旧状态。

        # 每批提交 8 条记录，批次之间让出事件循环以处理绘制和输入。
        # 网格按需创建可视卡片；全部批次提交后再判断是否需要继续回填。
        chunk_size = 8
        self._has_more = has_more
        self.browse_view.show_grid()  # 提交首批记录前显示网格视图。

        def _append_chunk(start_idx: int) -> None:
            """按块追加当前代次记录，滚动时延后提交，批次结束后按阈值补充后续页面。"""
            # 请求代次变化后停止提交当前批次。
            if gen != self._request_gen:
                return
            # 滚动停止后再提交本批记录，避免卡片更新与滚动绘制争用主线程。
            if self.grid.is_scrolling():
                self.grid.run_when_idle(lambda s=start_idx: _append_chunk(s))
                return
            chunk = records[start_idx : start_idx + chunk_size]
            if not chunk:
                # 全部记录提交后判断是否需要回填视口。
                if has_more and self.grid.count() < self._min_per_load and self._backfill_attempts < self._backfill_max:
                    self._backfill_attempts += 1
                    call_later(50, self, self._fetch_next_page)
                return
            self.grid.append_items(
                chunk,
                has_more=has_more,
                card_factory=self._make_card,
                card_binder=self._bind_card,
                image_prefetcher=self._prefetch_thumbnail,
                image_prefetch_canceller=self._cancel_thumbnail_prefetch,
            )
            call_later(0, self, lambda nxt=start_idx + chunk_size: _append_chunk(nxt))

        _append_chunk(0)

    def _fetch_download_counts(self, mod_ids: list[int], gen: int) -> None:
        """后台批量补全下载量，并在主线程更新对应卡片。

        超时补全、失败重试和内容策略切换可能请求同一批记录，因此每个请求代次内
        每个 Mod 只补全一次，以免重复访问触发接口限流。
        """
        if gen != self._dl_asked_gen:
            self._dl_asked_gen = gen
            self._dl_asked.clear()
        ids = [int(mid) for mid in mod_ids if mid and int(mid) not in self._dl_asked]
        if not ids:
            return
        self._dl_asked.update(ids)
        runner = self._make_runner()
        runner.finished.connect(lambda result, g=gen: self._on_download_counts(result, g))
        # 下载量补全失败不影响列表使用，无需显示错误提示。
        runner.failed.connect(lambda *_: None)
        runner.run(lambda ids=ids: self._client.bulk_download_counts(ids))

    def _on_download_counts(self, counts: dict, gen: int) -> None:
        """按在线 Mod 编号更新缓存记录和可见卡片下载量，统计结果可跨请求代次复用。"""
        if not isinstance(counts, dict) or not counts:
            return
        # 下载量按 Mod 编号匹配，页面请求代次变化不影响其有效性。
        _ = gen
        # 先将结果写回记录，使离屏卡片创建时也能读取最新下载量。
        for rec in self._all_records:
            if rec.mod_id in counts:
                try:
                    rec.download_count = int(counts[rec.mod_id])
                except (TypeError, ValueError):
                    pass
        # 立即更新可见卡片的下载量标签。
        for card in self.grid.iter_cards():
            mid = getattr(getattr(card, "record", None), "mod_id", 0)
            if mid in counts:
                card.update_downloads(counts[mid])

    def _apply_nsfw(self, records: list[GbModRecord]) -> list[GbModRecord]:
        """移除策略下过滤成人内容，其他策略保留条目并交给卡片控制预览。"""
        if self._nsfw_policy == "remove":
            return [r for r in records if not r.is_nsfw]
        return records

    def _scan_installed_gb(self) -> tuple[set, set, set]:
        """一次扫描建立卡片和文件行共用的安装索引。

        返回 (mod_ids, file_keys, md5_keys)：Mod 编号集合用于卡片徽章，
        (mod_id, file_id) 集合用于文件行，(mod_id, md5) 集合用于缺少文件编号时的匹配。
        这些集合共用一次查询，使后续卡片的安装状态判断保持常数开销。
        """
        mod_ids: set = set()
        file_keys: set = set()
        md5_keys: set = set()
        self._installed_gb_paths = {}
        idx = self._mod_index
        if idx is None:
            return mod_ids, file_keys, md5_keys
        try:
            mods = idx.get_all_mods() if hasattr(idx, "get_all_mods") else idx.get_mods()
        except Exception:
            return mod_ids, file_keys, md5_keys
        for m in mods:
            gb = (m.info.custom_data or {}).get("gamebanana") or {}
            mid = gb.get("mod_id")
            if not mid:
                continue
            mod_ids.add(mid)
            try:
                mid_i = int(mid)
            except (TypeError, ValueError):
                continue
            fid = gb.get("file_id")
            if fid:
                try:
                    file_keys.add((mid_i, int(fid)))
                except (TypeError, ValueError):
                    pass
            md5 = str(gb.get("file_md5") or "").strip().lower()
            if md5:
                md5_keys.add((mid_i, md5))
            path = getattr(m, "path", None)
            if path:
                self._installed_gb_paths.setdefault(mid_i, []).append(
                    (str(fid or ""), md5, path, getattr(m, "mod_id", ""))
                )
        return mod_ids, file_keys, md5_keys

    def _can_open_downloaded_mod(self, task) -> bool:
        """判断下载记录是否能关联到仍存在的本地 Mod。"""
        return self._downloaded_local_mod_id(task) is not None

    def _downloaded_local_mod_id(self, task):
        """按游戏范围及在线文件编号或摘要定位仍存在的本地 Mod。"""
        from pathlib import Path

        if self._local_game_name and task.game_id != self._local_game_name:
            return None
        self._cached_installed()
        mid = int(getattr(task.profile, "mod_id", 0))
        checksum = (task.file.md5_checksum or "").lower()
        for fid, md5, path, local_id in self._installed_gb_paths.get(mid, ()):
            if fid == str(task.file.file_id) or (checksum and checksum == md5):
                if local_id and Path(path).exists():
                    return local_id
        return None

    def _open_downloaded_file(self, task) -> None:
        """将下载记录对应的本地 Mod 定位请求交给宿主概览页。"""
        local_id = self._downloaded_local_mod_id(task)
        if local_id is not None:
            self.open_local_mod_in_overview_requested.emit(local_id)

    def _cached_installed(self) -> tuple[set, set, set]:
        """按需缓存安装索引，安装或删除事件使缓存失效后重新计算。

        全部卡片共享查询结果，避免虚拟滚动创建卡片时重复扫描资源库。
        """
        if self._installed_cache is None:
            self._installed_cache = self._scan_installed_gb()
        return self._installed_cache

    def _cached_installed_ids(self) -> set:
        """返回安装快照中的在线 Mod 标识集合，供卡片徽章快速查询。"""
        return self._cached_installed()[0]

    def _is_gb_file_installed(self, mod_id: int, file_id: int, md5: str) -> bool:
        """判断指定 GameBanana 文件是否已安装。

        同一在线 Mod 的不同下载文件分别安装，须按 (mod_id, file_id) 判断；
        file_id 缺失时改用 MD5 匹配。
        """
        _, file_keys, md5_keys = self._cached_installed()
        mid = int(mod_id or 0)
        if not mid:
            return False
        if file_id and (mid, int(file_id)) in file_keys:
            return True
        m = str(md5 or "").strip().lower()
        return bool(m and (mid, m) in md5_keys)

    def _invalidate_installed_cache(self) -> None:
        """使安装状态快照失效，下次查询时统一重建标识集合与文件映射。"""
        self._installed_cache = None

    def _is_installed(self, rec: GbModRecord) -> bool:
        """根据安装快照判断该在线 Mod 是否已有本地安装记录。"""
        return rec.mod_id in self._cached_installed_ids()

    def _make_card(self, rec: GbModRecord) -> GbModCard:
        """按当前记录及页面状态创建卡片，供首屏、分页和虚拟滚动共用。

        创建时读取安装索引、当前选中的 Mod 编号及记录中的下载量，
        确保离屏卡片重新创建后仍反映最新状态。
        """
        card = GbModCard(
            rec,
            image_loader=self._loader,
            nsfw_policy=self._nsfw_policy,
            is_installed=(rec.mod_id in self._cached_installed_ids()),
            download_count_pending=(not rec.download_count),
            defer_image=True,  # 缓存命中立即显示，缺图由 grid 在滚动中分批加载。
        )
        if self._selected_mod_id is not None and int(rec.mod_id) == self._selected_mod_id:
            card.set_selected(True)
        card.clicked.connect(self._on_card_clicked)
        return card

    def _bind_card(self, card: GbModCard, rec: GbModRecord) -> None:
        """复用控件时恢复当前记录和页面状态，不重复连接信号。"""
        card.set_record(
            rec,
            nsfw_policy=self._nsfw_policy,
            is_installed=(rec.mod_id in self._cached_installed_ids()),
            download_count_pending=(not rec.download_count),
            defer_image=True,
        )
        card.set_selected(self._selected_mod_id is not None and int(rec.mod_id) == self._selected_mod_id)

    def _prefetch_thumbnail(self, rec: GbModRecord) -> bool:
        """按卡片尺寸预取裁剪缩略图，缺少地址或预取能力时跳过。"""
        prefetch = getattr(self._loader, "prefetch", None)
        if not rec.thumbnail_url or not callable(prefetch):
            return True
        return prefetch(
            rec.thumbnail_url, size=QSize(GbModCard.CARD_WIDTH, GbModCard.THUMBNAIL_HEIGHT), crop_to_size=True
        )

    def _cancel_thumbnail_prefetch(self, rec: GbModRecord) -> None:
        """撤销对应卡片尺寸与裁剪方式的缩略图预取请求。"""
        cancel = getattr(self._loader, "cancel_prefetch", None)
        if rec.thumbnail_url and callable(cancel):
            cancel(rec.thumbnail_url, size=QSize(GbModCard.CARD_WIDTH, GbModCard.THUMBNAIL_HEIGHT), crop_to_size=True)

    def _has_local_md5(self, md5: str) -> bool:
        """通过下载管理器确认指定摘要对应的本地归档仍可用。"""
        return self._download_manager.has_local_md5(md5) is not None

    # 详情视图。
    def _on_card_clicked(self, record: GbModRecord) -> None:
        """清空旧详情并显示加载态，标记选中卡片后异步请求该资源详情。"""
        # 先显示浮层并同步清空旧内容，再异步请求详情，避免短暂显示其他 Mod 的数据。
        self.detail_view.show_loading_for(record)
        self._show_detail_floating()
        # 同步可见卡片的选中描边。
        self._set_selected_card(record.mod_id)
        runner = self._make_runner()
        runner.finished.connect(self.detail_view.load_profile)
        runner.failed.connect(self._on_error)
        # 请求失败时显示错误；按 Mod 编号校验回调，防止过期失败覆盖当前加载状态。
        runner.failed.connect(lambda *_a, mid=record.mod_id: self._on_profile_load_failed(mid))
        runner.run(lambda: self._client.get_profile(record.model, record.mod_id))

    def _on_profile_load_failed(self, mod_id) -> None:
        """仅在失败资源仍是当前待加载详情时显示失败状态，忽略迟到错误。"""
        try:
            if int(getattr(self.detail_view, "_pending_mod_id", 0) or 0) == int(mod_id or 0):
                self.detail_view.show_load_failed()
        except (AttributeError, RuntimeError, ValueError):
            pass

    # 浮窗显示 / 隐藏 / 重定位
    def _show_detail_floating(self) -> None:
        """按保存的有效宽度在右侧显示详情浮层，宽度不小于设定最小值。"""
        if self._detail_width < self._detail_min_width:
            self._detail_width = self._detail_min_width
        self._reposition_detail_view()  # 先确定详情和手柄的最终位置。
        target = self.detail_view.geometry()
        h_target = self._detail_handle.geometry()
        # 起点：整体贴到页面右缘之外（不可见），show 后再横向滑入到最终位置（弹出过渡）。
        off_x = self.width()
        self.detail_view.setGeometry(off_x, target.y(), target.width(), target.height())
        self._detail_handle.setGeometry(off_x, h_target.y(), h_target.width(), h_target.height())
        self.detail_view.show()
        self.detail_view.raise_()
        self._detail_handle.show()
        self._detail_handle.raise_()
        # 横向滑入 + 淡入（fade_in 结束移除 effect，不影响内部 QTextBrowser/图片渲染）。
        anim.animate_geometry(self.detail_view, target)
        anim.animate_geometry(self._detail_handle, h_target)
        anim.fade_in(self.detail_view)
        # 显示后刷新详情背景快照，滑入完成后再按最终位置更新一次。
        self._kick_detail_backdrop()
        call_later(260, self, self._kick_detail_backdrop)

    def _hide_detail_floating(self) -> None:
        """收起详情并清除选中状态；页面可见时播放滑出动画。"""
        try:
            if self.isVisible() and self.detail_view.isVisible() and anim.is_enabled():
                dv = self.detail_view
                hd = self._detail_handle
                off_x = self.width()
                geo = dv.geometry()
                h_geo = hd.geometry()

                def _finish() -> None:
                    """收起动画结束后隐藏详情和手柄，控件已经销毁时忽略。"""
                    try:
                        dv.hide()
                        hd.hide()
                    except RuntimeError:
                        pass

                # 详情与手柄滑出页面右侧后隐藏。
                anim.animate_geometry(hd, QRect(off_x, h_geo.y(), h_geo.width(), h_geo.height()))
                anim.animate_geometry(
                    dv,
                    QRect(off_x, geo.y(), geo.width(), geo.height()),
                    on_finished=_finish,
                )
            else:
                self.detail_view.hide()
                self._detail_handle.hide()
        except RuntimeError:
            pass
        # 取消所有卡片的选中描边
        self._set_selected_card(0)
        # 拖动触发自动收起时会释放鼠标捕获，mouseRelease 可能不再到达。
        # 显式重置拖动标志，避免影响下一次交互。
        self._detail_resizing = False

    def _reposition_detail_view(self) -> None:
        """按页面尺寸将详情定位在右侧，手柄紧贴其左边缘。

        页面显示、尺寸变化及拖动调整宽度时均须更新位置。
        """
        page_w = self.width()
        page_h = self.height()
        if page_w <= 0 or page_h <= 0:
            return
        # 错误提示位于独立浮层，详情无需为其预留布局空间。
        top_y = 0
        # 限制详情宽度，为浏览视图至少保留 200 像素。
        max_w = max(self._detail_min_width, page_w - 200)
        # 按控件最大宽度限制几何计算，避免实际宽度被 Qt 截断后右侧位置偏移。
        max_w = min(max_w, self.detail_view.maximumWidth())
        w = min(max(self._detail_min_width, self._detail_width), max_w)
        h = max(100, page_h - top_y)
        x = max(0, page_w - w)
        self.detail_view.setGeometry(x, top_y, w, h)
        # handle: 6px 宽，紧贴 detail_view 左边缘外侧（露在 BrowseView 上方）
        hw = self._detail_handle.HANDLE_WIDTH
        self._detail_handle.setGeometry(max(0, x - hw), top_y, hw, h)
        # 提升详情及手柄的层级；原生窗口的系统 Z 序也需保持与控件顺序一致，
        # 以便鼠标事件命中可见浮层。
        if self.detail_view.isVisible():
            self.detail_view.raise_()
            self._detail_handle.raise_()
        # 面板几何变化后节流更新对应背景区域的模糊快照。
        self._kick_detail_backdrop()

    def _kick_detail_backdrop(self) -> None:
        """合并滚动和重定位触发的快照刷新，每约 50 毫秒处理一次。

        仅在纯色主题且面板可见时更新；背景图主题由窗口统一提供背景。
        """
        if not self.detail_view.isVisible() or theme.is_background_active():
            return
        if not self._detail_backdrop_timer.isActive():
            self._detail_backdrop_timer.start(50)

    def _refresh_detail_backdrop(self) -> None:
        """捕获浮层下方的浏览视图，缩小并模糊后作为详情背景。

        捕获不含浮层自身，先缩小可限制高分辨率屏幕的模糊开销。
        """
        try:
            if not self.detail_view.isVisible() or theme.is_background_active():
                return
            dv = self.detail_view
            src = self.browse_view
            if src is None or not src.isVisible() or dv.width() <= 0 or dv.height() <= 0:
                return
            # 预览栏矩形映射到 BrowseView 坐标，取与其可见区的交集（面板可能比网格略高）。
            origin = dv.mapTo(src, QPoint(0, 0))
            region = QRect(origin, dv.size()).intersected(src.rect())
            if region.width() <= 2 or region.height() <= 2:
                dv.set_backdrop(None)
                return
            pm = src.grab(region)
            if pm.isNull():
                return
            # 先缩小到不超过 220 像素的工作宽度再模糊，绘制时放大以限制处理开销。
            work_w = 220
            if pm.width() > work_w:
                pm = pm.scaledToWidth(work_w, Qt.TransformationMode.SmoothTransformation)
            from src.ui.widgets.image_utils import _gaussian_blur

            dv.set_backdrop(_gaussian_blur(pm, radius=16.0))
        except Exception:
            pass

    def _on_detail_resize_drag(self, delta: int) -> None:
        """根据拖动增量调整浮层宽度，低于最小值时收起；浏览视图继续重绘新暴露区域。"""
        self._detail_resizing = True
        proposed = self._detail_width + delta
        if proposed < self._detail_min_width:
            # 宽度低于最小值时收起，并保留最小宽度供下次展开。
            self._detail_width = self._detail_min_width
            self._hide_detail_floating()
            return
        # 为浏览视图保留至少 200 像素。
        max_w = max(self._detail_min_width, self.width() - 200)
        self._detail_width = min(max_w, proposed)
        self._reposition_detail_view()

    def _on_detail_drag_released(self) -> None:
        """保存拖动后的详情宽度，并延后解除拖动标记以避免释放事件误触外部点击关闭。"""
        # 延迟到当前事件分发结束后清除拖动标志，避免释放鼠标时触发外部点击关闭。
        call_later(0, self, self._clear_resizing_flag)
        # 拖动结束后保存浮层宽度。
        Config.instance().set("ui.store_detail_width", int(self._detail_width))

    def _clear_resizing_flag(self) -> None:
        """解除详情宽度调整期间的外部点击屏蔽状态。"""
        self._detail_resizing = False

    def _set_selected_card(self, mod_id: int) -> None:
        """选中指定 Mod 的可见卡片，并清除其他卡片的选中状态。

        同时保存选中编号，供虚拟滚动创建卡片时恢复状态。
        """
        self._selected_mod_id = int(mod_id) if mod_id else None
        try:
            for card in self.grid.iter_cards():
                rec = getattr(card, "record", None)
                if rec is None or not hasattr(card, "set_selected"):
                    continue
                card.set_selected(int(rec.mod_id) == self._selected_mod_id)
        except Exception:
            pass

    def _on_back_to_browse(self) -> None:
        """收起详情浮层并清除浏览卡片的选中状态。"""
        # 返回时收起详情并清除选中状态。
        self._hide_detail_floating()

    def _on_download_task_removed(self, file_id: int) -> None:
        """移除文件任务的信号连接登记，并将详情按钮恢复为空闲状态。"""
        self._wired_download_tasks.discard(int(file_id))
        self.detail_view.set_file_state(file_id, "idle", progress=0)

    def _on_download_requested(self, file: GbModFile) -> None:
        """冻结下载所属游戏与详情，复用本地归档或任务，并仅为新任务连接进度和安装回调。"""
        if self.detail_view._profile is None:
            return
        # 创建下载任务时冻结详情数据，避免异步完成后读取已切换的名称或安装元数据。
        profile = self.detail_view._profile
        game_id = self._local_game_name or ""
        task = self._download_manager.request(
            profile,
            file,
            game_id=game_id,
        )
        if task is None:
            # md5 命中本地缓存：跳过下载，但仍需把详情按钮置为「已下载·安装」并触发安装流程。
            # 重启后也可使用缓存文件恢复安装，并采用当前详情数据。
            path = self._download_manager.has_local_md5(file.md5_checksum)
            if path is not None:
                self.detail_view.set_file_state(file.file_id, "done")
                self._after_download(file, path, profile, game_id)
            return
        if task.state == "done" and task.archive_path.is_file():
            self._after_download(file, task.archive_path, profile, game_id)
            return
        # 新任务立即显示下载状态，并通过进度、状态、失败和完成信号持续更新文件按钮。

        # 仅为新任务连接信号；已在队列中的文件会复用任务，重复连接会导致重复安装。
        if task.file.file_id in self._wired_download_tasks:
            # 已连接的任务仅显示下载管理，不重复连接信号。
            try:
                self._show_download_flyout()
            except Exception:
                pass
            return
        self._wired_download_tasks.add(task.file.file_id)
        self._on_task_state(file.file_id, task.state)
        # 新下载入队后显示下载管理；命中本地缓存直接安装时不弹出下载浮窗。
        try:
            self._show_download_flyout()
        except Exception:
            pass
        task.state_changed.connect(
            lambda s, fid=file.file_id, t=task: (
                self._on_task_state(fid, s) if self._download_manager.get_task(fid) is t else None
            )
        )
        task.progress.connect(
            lambda d, total, fid=file.file_id, t=task: (
                self._on_task_progress(fid, d, total) if self._download_manager.get_task(fid) is t else None
            )
        )
        task.failed.connect(
            lambda _msg, fid=file.file_id, t=task: (
                self.detail_view.set_file_state(fid, "failed") if self._download_manager.get_task(fid) is t else None
            )
        )
        task.finished.connect(
            lambda path, f=file, p=profile, g=task.game_id, t=task: (
                self._after_download(f, path, p, g) if self._download_manager.get_task(f.file_id) is t else None
            )
        )

    def _on_task_state(self, file_id: int, state: str) -> None:
        """将下载任务状态转换为详情文件行的显示状态。

        preparing 与 downloading 共用下载中状态，完成、失败和取消按各自结果更新按钮。
        """
        if state in ("queued", "preparing", "downloading"):
            self.detail_view.set_file_state(file_id, "downloading")
        elif state == "done":
            self.detail_view.set_file_state(file_id, "done")
        elif state in ("failed", "canceled"):
            self.detail_view.set_file_state(file_id, "failed")
        elif state == "paused":
            self.detail_view.set_file_state(file_id, "idle")

    def _on_task_progress(self, file_id: int, done_bytes: int, total: int) -> None:
        """总量已知时将下载字节进度转换为百分比，更新详情中的文件按钮。"""
        if total <= 0:
            return
        pct = max(0, min(100, int(done_bytes * 100 / total)))
        self.detail_view.set_file_state(file_id, "downloading", progress=pct)

    def _on_install_requested(self, file: GbModFile) -> None:
        """跳过已安装的具体文件，优先安装本地归档，缓存失效时重新进入下载请求流程。"""
        # 已安装的具体文件不重复触发安装；同一 Mod 的其他文件仍可安装。
        profile = self.detail_view._profile
        if profile is not None and self._is_gb_file_installed(
            int(getattr(profile, "mod_id", 0) or 0),
            int(file.file_id or 0),
            file.md5_checksum or "",
        ):
            return
        path = self._download_manager.has_local_md5(file.md5_checksum)
        if path is not None:
            # 即时安装使用当前游戏，无需记录下载期间游戏切换的上下文。
            self._after_download(file, path, profile)
        else:
            # 按钮绘制后缓存仍可能消失，请求入口也处理没有校验和的已完成下载。
            self._on_download_requested(file)

    def _after_download(
        self,
        file: GbModFile,
        archive_path,
        profile=None,
        game_id: str = "",
    ) -> None:
        """安装规划期间持有归档引用，无论成功、取消或异常都释放本次临时占用。"""
        owner = f"download:{file.file_id}"
        self._download_manager.retain(file.file_id, file.md5_checksum, owner=owner, path=archive_path)
        try:
            self._prepare_download_install(file, archive_path, profile, game_id)
        finally:
            # 返回前由宿主或待安装记录接管，取消选择和缺失资料也释放占用。
            self._download_manager.release(owner)
