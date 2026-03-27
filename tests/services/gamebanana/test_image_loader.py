import asyncio
import gc
import threading
import weakref
from concurrent.futures import CancelledError, Future

import pytest
import shiboken6
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QLabel

from src.services.gamebanana.cache import ImageCache
from src.services.gamebanana.image_loader import ImageLoader

pytestmark = pytest.mark.usefixtures("temp_data_dir")


@pytest.fixture
def loader_factory():
    loaders = []

    def create(cache):
        loader = ImageLoader(cache)
        loaders.append(loader)
        return loader

    yield create
    for loader in loaders:
        loader.shutdown()


@pytest.fixture
def png_bytes():
    """1x1 PNG (有效，由 PIL 生成；spec 原 hex 的 IDAT CRC 错误，libpng 会拒绝)"""
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
        "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae426082"
    )


def test_loader_uses_placeholder_first(qtbot, tmp_path, png_bytes, loader_factory):
    cache = ImageCache(base_dir=tmp_path)
    cache.get_path("https://example.com/x.png").write_bytes(png_bytes)
    loader = loader_factory(cache)
    label = QLabel()
    qtbot.addWidget(label)
    placeholder = QPixmap(8, 8)
    placeholder.fill()
    loader.load("https://example.com/x.png", label, placeholder=placeholder)
    pm = label.pixmap()
    assert pm is not None
    assert not pm.isNull()


def test_loader_sets_pixmap_on_success(qtbot, tmp_path, png_bytes, respx_mock, loader_factory):
    import httpx

    respx_mock.get("https://example.com/y.png").mock(return_value=httpx.Response(200, content=png_bytes))
    cache = ImageCache(base_dir=tmp_path)
    loader = loader_factory(cache)
    label = QLabel()
    qtbot.addWidget(label)
    loader.load("https://example.com/y.png", label, size=QSize(32, 32))
    qtbot.waitUntil(lambda: not label.pixmap().isNull(), timeout=5000)


def test_loader_cancel_for_target_no_crash(qtbot, tmp_path, png_bytes, loader_factory):
    cache = ImageCache(base_dir=tmp_path)
    cache.get_path("https://example.com/z.png").write_bytes(png_bytes)
    loader = loader_factory(cache)
    label = QLabel()
    qtbot.addWidget(label)
    loader.load("https://example.com/z.png", label)
    loader.cancel_for(label)
    # 验证调用不会抛出异常。


class _ControlledCache:
    """保持真实事件调度，由测试按顺序释放请求。"""

    def __init__(self):
        self.requests = []
        self.changed = threading.Condition()

    async def fetch(self, url):
        future = Future()
        with self.changed:
            self.requests.append((url, future))
            self.changed.notify_all()
        return await asyncio.wrap_future(future)

    def wait_for_requests(self, count):
        with self.changed:
            assert self.changed.wait_for(lambda: len(self.requests) >= count, timeout=3)


class _ControlledTransforms:
    """在后台执行真实图像变换，由测试控制完成时机。"""

    def __init__(self):
        self.jobs = []

    def submit(self, transform, *args):
        future = Future()
        # 模拟已运行的任务，仅取消请求不能阻止过期结果到达。
        future.set_running_or_notify_cancel()
        self.jobs.append((future, transform, args))
        return future

    def finish(self, index, error=None):
        future, transform, args = self.jobs[index]

        def run():
            try:
                if error is not None:
                    raise error
                result = transform(*args)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

        worker = threading.Thread(target=run)
        worker.start()
        worker.join(timeout=3)
        assert not worker.is_alive()

    def finish_pending(self):
        for index, (future, _transform, _args) in enumerate(self.jobs):
            if not future.done():
                self.finish(index)

    def shutdown(self, wait=False, cancel_futures=False):
        if cancel_futures:
            for future, _transform, _args in self.jobs:
                future.cancel()


def _flush_loop(loader):
    async def barrier():
        await asyncio.sleep(0)

    # 完成获取任务及其回调，但暂不分发 Qt 信号。
    asyncio.run_coroutine_threadsafe(barrier(), loader._loop).result(timeout=3)


