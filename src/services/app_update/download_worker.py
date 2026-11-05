"""将异步更新会话封装为 Qt 工作线程，通过任务编号隔离迟到事件。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Slot

from src.services.app_update.download import DownloadSession
from src.services.app_update.download_sources import DownloadRoute
from src.services.app_update.download_state import UpdateArtifact
from src.utils.logger import logger


class AppUpdateDownloadWorker(QThread):
    """独占下载事件循环，业务结果与原生线程退出使用不同信号。"""

    progress = Signal(int, "qint64", "qint64")
    source_changed = Signal(int, str, "qint64")
    status_changed = Signal(int, str)
    succeeded = Signal(int, str)
    failed = Signal(int, str)

    def __init__(
        self,
        session_id: int,
        artifact: UpdateArtifact,
        cache_dir: Path,
        routes: Sequence[DownloadRoute],
        *,
        automatic: bool,
    ) -> None:
        """创建无 Qt 父对象的线程，由管理器在原生运行期间登记保活。"""
        super().__init__()
        self._session_id = session_id
        self._cancelled = threading.Event()
        self._control_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._session = DownloadSession(
            artifact,
            cache_dir,
            routes,
            automatic=automatic,
            on_progress=self._report_progress,
            on_source=self._report_source,
            on_status=self._report_status,
        )

    def _report_progress(self, downloaded: int, total: int) -> None:
        """保留大文件字节精度，界面根据会话编号决定是否接受进度。"""
        self.progress.emit(self._session_id, downloaded, total)

    def _report_source(self, route: DownloadRoute, offset: int) -> None:
        """发送线路显示名和已有字节基线，不向界面暴露代理地址。"""
        self.source_changed.emit(self._session_id, route.name, offset)

    def _report_status(self, message: str) -> None:
        """转发会话阶段，实际界面修改只在管理器所属线程执行。"""
        self.status_changed.emit(self._session_id, message)

    async def _run_session(self) -> Path:
        """在锁内发布取消入口，覆盖取消先于协程启动的情况。"""
        with self._control_lock:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            if self._cancelled.is_set():
                raise asyncio.CancelledError
        return await self._session.run()

    def run(self) -> None:
        """关闭整个事件循环后才发送业务结果，所有 HTTP 和文件句柄已退出。"""
        try:
            path = asyncio.run(self._run_session())
            if not self._cancelled.is_set():
                self.succeeded.emit(self._session_id, str(path))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if not self._cancelled.is_set():
                logger.warning("程序更新下载失败：%s", exc)
                self.failed.emit(self._session_id, str(exc))
        finally:
            with self._control_lock:
                self._loop = None
                self._task = None

    @Slot()
    def cancel(self) -> None:
        """线程安全地中断正在等待的协程，重复取消不能打断资源关闭过程。"""
        with self._control_lock:
            if self._cancelled.is_set():
                return
            self._cancelled.set()
            if self._loop is not None and self._task is not None:
                try:
                    self._loop.call_soon_threadsafe(self._task.cancel)
                except RuntimeError:
                    pass
