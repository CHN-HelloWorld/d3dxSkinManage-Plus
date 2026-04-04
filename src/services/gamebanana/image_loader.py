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

    def _cached_pixmap(self, key: _ImageKey, *, promote: bool) -> QPixmap | None:
        """读取已浏览或预取缓存，按需提升预取条目，并在界面线程转换为可显示像素对象。"""
        generation = self._memory_cache.generation
        pixmap = self._memory_cache.get(key)
        if pixmap is None:
            pixmap = self._prefetch_cache.get(key)
            if pixmap is not None and promote:
                self._prefetch_cache.remove(key)
                self._memory_cache.put(key, pixmap, expected_generation=generation)
        return QPixmap.fromImage(pixmap) if isinstance(pixmap, QImage) else pixmap

    def try_cached(
        self,
        url: str,
        target,
        *,
        size: QSize | None = None,
        crop_to_size: bool = False,
    ) -> bool:
        """立即恢复已完成的像素，不重新下载或排队。"""
        with self._request_lock:
            if self._shutting_down:
                return False
            pixmap = self._cached_pixmap(self._image_key(url, size, crop_to_size), promote=True)
            if pixmap is None:
                return False
            self.cancel_for(target)
            try:
                target.setPixmap(pixmap)
            except RuntimeError:
                return False
            return True

    def load(
        self,
        url: str,
        target,
        *,
        size: QSize | None = None,
        placeholder: QPixmap | None = None,
        crop_to_size: bool = False,
    ) -> None:
        """撤销控件旧请求，优先使用缓存；未命中时设置占位图并登记具有独立身份的共享加载请求。"""
        target_id = id(target)
        loader_ref = weakref.ref(self)

        def _target_collected(ref) -> None:
            """控件被回收时撤销仍对应同一弱引用的请求，避免误删标识复用后的新请求。"""
            loader = loader_ref()
            if loader is not None:
                with loader._request_lock:
                    current = loader._inflight.get(target_id)
                    if current is not None and current.target_ref is ref:
                        loader._retire_request(current)

        with self._request_lock:
            if self._shutting_down:
                return
            # 先撤销旧请求，避免其排队结果满足新的同 URL 请求。
            self.cancel_for(target)
            if self.try_cached(url, target, size=size, crop_to_size=crop_to_size):
                return
            if placeholder is not None:
                target.setPixmap(placeholder)
            if self._loop is None or self._loop.is_closed():
                return
            key = self._image_key(url, size, crop_to_size)
            work = self._work_for(key, size)
            request = _ImageRequest(target_id, weakref.ref(target, _target_collected), work)
            self._inflight[target_id] = request
            work.requests[target_id] = request
            self._dispatch()

    def prefetch(
        self,
        url: str,
        *,
        size: QSize | None = None,
        crop_to_size: bool = False,
    ) -> bool:
        """无控件时预取像素；有界队列已满则返回 False。"""
        with self._request_lock:
            if (
                self._shutting_down
                or self._loop is None
                or self._loop.is_closed()
                or not image_cache_budget.prefetch_allowed
            ):
                return False
            key = self._image_key(url, size, crop_to_size)
            if self._cached_pixmap(key, promote=False) is not None:
                return True
            work = self._works.get(key)
            if work is None or not work.prefetch:
                pending = sum(item.prefetch for item in self._works.values())
                if pending >= _MAX_PENDING_PREFETCH:
                    return False
            if work is None:
                work = self._work_for(key, size)
            work.prefetch = True
            self._dispatch()
            return True

    def cancel_prefetch(
        self,
        url: str,
        *,
        size: QSize | None = None,
        crop_to_size: bool = False,
    ) -> None:
        """撤销预取需求，保留仍有可见消费者的工作。"""
        with self._request_lock:
            work = self._works.get(self._image_key(url, size, crop_to_size))
            if work is not None:
                work.prefetch = False
                if not work.requests:
                    self._discard_work(work)

    def _work_for(self, key: _ImageKey, size: QSize | None) -> _ImageWork:
        """复用同键工作或创建排队工作，创建时复制目标尺寸并捕获两类缓存代次。"""
        work = self._works.get(key)
        if work is None:
            work = _ImageWork(key, QSize(size) if size is not None else None)
            work.cache_generations = self._memory_cache.generation, self._prefetch_cache.generation
            self._works[key] = work
            self._queued[key] = work
        return work

    def _dispatch(self) -> None:
        """在请求锁内调用，对获取、转换和投递统一限制并发。"""
        while not self._shutting_down and len(self._active) < _MAX_ACTIVE_IMAGES:
            work = next((item for item in self._queued.values() if item.requests), None)
            if work is None:
                if not image_cache_budget.prefetch_allowed:
                    # 丢弃排队的预取，可见请求仍可重新发起。
                    for queued in list(self._queued.values()):
                        self._queued.pop(queued.key, None)
                        self._works.pop(queued.key, None)
                    return
                if sum(not item.requests for item in self._active) >= _MAX_ACTIVE_PREFETCH:
                    return
                work = next(iter(self._queued.values()), None)
            if work is None:
                return
            self._queued.pop(work.key)
            self._active.add(work)
            coroutine = self._cache.fetch(work.key[0])
            try:
                future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
            except RuntimeError:
                coroutine.close()
                self._finish_work(work)
                continue
            work.future = future
            loader_ref = weakref.ref(self)

            def fetched(done, item=work, reference=loader_ref):
                """下载完成后通过弱引用转交工作结果，加载器已回收时不再访问其状态。"""
                loader = reference()
                if loader is not None:
                    loader._on_fetch_done(done, item)

            future.add_done_callback(fetched)

    def cancel_for(self, target) -> None:
        """在请求锁保护下撤销指定控件当前的图像请求。"""
        with self._request_lock:
            request = self._inflight.get(id(target))
            if request is not None:
                self._retire_request(request)

    def _is_current(self, work: _ImageWork) -> bool:
        """确认加载器未关闭且该键仍指向同一工作对象，防止旧工作覆盖新请求。"""
        return not self._shutting_down and self._works.get(work.key) is work

    def _retire_request(self, request: _ImageRequest) -> None:
        """仅撤销仍在注册表中的请求；没有接收者且无需预取时使共享工作失效。"""
        if self._inflight.get(request.target_id) is not request:
            return
        self._inflight.pop(request.target_id)
        work = request.work
        work.requests.pop(request.target_id, None)
        if not work.requests and not work.prefetch:
            self._discard_work(work)

    def _discard_work(self, work: _ImageWork) -> None:
        """移除失效工作并尝试取消图像处理；共享下载继续收尾，完成前保留其并发占用。"""
        if self._works.get(work.key) is work:
            self._works.pop(work.key)
            self._queued.pop(work.key, None)
        # 原始图像请求可能共享 Future；等待其完成，
        # 取消一个协程可能同时取消其他消费者的下载。
        # 已过期但仍活动的工作在回调完成前继续占用并发名额。
        if work.scaling and work.future is not None:
            work.future.cancel()
        self._dispatch()

    def _finish_work(self, work: _ImageWork) -> None:
        """清除本项仍有效的接收者与工作引用，释放并发名额并继续调度队列。"""
        if self._works.get(work.key) is work:
            self._works.pop(work.key)
            self._queued.pop(work.key, None)
        for request in work.requests.values():
            if self._inflight.get(request.target_id) is request:
                self._inflight.pop(request.target_id)
        work.requests.clear()
        work.future = None
        self._active.discard(work)
        self._dispatch()

    def _on_fetch_done(self, future: Future, work: _ImageWork) -> None:
        """在锁内核对下载工作身份，成功后交给图像线程池处理，失败时转发空结果。"""
        with self._request_lock:
            if not self._is_current(work):
                self._finish_work(work)
                return
            try:
                path = future.result()
            except BaseException as exc:
                if not _is_shutdown_noise(exc):
                    logger.warning("ImageLoader fetch failed: %s (%s)", work.key[0], exc)
                self._emit_finished(work, None)
                return
            try:
                transformed = self._executor.submit(
                    _transform_pixmap,
                    str(path),
                    work.size,
                    work.key[3],
                )
            except RuntimeError:
                self._finish_work(work)
                return
            work.future = transformed
            work.scaling = True
            loader_ref = weakref.ref(self)

            def scaled(done):
                """通过加载器弱引用转交图像处理完成结果，避免后台回调持有已关闭的加载器。"""
                loader = loader_ref()
                if loader is not None:
                    loader._on_scale_done(done, work)

            transformed.add_done_callback(scaled)

    def _on_scale_done(self, future: Future, work: _ImageWork) -> None:
        """接收仍有效的图像处理结果，记录非关闭异常并将结果排队送回界面线程。"""
        with self._request_lock:
            if not self._is_current(work):
                self._finish_work(work)
                return
            try:
                pixmap = future.result()
            except BaseException as exc:
                if not _is_shutdown_noise(exc):
                    logger.warning("ImageLoader scale failed: %s (%s)", work.key[0], exc)
                pixmap = None
            self._emit_finished(work, pixmap)

    def _emit_finished(self, work: _ImageWork, pixmap: QImage | None) -> None:
        """发送携带工作身份的图像结果；信号对象已销毁时直接释放工作状态。"""
        try:
            self._signals.finished.emit(work, work.key[0], pixmap)
        except RuntimeError:
            self._finish_work(work)

    def _on_finished(self, work: _ImageWork, url: str, pixmap: QImage | QPixmap | None) -> None:
        """在界面线程再次验证工作与接收者身份，按原缓存代次入库并更新仍绑定本请求的控件。"""
        with self._request_lock:
            # 仅在界面投递时确认请求仍有效，避免取消或重新绑定前排队的结果被使用。

            if not self._is_current(work):
                self._finish_work(work)
                return
            requests = list(work.requests.values())
            valid = pixmap is not None and not pixmap.isNull()
            if valid:
                cache = self._memory_cache if requests else self._prefetch_cache
                generation = work.cache_generations[0 if requests else 1]
                image = pixmap if isinstance(pixmap, QImage) else pixmap.toImage()
                cache.put(work.key, image, expected_generation=generation)
                pixmap = QPixmap.fromImage(image)
            # 新请求不能加入正在投递的工作项；每次调用控件前重新检查接收者，
            # 因为 setPixmap 的重写方法可能同步重新绑定其他消费者。

            self._works.pop(work.key)
            if valid:
                for request in requests:
                    if self._inflight.get(request.target_id) is not request:
                        continue
                    self._inflight.pop(request.target_id)
                    work.requests.pop(request.target_id, None)
                    target = request.target_ref()
                    if target is not None:
                        try:
                            target.setPixmap(pixmap)
                        except RuntimeError:
                            pass
            self._finish_work(work)

    def _shutdown(self) -> None:
        """幂等撤销请求并清空缓存，请求后台资源退出；网络线程最多等待两秒，图像执行器不阻塞等待。"""
        with self._request_lock:
            if self._shutting_down:
                return
            self._shutting_down = True
            futures = []
            for work in self._active:
                if work.future is not None:
                    futures.append(work.future)
                    work.future = None
                work.requests.clear()
            self._inflight.clear()
            self._works.clear()
            self._queued.clear()
            self._active.clear()
            self._memory_cache.clear()
            self._prefetch_cache.clear()
            image_cache_budget.unregister(self._memory_cache)
            image_cache_budget.unregister(self._prefetch_cache)
        # 先断开界面更新信号，避免关闭期间的异步结果访问已销毁控件。
        try:
            self._signals.finished.disconnect()
        except (TypeError, RuntimeError):
            # 信号未连接或对象已销毁时无需处理。
            pass
        # 取消在途 Future，使请求在事件循环关闭前完成取消处理。
        for fut in futures:
            try:
                fut.cancel()
            except Exception:
                pass
        # 先停止网络事件循环并等待任务取消。
        try:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass
        try:
            if self._loop_thread is not None:
                self._loop_thread.join(timeout=2)
        except Exception:
            pass
        # 再关闭图像处理执行器。
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def shutdown(self) -> None:
        """终止加载任务和事件循环，避免关闭应用后残留未完成的异步工作。"""
        self._shutdown()

    def __del__(self) -> None:
        """析构时尽力触发幂等关闭，避免资源清理异常逃出析构函数。"""
        try:
            self._shutdown()
        except Exception:
            pass