@pytest.fixture
def controlled_loader(qapp, tmp_path, loader_factory):
    cache = _ControlledCache()
    loader = loader_factory(cache)
    loader._executor.shutdown()
    transforms = _ControlledTransforms()
    loader._executor = transforms
    paths = {}
    for name in ("red", "blue"):
        pixmap = QPixmap(8, 8)
        pixmap.fill(QColor(name))
        path = tmp_path / f"{name}.png"
        assert pixmap.save(str(path))
        paths[name] = path

    def resolve(index, color):
        cache.requests[index][1].set_result(paths[color])
        _flush_loop(loader)

    return loader, cache, transforms, resolve


def _new_label(qtbot):
    label = QLabel()
    qtbot.addWidget(label)
    placeholder = QPixmap(2, 2)
    placeholder.fill(QColor("white"))
    label.setPixmap(placeholder)
    return label


def _assert_pixels(label, color, size=4):
    pixmap = label.pixmap()
    assert not pixmap.isNull()
    assert pixmap.toImage().pixelColor(0, 0).name() == QColor(color).name()
    assert pixmap.size() == QSize(size, size)


@pytest.mark.parametrize("same_url", [False, True], ids=["A-to-B", "A-to-A"])
@pytest.mark.parametrize("order", [(0, 1), (1, 0)], ids=["old-first", "new-first"])
def test_rebind_rejects_stale_fetch_results(qtbot, qapp, controlled_loader, same_url, order):
    loader, cache, transforms, resolve = controlled_loader
    label = _new_label(qtbot)
    loader.load("A", label, size=QSize(8, 8))
    loader.load("A" if same_url else "B", label, size=QSize(4, 4))
    cache.wait_for_requests(2)

    for index in order:
        resolve(index, "red" if index == 0 else "blue")
        transforms.finish_pending()
        qapp.processEvents()

    _assert_pixels(label, "blue")
    assert not loader._inflight


@pytest.mark.parametrize("same_url", [False, True], ids=["A-to-B", "A-to-A"])
@pytest.mark.parametrize("order", [(0, 1), (1, 0)], ids=["old-first", "new-first"])
def test_rebind_rejects_stale_transform_results(qtbot, qapp, controlled_loader, same_url, order):
    loader, cache, transforms, resolve = controlled_loader
    label = _new_label(qtbot)
    loader.load("A", label, size=QSize(8, 8))
    cache.wait_for_requests(1)
    resolve(0, "red")
    loader.load("A" if same_url else "B", label, size=QSize(4, 4))
    cache.wait_for_requests(2)
    resolve(1, "blue")

    for index in order:
        # 获取完成时仍须保留 B 的请求。
        if index < len(transforms.jobs):
            transforms.finish(index)
        qapp.processEvents()

    _assert_pixels(label, "blue")
    assert not loader._inflight


@pytest.mark.parametrize("same_url", [False, True], ids=["A-to-B", "A-to-A"])
@pytest.mark.parametrize("deliver_old_first", [False, True], ids=["both-queued", "old-delivered-first"])
def test_rebind_rejects_result_already_queued_for_delivery(
    qtbot,
    qapp,
    controlled_loader,
    same_url,
    deliver_old_first,
):
    loader, cache, transforms, resolve = controlled_loader
    label = _new_label(qtbot)
    loader.load("A", label, size=QSize(8, 8))
    cache.wait_for_requests(1)
    resolve(0, "red")
    transforms.finish(0)  # 信号已从后台发出，Qt 尚未分发。

    loader.load("A" if same_url else "B", label, size=QSize(4, 4))
    cache.wait_for_requests(2)
    if deliver_old_first:
        qapp.processEvents()
        _assert_pixels(label, "white", size=2)
    resolve(1, "blue")
    transforms.finish_pending()
    qapp.processEvents()

    _assert_pixels(label, "blue")
    assert not loader._inflight


def test_cancelled_target_accepts_new_load_before_old_fetch_finishes(qtbot, qapp, controlled_loader):
    loader, cache, transforms, resolve = controlled_loader
    label = _new_label(qtbot)
    loader.load("A", label)
    cache.wait_for_requests(1)
    loader.cancel_for(label)
    loader.load("B", label, size=QSize(4, 4))
    cache.wait_for_requests(2)

    resolve(1, "blue")
    transforms.finish_pending()
    qapp.processEvents()
    _assert_pixels(label, "blue")

    resolve(0, "red")
    transforms.finish_pending()
    qapp.processEvents()
    _assert_pixels(label, "blue")
    assert not loader._inflight


