"""使用真实 Qt 卡片和不同的本地 PNG 测量在线预览复用。

JSONL 记录绑定和 Qt 事件处理耗时，不表示画面帧率或网络速度。
每个用例使用独立 ImageLoader、实际图像变换、应用样式和边缘渐隐。
配置、图像和缓存均位于临时目录。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import threading
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from time import perf_counter

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtCore import QCoreApplication, QEvent, QSize
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

_COLORS = ("#bf3546", "#3186c7")
_RECORD_COUNT = 2000
_MIB = 1024**2


class _Counters:
    """在线程间汇总图片获取、变换和界面处理的次数及耗时。"""

    def __init__(self):
        """初始化受同一把锁保护的获取、变换和显示统计容器。"""
        self.lock = threading.Lock()
        self.fetches = Counter()
        self.transforms = Counter()
        self.display_calls = Counter()
        self.display_seconds = Counter()

    def snapshot(self):
        """在锁内复制全部计数器，返回可供独立比较的采样快照。"""
        with self.lock:
            return (self.fetches.copy(), self.transforms.copy(), self.display_calls.copy(), self.display_seconds.copy())

    def timed_display(self, name, operation):
        """包装显示处理函数，使成功和异常路径都记录调用次数及耗时。"""

        def measured(*args, **kwargs):
            """转发显示操作，并在退出时以加锁方式累计对应统计项。"""
            started = perf_counter()
            try:
                return operation(*args, **kwargs)
            finally:
                elapsed = perf_counter() - started
                with self.lock:
                    self.display_calls[name] += 1
                    self.display_seconds[name] += elapsed

        return measured


class _FileCache:
    """替换远程获取操作，保留真实的解码、缩放和裁剪。"""

    def __init__(self, paths, counters):
        """绑定合成 URL 到本地文件的映射及共享统计对象。"""
        self.paths = paths
        self.counters = counters

    async def fetch(self, url):
        """记录一次获取并返回对应本地图片路径，供真实图片加载器继续解码。"""
        with self.counters.lock:
            self.counters.fetches[url] += 1
        return self.paths[url]


def _ready(card):
    """同时检查加载占位和实际像素颜色，确认复用卡片显示的是当前记录的图片。"""
    sharp = card.thumbnail._sharp
    if sharp is None or sharp.isNull() or card.thumbnail.pixmap().isNull() or not card.thumbnail._shimmer.isHidden():
        return False
    # 同时校验加载标记和像素。成人内容模糊单独处理，圆角清晰图必须对应当前记录。
    return sharp.toImage().pixelColor(100, 80) == QColor(_COLORS[card.record.mod_id % 2])


def _settle(grid, loader, timeout=30):
    """处理 Qt 事件直到可见图片、加载器和预取队列均就绪，超时报告各项待办数量。"""
    deadline = perf_counter() + timeout
    while True:
        with loader._request_lock:
            outstanding = len(loader._active) + len(loader._queued) + len(loader._inflight)
        if (
            grid._cards
            and all(_ready(card) for card in grid._cards.values())
            and not outstanding
            and not grid._prefetch_queue
            and not grid._img_timer.isActive()
            and not grid._vs_timer.isActive()
        ):
            return
        if perf_counter() > deadline:
            raise RuntimeError(
                f"online previews did not settle: ready={sum(_ready(c) for c in grid._cards.values())}"
                f"/{len(grid._cards)}, outstanding={outstanding},"
                f" prefetch_pending={len(grid._prefetch_queue)}"
            )
        QTest.qWait(5)


def _cache_snapshot(loader):
    """直接读取占用量，避免 get 改变 LRU 顺序或提升缓存项优先级。"""
    result = {}
    with loader._request_lock:
        for name, cache in (("viewed", loader._memory_cache), ("prefetch", loader._prefetch_cache)):
            with cache._lock:
                result[name] = {
                    "urls": {key[0] for key in cache._entries},
                    "bytes": cache.bytes_used,
                    "budget_bytes": cache.max_bytes,
                }
    return result


def _prefetch_report(grid, records, snapshot):
    """比较预取窗口及下一行记录与缓存快照，统计无控件相邻行的缓存覆盖情况。"""
    viewed = snapshot["viewed"]["urls"]
    prefetched = snapshot["prefetch"]["urls"]
    cached = viewed | prefetched
    first, last = grid._prefetch_bounds
    expected = {records[index].thumbnail_url for index in range(first, last + 1)}
    next_row = max(grid._cards) // grid._cols + 1
    adjacent_indices = list(range(next_row * grid._cols, min(len(records), (next_row + 1) * grid._cols)))
    adjacent = {records[index].thumbnail_url for index in adjacent_indices}
    return {
        "data_window_records": len(expected),
        "data_window_cache_hits": len(expected & cached),
        "adjacent_row": next_row,
        "adjacent_row_cards": len(adjacent),
        "adjacent_row_cache_hits": len(adjacent & cached),
        "adjacent_row_prefetch_cache_hits": len(adjacent & prefetched),
        "adjacent_row_all_cached": bool(adjacent) and adjacent <= cached,
        "adjacent_row_has_no_widgets": all(index not in grid._cards for index in adjacent_indices),
        "adjacent_row_inside_data_window": adjacent <= expected,
    }


def _memory_report(snapshot):
    """将各图片缓存的占用和额度换算为 MiB，汇总总量并检查是否超出各自预算。"""
    result = {
        name: {"used_mib": round(entry["bytes"] / _MIB, 2), "budget_mib": round(entry["budget_bytes"] / _MIB, 2)}
        for name, entry in snapshot.items()
    }
    result["total_used_mib"] = round(sum(entry["bytes"] for entry in snapshot.values()) / _MIB, 2)
    result["total_budget_mib"] = round(sum(entry["budget_bytes"] for entry in snapshot.values()) / _MIB, 2)
    result["within_budget"] = all(entry["bytes"] <= entry["budget_bytes"] for entry in snapshot.values())
    return result


def measure(app, size, records, paths, screens, nsfw_every):
    """以真实图像处理链测量跨屏返回时的绑定、解码和显示开销，结束后释放加载器并恢复统计钩子。"""
    from src.services.gamebanana import image_loader as image_module
    from src.ui.widgets import gb_mod_card as card_module
    from src.ui.widgets.gb_adaptive_grid import GbAdaptiveGrid

    counters = _Counters()
    path_urls = {str(path): url for url, path in paths.items()}
    transform = image_module._transform_pixmap
    rounding = card_module._round_top_corners
    blur = card_module._blur_pixmap

    def counted_transform(raw_path, *args, **kwargs):
        """按图片对应 URL 累计变换次数，再执行原始解码缩放操作。"""
        with counters.lock:
            counters.transforms[path_urls[str(raw_path)]] += 1
        return transform(raw_path, *args, **kwargs)

    image_module._transform_pixmap = counted_transform
    card_module._round_top_corners = counters.timed_display("round", rounding)
    card_module._blur_pixmap = counters.timed_display("blur", blur)
    loader = image_module.ImageLoader(_FileCache(paths, counters))
    grid = GbAdaptiveGrid()
    constructed = 0
    rebound = 0
    thumb_size = QSize(card_module.GbModCard.CARD_WIDTH, card_module.GbModCard.THUMBNAIL_HEIGHT)

    def factory(record):
        """统计新建在线卡片数量，并将实际图片请求延后交给网格调度。"""
        nonlocal constructed
        constructed += 1
        return card_module.GbModCard(record, image_loader=loader, defer_image=True)

    def bind(card, record):
        """统计卡片重绑定次数，再为复用控件设置新的资源记录。"""
        nonlocal rebound
        rebound += 1
        card.set_record(record, defer_image=True)

    def prefetch(record):
        """按卡片缩略图尺寸提交裁剪预取，返回加载器是否接受请求。"""
        return loader.prefetch(record.thumbnail_url, size=thumb_size, crop_to_size=True)

    def cancel_prefetch(record):
        """使用相同尺寸及裁剪条件取消已移出窗口的预取请求。"""
        loader.cancel_prefetch(record.thumbnail_url, size=thumb_size, crop_to_size=True)

    try:
        grid.resize(*size)
        grid.show()
        app.processEvents()
        grid.set_items(
            records,
            has_more=False,
            card_factory=factory,
            card_binder=bind,
            image_prefetcher=prefetch,
            image_prefetch_canceller=cancel_prefetch,
        )
        bar = grid.scroll_area.verticalScrollBar()
        rows = math.ceil(grid.scroll_area.viewport().height() / grid._row_h)
        origin = 10 * grid._row_h

        def scroll(position):
            """立即设置并同步基准滚动位置，停止延后更新以固定计时边界。"""
            bar.setValue(position)
            grid._update_visible()
            grid._vs_timer.stop()

        started = perf_counter()
        scroll(origin)
        _settle(grid, loader)
        first_warm_seconds = perf_counter() - started
        first_indices = set(grid._cards)
        first_urls = {records[index].thumbnail_url for index in first_indices}
        first_fetches, first_transforms, _, _ = counters.snapshot()
        first_prefetch = _prefetch_report(grid, records, _cache_snapshot(loader))
        warm_constructed = constructed
        peak_active = len(grid._cards)
        warm_seconds = []
        for screen in range(1, screens):
            started = perf_counter()
            scroll(origin + screen * rows * grid._row_h)
            _settle(grid, loader)
            warm_seconds.append(round(perf_counter() - started, 3))
            peak_active = max(peak_active, len(grid._cards))

        before_return = counters.snapshot()
        before_rebound = rebound
        before_constructed = constructed
        started = perf_counter()
        scroll(origin)
        bind_ms = (perf_counter() - started) * 1000
        immediately_after = counters.snapshot()
        probe_started = perf_counter()
        ready_now = sum(_ready(card) for card in grid._cards.values())
        ready_probe_ms = (perf_counter() - probe_started) * 1000
        if set(grid._cards) != first_indices:
            raise RuntimeError("returning to the origin produced a different active window")
        app.processEvents()
        bind_events_ms = (perf_counter() - started) * 1000
        _settle(grid, loader)
        after_return = counters.snapshot()
        display_work = {
            name: {
                "calls": immediately_after[2][name] - before_return[2][name],
                "milliseconds": round((immediately_after[3][name] - before_return[3][name]) * 1000, 3),
            }
            for name in ("round", "blur")
        }
        return {
            "page": "online",
            "window": list(size),
            "viewport": [grid.scroll_area.viewport().width(), grid.scroll_area.viewport().height()],
            "records": len(records),
            "source_image": [640, 480],
            "screens_visited": screens,
            "first_row": 10,
            "rows_per_screen": rows,
            "nsfw_every": nsfw_every,
            "return_cards": len(first_indices),
            "ready_before_event_loop": ready_now,
            "all_ready_before_event_loop": ready_now == len(first_indices),
            "repeat_fetches_of_return_cards": sum(after_return[0][url] - first_fetches[url] for url in first_urls),
            "repeat_transforms_of_return_cards": sum(
                after_return[1][url] - first_transforms[url] for url in first_urls
            ),
            "return_fetches_including_settle": sum(after_return[0].values()) - sum(before_return[0].values()),
            "return_transforms_including_settle": sum(after_return[1].values()) - sum(before_return[1].values()),
            "return_bind_ms": round(bind_ms, 3),
            "return_bind_and_events_ms": round(bind_events_ms, 3),
            "return_ready_probe_ms_included_above": round(ready_probe_ms, 3),
            "return_display_work": display_work,
            "return_rebound_cards": rebound - before_rebound,
            "new_widgets_on_return": constructed - before_constructed,
            "new_widgets_after_warmup": constructed - warm_constructed,
            "peak_active_cards": max(peak_active, len(grid._cards)),
            "first_warm_seconds": round(first_warm_seconds, 3),
            "warm_next_screen_seconds": warm_seconds,
            "first_warm_prefetch": first_prefetch,
            "cache": _memory_report(_cache_snapshot(loader)),
            "io": "local PNG files; no network",
            "timing_scope": "instrumented Qt work, not presented FPS or real network speed",
        }
    finally:
        grid.clear()
        loader.shutdown()
        # 发生错误时先等待 CPU 任务结束，再清理临时图像和测量钩子。
        loader._executor.shutdown(wait=True, cancel_futures=True)
        grid.close()
        grid.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        image_module._transform_pixmap = transform
        card_module._round_top_corners = rounding
        card_module._blur_pixmap = blur


def _make_records(root, nsfw_every):
    """生成具有独立文件路径和合成 URL 的图片记录，并按参数标记分级样本。"""
    from src.services.gamebanana.models import GbAuthor, GbModel, GbModRecord

    image_bytes = []
    for index, color in enumerate(_COLORS):
        image = QImage(640, 480, QImage.Format.Format_RGB32)
        image.fill(QColor(color))
        path = root / f"source-{index}.png"
        if not image.save(str(path)):
            raise RuntimeError(f"could not save synthetic source {path}")
        image_bytes.append(path.read_bytes())
    records, paths = [], {}
    for index in range(_RECORD_COUNT):
        path = root / f"preview-{index:05}.png"
        path.write_bytes(image_bytes[index % len(image_bytes)])
        url = f"https://preview-benchmark.invalid/{index:05}.png"
        paths[url] = path
        records.append(
            GbModRecord(
                mod_id=index,
                model=GbModel.MOD,
                name=f"Mod {index:05}",
                submitter=GbAuthor(1, "Author", "", ""),
                thumbnail_url=url,
                page_url=f"https://preview-benchmark.invalid/mods/{index}",
                view_count=100 + index,
                like_count=10,
                download_count=5,
                comment_count=0,
                is_nsfw=bool(nsfw_every and index % nsfw_every == 0),
                date_updated_ts=0,
            )
        )
    return records, paths


def main():
    """校验窗口、缓存和分级参数，在临时应用目录中测量在线预览返回并输出独立 JSON 结果。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", choices=("all", "2k", "4k"), default="all")
    parser.add_argument("--screens", type=int, default=3)
    parser.add_argument("--cache-mb", type=int, default=768, help="shared RAM image-cache budget in MiB")
    parser.add_argument(
        "--nsfw-every", type=int, default=0, help="blur every Nth record; 0 keeps the baseline free of NSFW cards"
    )
    args = parser.parse_args()
    if not 2 <= args.screens <= 5:
        parser.error("use between 2 and 5 screens")
    if args.nsfw_every < 0:
        parser.error("--nsfw-every must be non-negative")
    if not 64 <= args.cache_mb <= 8192:
        parser.error("--cache-mb must be between 64 and 8192")
    sizes = {"2k": (2560, 1440), "4k": (3840, 2160)}
    if args.size != "all":
        sizes = {args.size: sizes[args.size]}

    # 导入配置模块前替换路径函数；保留真实资源路径，以使用应用的样式和图标。
    from src.utils import paths as paths_module

    original_app_dir = paths_module.get_app_dir
    with tempfile.TemporaryDirectory(prefix="online-preview-benchmark-") as temporary:
        root = Path(temporary)
        paths_module.get_app_dir = lambda: root
        try:
            with redirect_stdout(sys.stderr):
                from src.core import config as config_module
                from src.ui import anim, theme
                from src.utils.image_cache_budget import image_cache_budget

                config_module.Config._instance = None
                image_cache_budget.set_budget(args.cache_mb * _MIB)
                app = QApplication([])
                anim.set_enabled(False)
                theme.set_theme("dark")
                theme.set_background_active(True)
                app.setStyleSheet(theme.render_qss("dark") + theme.background_overlay_qss("dark"))
                records, paths = _make_records(root, args.nsfw_every)
            for size in sizes.values():
                with redirect_stdout(sys.stderr):
                    result = measure(app, size, records, paths, args.screens, args.nsfw_every)
                result["configured_cache_mib"] = args.cache_mb
                print(json.dumps(result), flush=True)
        finally:
            paths_module.get_app_dir = original_app_dir


if __name__ == "__main__":
    main()
