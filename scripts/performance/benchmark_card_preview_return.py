"""使用互不相同的本地预览文件测量离屏滚动返回性能。

记录滚动多屏后的同步图像就绪时间和重复解码次数。计时范围为 Qt 事件处理，
不代表屏幕显示帧率。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from time import perf_counter

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from src.core import config as config_module
from src.models.mod import Mod
from src.ui import anim, theme
from src.ui.pages import gallery, overview
from src.ui.widgets import local_preview_cache
from src.utils.image_cache_budget import image_cache_budget


def _ready(card):
    """检查总览或画廊卡片是否已有缓存像素，并且加载占位已隐藏。"""
    label = getattr(card, "_preview_label", None)
    if label is None:
        label = card._image
    return card._cached_pixmap is not None and not label.pixmap().isNull() and label._shimmer.isHidden()


def _settle(page, timeout=20):
    """处理 Qt 事件直到所有预览及预取请求完成，超过截止时间时报告基准未就绪。"""
    deadline = perf_counter() + timeout
    while (
        not all(_ready(card) for card in page._vs_card_cache.values())
        or page._vs_image_requests
        or page._vs_pending_prefetch
        or page._vs_pixmap_timer.isActive()
    ):
        if perf_counter() > deadline:
            raise RuntimeError("previews did not settle within the timeout")
        QTest.qWait(5)


def measure(app, name, size, mods, screens):
    """在隔离缓存中测量跨屏滚动和返回的预览就绪时间及重复解码次数，结束后恢复解码入口。"""
    module = overview if name == "overview" else gallery
    page_type = module.OverviewPage if name == "overview" else module.GalleryPage
    local_preview_cache._images.clear()
    local_preview_cache._prefetched_images.clear()
    local_preview_cache._rendered_images.clear()
    reads = Counter()
    decode = module.load_scaled_qimage_file

    def counted_decode(path, *args, **kwargs):
        """按源路径统计实际解码次数，再调用原始图片解码函数。"""
        reads[str(path)] += 1
        return decode(path, *args, **kwargs)

    module.load_scaled_qimage_file = counted_decode
    page = page_type()
    try:
        page.resize(*size)
        page.show()
        app.processEvents()
        if name == "overview":
            page.set_current_mods(mods, mods)
        else:
            page.set_mods(mods)
        page._fresh_population = False
        page._vs_population_timer.stop()
        bar = page._scroll_area.verticalScrollBar()
        rows = math.ceil(page._scroll_area.viewport().height() / page._vs_row_h)
        origin = 10 * page._vs_row_h

        def scroll(position):
            """设置基准滚动位置并同步更新可见卡片，停止延后滚动更新以固定测量边界。"""
            bar.setValue(position)
            page._vs_update_visible()
            page._vs_scroll_timer.stop()

        scroll(origin)
        _settle(page)
        first_ids = set(page._vs_card_cache)
        first_paths = {card._mod.primary_preview for card in page._vs_card_cache.values()}
        next_index = max(int(item_id[-5:]) for item_id in first_ids) + 1
        next_row = mods[next_index : next_index + page._vs_cols]
        decode_size = overview._PixmapLoadWorker._MAX_CACHE_SIZE if name == "overview" else page._preview_size()
        # 只查看缓存键，避免更新 LRU 顺序或提升预取图像优先级。
        retained_keys = set()
        for cache in (local_preview_cache._images, local_preview_cache._prefetched_images):
            with cache._lock:
                retained_keys.update(cache._entries)
        prefetched_next_row = sum(
            local_preview_cache._key(
                mod.primary_preview,
                decode_size,
                name == "gallery",
            )
            in retained_keys
            for mod in next_row
        )
        initial_reads = {path: reads[path] for path in first_paths}
        warm_seconds = []
        for screen in range(1, screens):
            started = perf_counter()
            scroll(origin + screen * rows * page._vs_row_h)
            _settle(page)
            warm_seconds.append(round(perf_counter() - started, 3))
        started = perf_counter()
        scroll(origin)
        bind_ms = (perf_counter() - started) * 1000
        ready_now = sum(_ready(card) for card in page._vs_card_cache.values())
        assert set(page._vs_card_cache) == first_ids
        app.processEvents()
        event_ms = (perf_counter() - started) * 1000
        _settle(page)
        return {
            "page": name,
            "window": list(size),
            "screens_visited": screens,
            "source_image": [640, 480],
            "records": len(mods),
            "return_cards": len(first_ids),
            "ready_before_event_loop": ready_now,
            "prefetched_next_row": [prefetched_next_row, len(next_row)],
            "repeat_decodes_of_return_cards": sum(reads[path] - initial_reads[path] for path in first_paths),
            "return_bind_ms": round(bind_ms, 3),
            "return_bind_and_events_ms": round(event_ms, 3),
            "warm_next_screen_seconds": warm_seconds,
            "cache_mib": round(
                sum(
                    cache.bytes_used
                    for cache in (
                        local_preview_cache._images,
                        local_preview_cache._prefetched_images,
                        local_preview_cache._rendered_images,
                    )
                )
                / 1024**2,
                2,
            ),
        }
    finally:
        page.close()
        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        module.load_scaled_qimage_file = decode


def main():
    """校验页型、屏幕和缓存参数，生成互异临时预览文件并输出各场景的滚动返回指标。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", choices=("all", "overview", "gallery"), default="all")
    parser.add_argument("--size", choices=("all", "2k", "4k"), default="all")
    parser.add_argument("--screens", type=int, default=3)
    parser.add_argument("--cache-mb", type=int, default=768, help="shared RAM image-cache budget in MiB")
    args = parser.parse_args()
    if args.screens < 2 or args.screens > 5:
        parser.error("use between 2 and 5 screens")
    if not 64 <= args.cache_mb <= 8192:
        parser.error("--cache-mb must be between 64 and 8192")
    image_cache_budget.set_budget(args.cache_mb * 1024**2)
    app = QApplication([])
    anim.set_enabled(False)
    theme.set_theme("dark")
    theme.set_background_active(True)
    app.setStyleSheet(theme.render_qss("dark") + theme.background_overlay_qss("dark"))
    names = ("overview", "gallery") if args.page == "all" else (args.page,)
    sizes = {"2k": (2560, 1440), "4k": (3840, 2160)}
    if args.size != "all":
        sizes = {args.size: sizes[args.size]}
    with tempfile.TemporaryDirectory(prefix="card-preview-benchmark-") as temp:
        root = Path(temp)
        config_module.get_config_dir = lambda: root / "config"
        config_module.Config._instance = None
        data = []
        for index, color in enumerate(("#bf3546", "#3186c7")):
            picture = QImage(640, 480, QImage.Format.Format_RGB32)
            picture.fill(QColor(color))
            source = root / f"source-{index}.png"
            assert picture.save(str(source))
            data.append(source.read_bytes())
        mods = []
        for index in range(2000):
            path = root / f"preview-{index:05}.png"
            path.write_bytes(data[index % 2])
            mods.append(Mod(id=f"mod-{index:05}", name=f"Mod {index:05}", preview_images=[str(path)]))
        for size in sizes.values():
            for name in names:
                result = measure(app, name, size, mods, args.screens)
                result["configured_cache_mib"] = args.cache_mb
                print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