def test_pending_request_does_not_keep_target_or_its_bookkeeping_alive(controlled_loader):
    loader, cache, _transforms, _resolve = controlled_loader
    label = QLabel()
    target_ref = weakref.ref(label)
    loader.load("A", label)
    cache.wait_for_requests(1)

    del label
    gc.collect()

    assert target_ref() is None
    assert not loader._inflight


def test_deleted_qt_target_releases_completed_bookkeeping(qapp, controlled_loader):
    loader, cache, transforms, resolve = controlled_loader
    label = QLabel()
    loader.load("A", label)
    cache.wait_for_requests(1)
    resolve(0, "red")
    shiboken6.delete(label)

    transforms.finish(0)
    qapp.processEvents()

    assert not loader._inflight


def test_cancelled_transform_keeps_placeholder_and_releases_bookkeeping(qtbot, qapp, controlled_loader):
    loader, cache, transforms, resolve = controlled_loader
    label = _new_label(qtbot)
    loader.load("A", label)
    cache.wait_for_requests(1)
    resolve(0, "red")

    transforms.finish(0, error=CancelledError())
    qapp.processEvents()

    _assert_pixels(label, "white", size=2)
    assert not loader._inflight


def test_shutdown_ignores_transform_callback_arriving_later(qtbot, qapp, controlled_loader, caplog):
    loader, cache, transforms, resolve = controlled_loader
    label = _new_label(qtbot)
    loader.load("A", label)
    cache.wait_for_requests(1)
    resolve(0, "red")

    loader.shutdown()
    transforms.finish(0, error=ValueError("late transform failure"))
    qapp.processEvents()

    _assert_pixels(label, "white", size=2)
    assert not loader._inflight
    assert not [record for record in caplog.records if "ImageLoader" in record.getMessage()]


def test_rebinding_one_target_preserves_a_shared_image_cache_fetch(
    qtbot,
    tmp_path,
    png_bytes,
    respx_mock,
    loader_factory,
):
    import httpx

    release_download = Future()
    release_download.set_running_or_notify_cancel()
    download_started = threading.Event()

    async def respond(request):
        download_started.set()
        await asyncio.wrap_future(release_download)
        return httpx.Response(200, content=png_bytes)

    url = "https://example.com/shared.png"
    respx_mock.get(url).mock(side_effect=respond)
    loader = loader_factory(ImageCache(base_dir=tmp_path))
    first = _new_label(qtbot)
    second = _new_label(qtbot)
    loader.load(url, first, size=QSize(8, 8))
    assert download_started.wait(timeout=3)
    loader.load(url, second, size=QSize(4, 4))
    _flush_loop(loader)

    loader.cancel_for(first)
    loader.load(url, first, size=QSize(4, 4))
    _flush_loop(loader)
    release_download.set_result(None)

    qtbot.waitUntil(lambda: first.pixmap().size() == second.pixmap().size() == QSize(4, 4), timeout=3000)
    _assert_pixels(first, "red")
    _assert_pixels(second, "red")
    assert not loader._inflight


def test_shutdown_ignores_queued_delivery_and_new_loads(qtbot, qapp, controlled_loader):
    loader, cache, transforms, resolve = controlled_loader
    label = _new_label(qtbot)
    loader.load("A", label)
    cache.wait_for_requests(1)
    resolve(0, "red")
    transforms.finish(0)

    loader.shutdown()
    qapp.processEvents()
    _assert_pixels(label, "white", size=2)
    replacement = QPixmap(4, 4)
    replacement.fill(QColor("blue"))
    loader.load("B", label, placeholder=replacement)

    _assert_pixels(label, "white", size=2)
    assert not loader._inflight


def test_reentrant_delivery_cannot_overwrite_another_rebound_consumer(
    qtbot,
    qapp,
    controlled_loader,
):
    loader, cache, transforms, resolve = controlled_loader
    second = _new_label(qtbot)
    loader.load("B", second, size=QSize(4, 4))
    cache.wait_for_requests(1)
    resolve(0, "blue")
    transforms.finish(0)
    qapp.processEvents()

    class RebindingLabel(QLabel):
        def setPixmap(self, pixmap):
            super().setPixmap(pixmap)
            loader.load("B", second, size=QSize(4, 4))

    first = RebindingLabel()
    qtbot.addWidget(first)
    loader.load("A", first, size=QSize(4, 4))
    loader.load("A", second, size=QSize(4, 4))
    cache.wait_for_requests(2)
    resolve(1, "red")
    transforms.finish(1)
    qapp.processEvents()

    _assert_pixels(first, "red")
    _assert_pixels(second, "blue")
    assert not loader._inflight


