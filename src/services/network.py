"""网络请求服务"""

from __future__ import annotations

import weakref
from pathlib import Path
from typing import Any

import httpx
from PySide6.QtCore import SIGNAL, QCoreApplication, QObject, Qt, QThread, Signal, Slot

from src.utils.logger import logger

_running_downloads: set[DownloadWorker] = set()
_connected_apps: weakref.WeakSet[QCoreApplication] = weakref.WeakSet()


def _finish_downloads() -> None:
    """应用退出前请求取消并等待原生下载线程，避免解释器销毁仍在运行的线程对象。"""
    workers = list(_running_downloads)
    for worker in workers:
        worker.cancel()
    for worker in workers:
        try:
            worker.wait()
        except RuntimeError:
            pass


class DownloadWorker(QThread):
    """后台下载线程"""

    progress = Signal("qint64", "qint64")  # 已下载字节数、总字节数，支持超过 2 GiB 的文件
    finished = Signal(str)  # 下载完成的文件路径
    error = Signal(str)  # 错误信息

    def __init__(self, url: str, save_path: str, parent: QObject | None = None, proxy: str | None = None) -> None:
        """保存下载地址、输出路径及代理设置，初始化可由宿主请求的取消标记。"""
        super().__init__(parent)
        self._url = url
        self._save_path = save_path
        self._proxy = proxy
        self._cancelled = False

    def run(self) -> None:
        """执行下载。若配置了代理且代理连接失败/超时，自动降级为直连重试一次。"""
        # 已配置代理时优先使用代理；连接或超时错误由调用方改为直连重试。
        if self._proxy:
            if self._download_once(self._proxy, degrade_ok=True):
                return
            if self._cancelled:
                return
            logger.warning("下载经代理失败，降级直连重试: %s", self._url)
        self._download_once(None, degrade_ok=False)

    def _download_once(self, proxy: str | None, degrade_ok: bool) -> bool:
        """执行一次下载，成功时发送 finished 信号并返回 True。

        degrade_ok=True 时，代理、连接或超时错误返回 False，由调用方改为直连重试。
        其他下载错误发送 error 信号并返回 False；取消请求不发送错误信号。
        """
        try:
            Path(self._save_path).parent.mkdir(parents=True, exist_ok=True)

            with httpx.stream("GET", self._url, follow_redirects=True, timeout=60, proxy=proxy) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0

                with open(self._save_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=8192):
                        if self._cancelled:
                            return False
                        f.write(chunk)
                        downloaded += len(chunk)
                        self.progress.emit(downloaded, total)

            self.finished.emit(self._save_path)
            logger.info("下载完成: %s", self._save_path)
            return True

        except Exception as e:
            if self._cancelled:
                return False
            # 允许直连重试时，连接相关错误由调用方处理，不提前发送错误信号。
            if degrade_ok and isinstance(
                e, (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout)
            ):
                logger.warning("下载代理失败（可降级）: %s", e)
                return False
            self.error.emit(str(e))
            logger.error("下载失败: %s", e)
            return False

    @Slot()
    def cancel(self) -> None:
        """取消下载"""
        self._cancelled = True


class NetworkService(QObject):
    """网络请求服务"""

    def __init__(self, parent: QObject | None = None) -> None:
        """创建按下载地址管理活动工作线程的注册表，由 Qt 父对象管理服务生命周期。"""
        super().__init__(parent)
        self._active_downloads: dict[str, DownloadWorker] = {}

    def download_file(
        self,
        url: str,
        save_path: str | Path,
        on_progress: Any = None,
        on_finished: Any = None,
        on_error: Any = None,
        proxy: str | None = None,
    ) -> DownloadWorker:
        """异步下载文件

        参数：
            url: 下载 URL
            save_path: 保存路径
            on_progress: 进度回调 (downloaded, total)
            on_finished: 完成回调 (file_path)
            on_error: 错误回调 (error_message)
            proxy: 代理 URL；提供时优先经代理下载，代理连接/超时失败自动降级直连

        返回：
            下载线程对象
        """
        # 服务可能先于网络读取结束；原生线程由活动集合保活，不随服务的 Qt 父子关系销毁。
        worker = DownloadWorker(url, str(save_path), proxy=proxy)
        self.destroyed.connect(worker.cancel)

        if on_progress:
            worker.progress.connect(on_progress)
        if on_finished:
            worker.finished.connect(on_finished)
        if on_error:
            worker.error.connect(on_error)

        service_ref = weakref.ref(self)

        def cleanup() -> None:
            """原生线程退出后解除当前任务登记及保活，不影响同地址的后续下载。"""
            service = service_ref()
            if service is not None and service._active_downloads.get(url) is worker:
                del service._active_downloads[url]
            _running_downloads.discard(worker)
            try:
                worker.deleteLater()
            except RuntimeError:
                pass

        # finished(str) 是业务结果，可能在 run 返回前发出；字符串签名显式选择 QThread 原生结束信号。
        QObject.connect(worker, SIGNAL("finished()"), cleanup, Qt.ConnectionType.QueuedConnection)

        self._active_downloads[url] = worker
        _running_downloads.add(worker)
        app = QCoreApplication.instance()
        if app is not None and app not in _connected_apps:
            _connected_apps.add(app)
            app.aboutToQuit.connect(_finish_downloads)
        worker.start()
        return worker
