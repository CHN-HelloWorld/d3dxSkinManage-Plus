"""使用合成数据进行可重复的离屏卡片滚动基准测试。

从仓库根目录使用项目虚拟环境运行。计时包含 Qt 事件处理，不代表屏幕显示帧率。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
from pathlib import Path
from time import perf_counter

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from src.core import config as config_module
from src.models.mod import Mod
from src.services.gamebanana.models import GbAuthor, GbModel, GbModRecord
from src.ui import anim, theme
from src.ui.pages.gallery import GalleryItem, GalleryPage
from src.ui.pages.overview import OverviewPage
from src.ui.widgets.gb_adaptive_grid import GbAdaptiveGrid
from src.ui.widgets.gb_mod_card import GbModCard
from src.ui.widgets.mod_card import ModCard


def _summary(values):
    """汇总非空耗时样本的中位数、第九十五百分位和最大值，统一保留毫秒精度。"""
    values = sorted(values)
    return {
        "median_ms": round(statistics.median(values), 3),
        "p95_ms": round(values[min(len(values) - 1, int(len(values) * 0.95))], 3),
        "max_ms": round(max(values), 3),
    }


def measure(app, name, size, records, steps):
    """统计指定页面往返滚动中的卡片构造量及事件处理耗时，结束后恢复构造方法并清理页面。"""
    card_type = {"overview": ModCard, "gallery": GalleryItem, "online": GbModCard}[name]
    original_init = card_type.__init__
    constructed = 0

    def counted_init(card, *args, **kwargs):
        """累计真实卡片构造次数，并完整转发原构造方法的参数。"""
        nonlocal constructed
        constructed += 1
        original_init(card, *args, **kwargs)

    card_type.__init__ = counted_init
    page = {"overview": OverviewPage, "gallery": GalleryPage, "online": GbAdaptiveGrid}[name]()
    page.resize(*size)
    page.show()
    app.processEvents()
    try:
        if name == "online":
            items = [
                GbModRecord(
                    mod_id=i,
                    model=GbModel.MOD,
                    name=f"Mod {i:05}",
                    submitter=GbAuthor(1, "Author", "", ""),
                    thumbnail_url="",
                    page_url="",
                    view_count=10,
                    like_count=2,
                    download_count=1,
                    comment_count=0,
                    is_nsfw=False,
                    date_updated_ts=0,
                )
                for i in range(records)
            ]

            def factory(record):
                """为在线滚动基准创建延后读图的卡片，使统计聚焦控件构造和绑定。"""
                return GbModCard(record, image_loader=None, defer_image=True)

            page.set_items(
                items,
                has_more=False,
                card_factory=factory,
                card_binder=lambda card, record: card.set_record(record, defer_image=True),
            )
            area, update, cache = page.scroll_area, page._update_visible, page._cards
            row_h = page._row_h
            timers = (page._vs_timer, page._img_timer, page._scroll_idle)
        else:
            mods = [Mod(mod_id=f"mod-{i:05}", name=f"Mod {i:05}") for i in range(records)]
            if name == "overview":
                page.set_current_mods(mods, mods)
            else:
                page.set_mods(mods)
            page._vs_population_timer.stop()
            page._fresh_population = False
            area, update, cache = page._scroll_area, page._vs_update_visible, page._vs_card_cache
            row_h = page._vs_row_h
            timers = (page._vs_scroll_timer, page._vs_pixmap_timer, page._vs_population_timer)
        app.processEvents()
        bar = area.verticalScrollBar()
        start = min(bar.maximum() // 4, row_h * 20)
        bar.setValue(start)
        update()
        for timer in timers:
            timer.stop()
        app.processEvents()
        before = constructed
        peak_active = len(cache)
        binding_times, event_times = [], []
        positions = [start + i * 67 for i in range(steps)]
        for position in positions + list(reversed(positions)):
            began = perf_counter()
            bar.setValue(position)
            update()
            binding_times.append((perf_counter() - began) * 1000)
            for timer in timers:
                timer.stop()
            app.processEvents()
            event_times.append((perf_counter() - began) * 1000)
            peak_active = max(peak_active, len(cache))
        return {
            "page": name,
            "window": list(size),
            "viewport": [area.viewport().width(), area.viewport().height()],
            "records": records,
            "scroll_steps": len(event_times),
            "new_widgets_after_warmup": constructed - before,
            "peak_active_cards": peak_active,
            "update": _summary(binding_times),
            "update_and_events": _summary(event_times),
        }
    finally:
        card_type.__init__ = original_init
        page.close()
        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def main():
    """解析并校验合成记录和滚动参数，在临时配置下运行指定页面及屏幕尺寸的基准。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page", choices=("all", "overview", "gallery", "online"), default="all")
    parser.add_argument("--size", choices=("all", "2k", "4k"), default="all")
    parser.add_argument("--records", type=int, default=2000)
    parser.add_argument("--steps", type=int, default=80)
    args = parser.parse_args()
    if args.steps < 2 or args.records < 100:
        parser.error("use at least 2 steps and 100 records")
    app = QApplication([])
    anim.set_enabled(False)
    theme.set_theme("dark")
    theme.set_background_active(True)
    app.setStyleSheet(theme.render_qss("dark") + theme.background_overlay_qss("dark"))
    names = ("overview", "gallery", "online") if args.page == "all" else (args.page,)
    sizes = {"2k": (2560, 1440), "4k": (3840, 2160)}
    if args.size != "all":
        sizes = {args.size: sizes[args.size]}
    with tempfile.TemporaryDirectory(prefix="card-scroll-benchmark-") as temp:
        config_module.get_config_dir = lambda: Path(temp)
        config_module.Config._instance = None
        for size in sizes.values():
            for name in names:
                print(json.dumps(measure(app, name, size, args.records, args.steps)), flush=True)


if __name__ == "__main__":
    main()