def test_retiring_visible_requests_cannot_overfill_prefetch_queue(qtbot, controlled_loader):
    loader, cache, _transforms, _resolve = controlled_loader
    accepted = []
    for index in range(60):
        target = _new_label(qtbot)
        url = f"image-{index}"
        loader.load(url, target, size=QSize(4, 4))
        accepted.append(loader.prefetch(url, size=QSize(4, 4)))
        loader.cancel_for(target)
    _flush_loop(loader)

    assert sum(accepted) <= 24
    assert len(cache.requests) <= 8


def test_returning_thumbnail_restores_pixels_without_fetch_or_transform(
    qtbot,
    qapp,
    controlled_loader,
):
    loader, cache, transforms, resolve = controlled_loader
    first = _new_label(qtbot)
    loader.load("A", first, size=QSize(4, 4), crop_to_size=True)
    cache.wait_for_requests(1)
    resolve(0, "red")
    transforms.finish(0)
    qapp.processEvents()
    _assert_pixels(first, "red")

    returning = _new_label(qtbot)
    loader.load("A", returning, size=QSize(4, 4), crop_to_size=True)

    _assert_pixels(returning, "red")  # 不执行额外事件轮次或后台操作。
    _flush_loop(loader)
    assert len(cache.requests) == len(transforms.jobs) == 1
    assert not loader._inflight


def test_try_cached_uses_transform_parameters_and_retires_replaced_target(
    qtbot,
    qapp,
    controlled_loader,
):
    loader, cache, transforms, resolve = controlled_loader
    label = _new_label(qtbot)
    loader.load("A", label, size=QSize(4, 4), crop_to_size=True)
    cache.wait_for_requests(1)
    resolve(0, "red")
    transforms.finish(0)
    qapp.processEvents()

    returning = _new_label(qtbot)
    assert not loader.try_cached("A", returning, size=QSize(4, 4))
    assert not loader.try_cached("A", returning, size=QSize(8, 8), crop_to_size=True)
    _assert_pixels(returning, "white", size=2)
    loader.load("B", returning)
    cache.wait_for_requests(2)
    assert loader.try_cached("A", returning, size=QSize(4, 4), crop_to_size=True)
    resolve(1, "blue")
    transforms.finish_pending()
    qapp.processEvents()

    _assert_pixels(returning, "red")
    assert not loader._inflight


def test_queued_same_key_result_cannot_be_admitted_after_rebinding(
    qtbot,
    qapp,
    controlled_loader,
):
    loader, cache, transforms, resolve = controlled_loader
    label = _new_label(qtbot)
    loader.load("A", label, size=QSize(4, 4))
    cache.wait_for_requests(1)
    resolve(0, "red")
    transforms.finish(0)
    loader.load("A", label, size=QSize(4, 4))
    cache.wait_for_requests(2)
    qapp.processEvents()

    other = _new_label(qtbot)
    assert not loader.try_cached("A", other, size=QSize(4, 4))
    _assert_pixels(label, "white", size=2)
    resolve(1, "blue")
    transforms.finish(1)
    qapp.processEvents()
    assert loader.try_cached("A", other, size=QSize(4, 4))
    _assert_pixels(label, "blue")
    _assert_pixels(other, "blue")


def test_prefetch_and_visible_consumers_share_fetch_and_transform(
    qtbot,
    qapp,
    controlled_loader,
):
    loader, cache, transforms, resolve = controlled_loader
    assert loader.prefetch("A", size=QSize(4, 4), crop_to_size=True)
    cache.wait_for_requests(1)
    assert loader.prefetch("A", size=QSize(4, 4), crop_to_size=True)
    first, second = _new_label(qtbot), _new_label(qtbot)
    loader.load("A", first, size=QSize(4, 4), crop_to_size=True)
    loader.load("A", second, size=QSize(4, 4), crop_to_size=True)
    loader.cancel_prefetch("A", size=QSize(4, 4), crop_to_size=True)
    loader.cancel_for(first)
    resolve(0, "red")
    transforms.finish_pending()
    qapp.processEvents()

    _assert_pixels(first, "white", size=2)
    _assert_pixels(second, "red")
    assert len(cache.requests) == len(transforms.jobs) == 1
    assert not loader._inflight


