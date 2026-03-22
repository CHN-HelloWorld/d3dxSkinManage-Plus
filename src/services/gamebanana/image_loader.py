"""将异步获取的图片投递到界面控件。

网络请求共用加载器所属线程的事件循环，避免跨事件循环复用 Future。
图像解码和缩放在工作线程执行，界面线程仅创建 QPixmap 并显示。
卡片可按比例填充并居中裁剪，详情和灯箱可按比例完整显示。
"""

from __future__ import annotations

import asyncio
import threading
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtGui import QImage, QPixmap

from src.services.gamebanana.cache import ImageCache
from src.utils import fs
from src.utils.image_cache_budget import image_cache_budget
from src.utils.image_memory_cache import ImageMemoryCache
from src.utils.logger import logger

_DEFAULT_WORKERS = 6
_MAX_ACTIVE_IMAGES = 8
_MAX_ACTIVE_PREFETCH = 2
_MAX_PENDING_PREFETCH = 24
_ImageKey = tuple[str, int, int, bool]


def _is_shutdown_noise(exc: BaseException | None) -> bool:
    """识别关闭任务、客户端和事件循环时产生的预期异常。

    取消任务及已关闭资源的访问异常不作为加载错误上报。
    """
    if exc is None:
        return False
    name = type(exc).__name__
    if name in {"ClosedResourceError", "CancelledError", "GeneratorExit"}:
        return True
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        if "closed" in msg:
            return True
    return False


class _ImageFetchSignals(QObject):
    """将工作身份与图像结果排队送回加载器所属的 Qt 线程。"""

    # 工作身份在排队投递期间保持独立，相同 URL 的新请求也不会复用旧身份。
    finished = Signal(object, str, object)


@dataclass(eq=False)
class _ImageRequest:
    """关联控件的弱引用与共享图像工作，保持每次控件请求的独立身份。"""

    target_id: int
    target_ref: weakref.ReferenceType
    work: _ImageWork


@dataclass(eq=False)
class _ImageWork:
    """合并同一 URL、尺寸和裁剪策略的图像工作，保存接收者、预取状态及缓存代次。"""

    key: _ImageKey
    size: QSize | None
    requests: dict[int, _ImageRequest] = field(default_factory=dict)
    prefetch: bool = False
    future: Future | None = None
    scaling: bool = False
    cache_generations: tuple[int, int] = (0, 0)


def _transform_pixmap(
    raw_path: str,
    size: QSize | None,
    crop_to_size: bool,
) -> QImage | None:
    """工作线程只解码和缩放 QImage，投递到界面线程后才创建 QPixmap。"""
    try:
        pixmap = QImage(fs.native_path(raw_path))
        if pixmap.isNull():
            return None
        if size is None or size.isEmpty():
            return pixmap
        if crop_to_size:
            scaled = pixmap.scaled(
                size,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = max(0, (scaled.width() - size.width()) // 2)
            y = max(0, (scaled.height() - size.height()) // 2)
            return scaled.copy(x, y, size.width(), size.height())
        return pixmap.scaled(
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    except Exception as exc:
        logger.warning("ImageLoader transform failed: %s", exc)
        return None


class ImageLoader(QObject):
    """加载远程图片到 QLabel.setPixmap。"""

    def __init__(self, cache: ImageCache, parent: QObject | None = None) -> None:
        """创建网络事件循环、图像处理线程池和受预算管理的缓存，并连接排队结果与对象销毁清理。"""
        super().__init__(parent)
        self._cache = cache
        # 耗费 CPU 的图像处理独立于异步网络循环和界面像素对象。
        self._executor = ThreadPoolExecutor(
            max_workers=_DEFAULT_WORKERS,
            thread_name_prefix="img-scale",
        )
        # 请求状态会跨越界面、异步网络和图像处理线程。
        self._request_lock = threading.RLock()
        self._shutting_down = False
        # 只保留活动请求；同一控件重复请求同一 URL 时也创建独立请求对象。

        self._inflight: dict[int, _ImageRequest] = {}
        self._memory_cache = ImageMemoryCache(max_bytes=0)
        self._prefetch_cache = ImageMemoryCache(max_bytes=0)
        image_cache_budget.register(self._memory_cache, "online_viewed")
        image_cache_budget.register(self._prefetch_cache, "online_prefetch")
        self._works: dict[_ImageKey, _ImageWork] = {}
        self._queued: dict[_ImageKey, _ImageWork] = {}
        self._active: set[_ImageWork] = set()
        # 全部网络协程提交到守护线程中的共享事件循环，
        # 使图片缓存中的 Future 始终属于同一循环。
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._loop_thread: threading.Thread | None = None
        self._start_loop()
        self._signals = _ImageFetchSignals()
        self._signals.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        loader_ref = weakref.ref(self)

        def _loader_destroyed(_=None) -> None:
            """通过弱引用在 Qt 对象销毁时请求释放后台资源，避免回调延长加载器生命周期。"""
            loader = loader_ref()
            if loader is not None:
                loader._shutdown()

        self.destroyed.connect(_loader_destroyed)

    def _start_loop(self) -> None:
        """启动仅以弱引用访问加载器的网络循环线程，并在限定时间内等待循环就绪。"""
        loader_ref = weakref.ref(self)
        ready = self._loop_ready

        def on_exception(loop, context):
            """加载器仍存在时转交循环异常，不由异常处理器持有加载器。"""
            loader = loader_ref()
            if loader is not None:
                loader._on_loop_exception(loop, context)

        def run():
            """在后台线程运行网络事件循环，退出时取消并收集剩余协程后关闭循环。"""
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loader = loader_ref()
            if loader is None:
                loop.close()
                ready.set()
                return
            loader._loop = loop
            # 线程入口和事件循环处理器不能持有加载器，
            # 否则无父对象的加载器无法通过析构终止事件循环。
            del loader
            # 异常处理器区分资源关闭产生的预期结果与实际加载错误。
            loop.set_exception_handler(on_exception)
            ready.set()
            try:
                loop.run_forever()
            finally:
                # 退出事件循环前取消并等待活动任务。
                try:
                    pending = asyncio.all_tasks(loop)
                    for t in pending:
                        t.cancel()
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except Exception:
                    pass
                loop.close()

        self._loop_thread = threading.Thread(
            target=run,
            name="img-loader-loop",
            daemon=True,
        )
        self._loop_thread.start()
        # 等待事件循环就绪，确保 load 可以立即提交请求。
        self._loop_ready.wait(timeout=2)

    def _on_loop_exception(self, loop, context: dict) -> None:
        """忽略预期关闭异常，其他异常交给默认处理器。"""
        exc = context.get("exception")
        if self._shutting_down or _is_shutdown_noise(exc):
            # 图片加载失败由界面占位状态表示，避免重复记录逐图请求日志。
            return
        loop.default_exception_handler(context)

    @staticmethod
    def _image_key(url: str, size: QSize | None, crop_to_size: bool) -> _ImageKey:
        """按 URL、有效尺寸与裁剪方式生成缓存键，未指定尺寸时统一使用原图键。"""
        if size is None or size.isEmpty():
            return url, 0, 0, False
        return url, size.width(), size.height(), bool(crop_to_size)
