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