def test_target_free_prefetch_restores_pixels_synchronously(qtbot, qapp, controlled_loader):
    loader, cache, transforms, resolve = controlled_loader
    assert loader.prefetch("A", size=QSize(4, 4))
    cache.wait_for_requests(1)
    resolve(0, "red")
    transforms.finish(0)
    qapp.processEvents()

    returning = _new_label(qtbot)
    assert loader.try_cached("A", returning, size=QSize(4, 4))
    _assert_pixels(returning, "red")
    assert len(cache.requests) == len(transforms.jobs) == 1


def test_cancelled_prefetch_result_is_not_retained(qtbot, qapp, controlled_loader):
    loader, cache, transforms, resolve = controlled_loader
    assert loader.prefetch("A", size=QSize(4, 4))
    cache.wait_for_requests(1)
    resolve(0, "red")
    transforms.finish(0)
    loader.cancel_prefetch("A", size=QSize(4, 4))
    qapp.processEvents()

    assert not loader.try_cached("A", _new_label(qtbot), size=QSize(4, 4))


def test_prefetch_bounds_network_and_queue_and_keeps_capacity_for_visible(
    qtbot,
    controlled_loader,
):
    loader, cache, _transforms, _resolve = controlled_loader
    accepted = [loader.prefetch(f"P{i}", size=QSize(4, 4)) for i in range(100)]
    _flush_loop(loader)
    assert sum(accepted) <= 24
    assert len(cache.requests) <= 2

    loader.load("visible", _new_label(qtbot), size=QSize(4, 4))
    _flush_loop(loader)
    assert "visible" in [url for url, _future in cache.requests]


def test_prefetch_partition_preserves_viewed_images_and_promotes_without_duplicates(
    qtbot,
    qapp,
    controlled_loader,
):
    from src.utils.image_memory_cache import ImageMemoryCache

    loader, cache, transforms, resolve = controlled_loader
    loader._memory_cache = ImageMemoryCache(max_bytes=64)
    loader._prefetch_cache = ImageMemoryCache(max_bytes=64)
    label = _new_label(qtbot)
    loader.load("viewed", label, size=QSize(4, 4))
    cache.wait_for_requests(1)
    resolve(0, "red")
    transforms.finish(0)
    qapp.processEvents()
    for index, url in enumerate(("nearby-old", "nearby-new"), start=1):
        assert loader.prefetch(url, size=QSize(4, 4))
        cache.wait_for_requests(index + 1)
        resolve(index, "blue")
        transforms.finish(index)
        qapp.processEvents()

    assert loader._memory_cache.bytes_used + loader._prefetch_cache.bytes_used == 128
    assert loader.try_cached("viewed", label, size=QSize(4, 4))
    _assert_pixels(label, "red")
    assert not loader.try_cached("nearby-old", label, size=QSize(4, 4))
    assert loader.try_cached("nearby-new", label, size=QSize(4, 4))
    _assert_pixels(label, "blue")
    assert loader._memory_cache.bytes_used == 64
    assert loader._prefetch_cache.bytes_used == 0
    assert len(cache.requests) == len(transforms.jobs) == 3


def test_memory_eviction_keeps_recent_pixels_and_releases_cache_on_shutdown(
    qtbot,
    qapp,
    controlled_loader,
):
    from src.utils.image_memory_cache import ImageMemoryCache

    loader, cache, transforms, resolve = controlled_loader
    loader._memory_cache = ImageMemoryCache(max_bytes=128)  # 两张四乘四像素的 RGBA 图像。
    label = _new_label(qtbot)
    for index, (url, color) in enumerate((("A", "red"), ("B", "blue"))):
        loader.load(url, label, size=QSize(4, 4))
        cache.wait_for_requests(index + 1)
        resolve(index, color)
        transforms.finish(index)
        qapp.processEvents()
    assert loader.try_cached("A", label, size=QSize(4, 4))
    loader.load("C", label, size=QSize(4, 4))
    cache.wait_for_requests(3)
    resolve(2, "blue")
    transforms.finish(2)
    qapp.processEvents()

    assert loader._memory_cache.bytes_used <= 128
    assert not loader.try_cached("B", label, size=QSize(4, 4))
    assert loader.try_cached("A", label, size=QSize(4, 4))
    _assert_pixels(label, "red")
    loader.shutdown()
    assert loader._memory_cache.bytes_used == 0
    assert not loader.try_cached("A", label, size=QSize(4, 4))


@pytest.mark.parametrize("stage", ["fetch", "transform", "delivery"])
def test_clear_while_work_is_pending_displays_current_request_without_refilling_cache(
    qtbot,
    qapp,
    controlled_loader,
    stage,
):
    loader, cache, transforms, resolve = controlled_loader
    label = _new_label(qtbot)
    loader.load("clear-race", label, size=QSize(4, 4))
    cache.wait_for_requests(1)
    if stage != "fetch":
        resolve(0, "red")
    if stage == "delivery":
        transforms.finish(0)
    loader._memory_cache.clear()
    loader._prefetch_cache.clear()
    if stage == "fetch":
        resolve(0, "red")
    if stage != "delivery":
        transforms.finish(0)
    qapp.processEvents()
    _assert_pixels(label, "red")
    assert loader._memory_cache.bytes_used == 0
    assert loader._prefetch_cache.bytes_used == 0
    assert not loader._works and not loader._active and not loader._inflight


def test_live_loaders_share_process_budget_and_shutdown_releases_their_share(qapp, loader_factory):
    from src.utils.image_cache_budget import image_cache_budget

    first = loader_factory(_ControlledCache())
    first_allowance = first._memory_cache.max_bytes
    second = loader_factory(_ControlledCache())
    assert first._memory_cache.max_bytes + second._memory_cache.max_bytes <= first_allowance
    second.shutdown()
    assert second._memory_cache.max_bytes == 0
    assert first._memory_cache.max_bytes == first_allowance
    assert image_cache_budget.snapshot().bytes_used <= image_cache_budget.snapshot().limit_bytes


def test_worker_returns_qimage_and_gui_delivery_creates_pixmap(qtbot, qapp, controlled_loader):
    from PySide6.QtGui import QImage

    loader, cache, transforms, resolve = controlled_loader
    label = _new_label(qtbot)
    loader.load("qimage-worker", label, size=QSize(4, 4))
    cache.wait_for_requests(1)
    resolve(0, "blue")
    transforms.finish(0)
    assert isinstance(transforms.jobs[0][0].result(), QImage)
    qapp.processEvents()
    _assert_pixels(label, "blue")


def test_pressure_drops_queued_prefetch_but_still_loads_foreground(
    qtbot,
    qapp,
    controlled_loader,
    monkeypatch,
):
    from src.services.gamebanana import image_loader as loader_module
    from src.utils.image_cache_budget import ImageCacheBudget

    loader, cache, transforms, resolve = controlled_loader
    for index in range(6):
        assert loader.prefetch(f"speculative-{index}", size=QSize(4, 4))
    cache.wait_for_requests(2)
    assert len(loader._queued) == 4
    budget = ImageCacheBudget()
    for now in (0, 5, 10):
        budget.observe_memory(16 * 1024**3, 300 * 1024**2, now=now)
    # 保留真实缓存的所有者，仅隔离调度策略，避免内存压力影响其他用例。
    with monkeypatch.context() as policy_patch:
        policy_patch.setattr(loader_module, "image_cache_budget", budget)
        assert not loader.prefetch("new-speculation", size=QSize(4, 4))
        label = _new_label(qtbot)
        loader.load("foreground", label, size=QSize(4, 4))
        cache.wait_for_requests(3)
        assert not loader._queued
        assert cache.requests[2][0] == "foreground"
        resolve(2, "red")
        transforms.finish(0)
        qapp.processEvents()
        _assert_pixels(label, "red")


@pytest.mark.parametrize("pending", [False, True])
def test_unowned_loader_can_be_collected_without_leaking_its_loop(qtbot, pending):
    cache = _ControlledCache()
    loader = ImageLoader(cache)
    reference = weakref.ref(loader)
    loop_thread = loader._loop_thread
    if pending:
        target = _new_label(qtbot)
        loader.load("pending-at-collection", target, size=QSize(4, 4))
        cache.wait_for_requests(1)
    del loader
    gc.collect()
    try:
        qtbot.waitUntil(lambda: not loop_thread.is_alive(), timeout=3000)
        assert reference() is None
    finally:
        retained = reference()
        if retained is not None:
            retained.shutdown()
