"""使用合成文件测量 CPU 图像缓存和 Qt 对象生命周期，不访问网络。

稳定采样轮次复用相同页面和图像；对象销毁单独记录，采样后再执行新图加载、
清空和缩容。Windows 隐藏窗口测试仍使用原生 Qt 平台，但不测量实际画面呈现。
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import gc
import hashlib
import json
import logging
import math
import os
import statistics
import sys
import tempfile
import threading
import time
import traceback
import weakref
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QSize, Qt
from PySide6.QtGui import QImageReader
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QStackedWidget, QVBoxLayout, QWidget

MIB = 1024**2
SIZES = {"2k": (2560, 1440), "4k": (3840, 2160)}
ROOT = Path(__file__).resolve().parents[2]


def flush_deletes():
    """多轮处理 Qt 延后删除和事件回调，使级联安排的对象释放完成。"""
    # Qt 信号代理处理 DeferredDelete 时可能继续安排删除，需多轮事件循环完成释放。
    for _ in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
        QTest.qWait(2)


def wait_for(predicate, description, timeout=30):
    """在处理 Qt 事件的同时等待条件成立，超时抛出包含等待对象说明的错误。"""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for {description}")
        QTest.qWait(5)


class Jsonl:
    """逐条写入带相对时间的测量记录，及时刷新以保留中断前的数据。"""

    def __init__(self, path):
        """创建输出目录及 UTF-8 记录文件，并保存测量计时起点。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("w", encoding="utf-8")
        self.started = time.perf_counter()

    def write(self, kind, **record):
        """添加记录类型和相对时间，写出并刷新一行 JSON 后返回实际记录。"""
        record = {"kind": kind, "elapsed_seconds": time.perf_counter() - self.started, **record}
        self.file.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.file.flush()
        return record


class ProcessCounters:
    """读取 Windows 原生进程计数器，结果不局限于 Python 分配的内存。"""

    def __init__(self):
        """准备可选的进程统计接口，在 Windows 上绑定原生内存、句柄及 GUI 资源计数器。"""
        try:
            import psutil

            self.process = psutil.Process()
        except ImportError:
            self.process = None
        if os.name != "nt":
            return
        from ctypes import wintypes

        class MemoryCounters(ctypes.Structure):
            """按 Windows 进程内存统计结构的字段顺序接收原生计数结果。"""

            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                    "PrivateUsage",
                )
            ]

        self.memory_type = MemoryCounters
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.handle = self.kernel.GetCurrentProcess()
        self.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        self.psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        self.kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.GetProcessHandleCount.restype = wintypes.BOOL
        self.user.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.user.GetGuiResources.restype = wintypes.DWORD

    def sample(self):
        """采集进程内存、线程和句柄资源，保留不可用字段及原生接口错误码。"""
        result = dict.fromkeys(("working_set_bytes", "private_usage_bytes", "handles", "gdi", "user"))
        result["python_threads"] = len(threading.enumerate())
        result["native_threads"] = self.process.num_threads() if self.process is not None else None
        errors = {}
        if os.name == "nt":
            memory = self.memory_type()
            memory.cb = ctypes.sizeof(memory)
            if self.psapi.GetProcessMemoryInfo(self.handle, ctypes.byref(memory), memory.cb):
                result.update(working_set_bytes=memory.WorkingSetSize, private_usage_bytes=memory.PrivateUsage)
            else:
                errors["memory"] = ctypes.get_last_error()
            count = ctypes.c_ulong()
            if self.kernel.GetProcessHandleCount(self.handle, ctypes.byref(count)):
                result["handles"] = count.value
            else:
                errors["handles"] = ctypes.get_last_error()
            for key, flag in (("gdi", 0), ("user", 1)):
                ctypes.set_last_error(0)
                value = self.user.GetGuiResources(self.handle, flag)
                error = ctypes.get_last_error()
                if value or not error:
                    result[key] = value
                else:
                    errors[key] = error
        elif self.process is not None:
            result["working_set_bytes"] = self.process.memory_info().rss
        if errors:
            result["counter_errors"] = errors
        return result


class WorkCounters:
    """统计在途与完成的图片工作，并为对象销毁用例提供可控的解码阻塞点。"""

    def __init__(self):
        """初始化线程安全计数、解码屏障及原函数恢复记录。"""
        self.lock = threading.Lock()
        self.active = Counter()
        self.finished = Counter()
        self.local_entered = threading.Event()
        self.local_release = threading.Event()
        self.local_release.set()
        self.originals = []

    def install(self, module, attribute, name):
        """包装指定图像处理入口，登记真实工作数量并支持暂态图片的受控阻塞。"""
        original = getattr(module, attribute)
        self.originals.append((module, attribute, original))

        def measured(path, *args, **kwargs):
            """增加在途计数并处理可选解码屏障，退出时将工作从活动数转入完成数。"""
            with self.lock:
                self.active[name] += 1
            try:
                if Path(path).name == "transient.png":
                    self.local_entered.set()
                    if not self.local_release.wait(10):
                        raise RuntimeError("benchmark transient decode barrier timed out")
                return original(path, *args, **kwargs)
            finally:
                with self.lock:
                    self.active[name] -= 1
                    self.finished[name] += 1

        setattr(module, attribute, measured)

    def snapshot(self):
        """在锁内复制活动及完成计数，避免采样线程读取不一致状态。"""
        with self.lock:
            return {"active": dict(self.active), "finished": dict(self.finished)}

    def restore(self):
        """释放解码屏障并恢复所有被测量包装替换的原始函数。"""
        self.local_release.set()
        for module, attribute, original in self.originals:
            setattr(module, attribute, original)


class FileCache:
    """以本地合成文件实现异步图片获取，可暂停指定请求以验证取消和销毁边界。"""

    def __init__(self, paths):
        """保存 URL 路径映射，初始化异步获取屏障和线程安全调用计数。"""
        self.paths = paths
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.lock = threading.Lock()
        self.fetches = 0

    async def fetch(self, url):
        """统计获取并等待受控请求放行，返回本地路径；未知 URL 直接失败而不访问网络。"""
        with self.lock:
            self.fetches += 1
        if "/gated/" in url:
            self.entered.set()
            while not self.release.is_set():
                await asyncio.sleep(0.002)
        await asyncio.sleep(0.002)
        # 未知 URL 直接在本地返回失败，避免基准测试访问网络。
        return self.paths[url]


class RetiredObjects:
    """通过弱引用跟踪预期已释放的 Python 包装、Qt 对象和工作线程。"""

    def __init__(self):
        """初始化待回收对象及线程的弱引用记录。"""
        self.refs = []
        self.threads = []

    def track(self, kind, obj):
        """按对象类别登记弱引用，使统计本身不延长对象生命周期。"""
        self.refs.append((kind, weakref.ref(obj)))

    def thread(self, obj):
        """登记待退出线程的弱引用，用于检查清理后的运行状态。"""
        self.threads.append(weakref.ref(obj))

    def snapshot(self):
        """分别统计仍存活的 Python 包装、原生 Qt 对象和运行中的已退役线程。"""
        wrappers, native = Counter(), Counter()
        for kind, reference in self.refs:
            obj = reference()
            if obj is not None:
                wrappers[kind] += 1
                if shiboken6.isValid(obj):
                    native[kind] += 1
        running = 0
        for reference in self.threads:
            thread = reference()
            if thread is not None:
                if isinstance(thread, threading.Thread):
                    running += thread.is_alive()
                elif shiboken6.isValid(thread):
                    running += thread.isRunning()
        return {
            "tracked": len(self.refs),
            "wrappers_alive": dict(wrappers),
            "native_alive": dict(native),
            "retired_threads_running": running,
        }


def make_corpus(directory):
    """生成包含多格式、透明度、方向元数据及无效图片的固定样本，返回本地与在线模型和校验清单。"""
    from PIL import Image, ImageDraw

    from src.models.mod import Mod
    from src.services.gamebanana.models import GbAuthor, GbModel, GbModRecord

    directory.mkdir()
    qt_formats = {bytes(value).decode("ascii").lower() for value in QImageReader.supportedImageFormats()}
    specs = [
        ("thumb.png", (640, 480), "RGB"),
        ("thumb.jpg", (640, 480), "RGB"),
        ("alpha-2k.png", (2560, 1440), "RGBA"),
        ("exif-4k.jpg", (3840, 2160), "RGB"),
        ("pressure.jpg", (1024, 768), "RGB"),
    ]
    if "webp" in qt_formats:
        specs.append(("thumb.webp", (640, 480), "RGB"))
    templates, manifest = {}, []
    for index, (name, dimensions, mode) in enumerate(specs):
        color = (49 + index * 20, 110, 180, 150) if mode == "RGBA" else (49 + index * 20, 110, 180)
        image = Image.new(mode, dimensions, color)
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, dimensions[0] // 4, dimensions[1] // 4), fill="white")
        options = {"quality": 88} if name.endswith(("jpg", "webp")) else {}
        if name.startswith("exif"):
            exif = Image.Exif()
            exif[274] = 6
            options["exif"] = exif
        path = directory / name
        image.save(path, **options)
        payload = path.read_bytes()
        templates[name] = payload
        manifest.append(
            {
                "file": name,
                "dimensions": list(dimensions),
                "mode": mode,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "exif_orientation": 6 if options.get("exif") else 1,
            }
        )
        image.close()
    small = [name for name in templates if name.startswith("thumb")]
    paths, mods, records = {}, [], []
    digest = hashlib.sha256()
    for index in range(2000):
        name = "exif-4k.jpg" if index % 53 == 0 else "alpha-2k.png" if index % 29 == 0 else small[index % len(small)]
        path = directory / f"fixed-{index:05}{Path(name).suffix}"
        path.write_bytes(templates[name])
        url = f"https://image-lifetime.invalid/fixed/{index}"
        paths[url] = path
        digest.update(f"{index}:{name}\n".encode())
        mods.append(Mod(id=f"mod-{index:05}", name=f"Synthetic {index:05}", preview_images=[str(path)]))
        records.append(
            GbModRecord(
                mod_id=index,
                model=GbModel.MOD,
                name=f"Synthetic {index:05}",
                submitter=GbAuthor(1, "Synthetic", "", ""),
                thumbnail_url=url,
                page_url="",
                view_count=0,
                like_count=0,
                download_count=0,
                comment_count=0,
                is_nsfw=False,
                date_updated_ts=0,
            )
        )
    (directory / "transient.png").write_bytes(templates["thumb.png"])
    (directory / "invalid.png").write_bytes(b"not an image")
    return (
        mods,
        records,
        paths,
        templates,
        {
            "records": len(mods),
            "templates": manifest,
            "assignment_sha256": digest.hexdigest(),
            "qt_image_formats": sorted(qt_formats),
        },
    )


class Benchmark:
    """协调图像缓存稳态、压力和对象销毁场景，并在隔离目录中采集资源统计。"""

    def __init__(self, args, directory, output):
        """建立共用预算、测量钩子、合成图片及三类页面，连接真实图片加载链并显示测试宿主。"""
        from src.services.gamebanana import image_loader
        from src.ui.pages import gallery, overview
        from src.ui.widgets.gb_adaptive_grid import GbAdaptiveGrid
        from src.ui.widgets.gb_lightbox import GbLightBox
        from src.ui.widgets.gb_mod_card import GbModCard
        from src.ui.widgets.gb_mod_detail_view import GbModDetailView
        from src.utils.image_cache_budget import image_cache_budget

        self.args, self.directory, self.output = args, directory, output
        self.manager = image_cache_budget
        self.manager.set_budget(args.cache_mb * MIB)
        self.work = WorkCounters()
        self.work.install(overview, "load_scaled_qimage_file", "overview_decode")
        self.work.install(gallery, "load_scaled_qimage_file", "gallery_decode")
        self.work.install(image_loader, "_transform_pixmap", "online_transform")
        self.retired = RetiredObjects()
        self.process = ProcessCounters()
        self.mods, self.records, paths, self.templates, self.corpus = make_corpus(directory / "images")
        self.cache = FileCache(paths)
        self.host = QWidget()
        self.host.setWindowTitle("Synthetic image lifetime benchmark")
        self.host.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, args.hidden)
        self.host.resize(*SIZES[args.size])
        self.stack = QStackedWidget(self.host)
        layout = QVBoxLayout(self.host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)
        self.loader = image_loader.ImageLoader(self.cache, self.host)
        self.pages = {"overview": overview.OverviewPage(), "gallery": gallery.GalleryPage(), "online": GbAdaptiveGrid()}
        for page in self.pages.values():
            self.stack.addWidget(page)
        self.detail = GbModDetailView(image_loader=self.loader, parent=self.host)
        self.detail.hide()
        self.lightbox = GbLightBox(image_loader=self.loader, parent=self.host)
        self.lightbox.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, args.hidden)
        self.lightbox.hide()
        self.pages["overview"].set_current_mods(self.mods, self.mods)
        self.pages["gallery"].set_mods(self.mods)
        for name in ("overview", "gallery"):
            self.pages[name]._fresh_population = False
            self.pages[name]._vs_population_timer.stop()
        loader = self.loader
        thumbnail_size = QSize(GbModCard.CARD_WIDTH, GbModCard.THUMBNAIL_HEIGHT)
        self.pages["online"].set_items(
            self.records,
            has_more=False,
            card_factory=lambda record: GbModCard(record, image_loader=loader, defer_image=True),
            card_binder=lambda card, record: card.set_record(record, defer_image=True),
            image_prefetcher=lambda record: loader.prefetch(
                record.thumbnail_url, size=thumbnail_size, crop_to_size=True
            ),
            image_prefetch_canceller=lambda record: loader.cancel_prefetch(
                record.thumbnail_url, size=thumbnail_size, crop_to_size=True
            ),
        )
        self.host.show()

    def loader_state(self):
        """在加载器请求锁内快照工作、排队、活动及合并请求数量。"""
        with self.loader._request_lock:
            return {name: len(getattr(self.loader, "_" + name)) for name in ("works", "queued", "active", "inflight")}

    @staticmethod
    def ready(card, online=False):
        """按本地或在线卡片结构检查像素是否可用且加载占位已经隐藏。"""
        label = card.thumbnail if online else getattr(card, "_preview_label", None)
        if label is None:
            label = card._image
        pixmap = label.pixmap()
        shimmer = getattr(label, "_shimmer", None)
        return pixmap is not None and not pixmap.isNull() and (shimmer is None or shimmer.isHidden())

    def state(self):
        """汇总页面可见卡片、空闲池、图片与布局队列，以及加载器和工作计数。"""
        pages = {}
        for name, page in self.pages.items():
            online = name == "online"
            cards = page._cards if online else page._vs_card_cache
            pool = page._card_pool if online else page._vs_card_pool
            pending = (
                {
                    "prefetch": len(page._prefetch_queue),
                    "image_timer": int(page._img_timer.isActive()),
                    "layout_timer": int(page._vs_timer.isActive()),
                    "reflow_timer": int(page._reflow_timer.isActive()),
                }
                if online
                else {
                    "requests": len(page._vs_image_requests),
                    "prefetch": len(page._vs_pending_prefetch),
                    "images": len(
                        getattr(page, "_vs_pending_pixmap_cards", getattr(page, "_vs_pending_pixmap_items", []))
                    ),
                    "worker_queue": page._pixmap_worker._queue.qsize(),
                    "image_timer": int(page._vs_pixmap_timer.isActive()),
                    "layout_timer": int(page._vs_scroll_timer.isActive()),
                    "population_timer": int(page._vs_population_timer.isActive()),
                }
            )
            viewport = page.scroll_area.viewport() if online else page._scroll_area.viewport()
            pages[name] = {
                "visible": page.isVisible(),
                "active_cards": len(cards),
                "ready_cards": sum(self.ready(c, online) for c in cards.values()),
                "idle_cards": {kind.__name__: len(items) for kind, items in pool._idle.items()},
                "viewport": [viewport.width(), viewport.height()],
                "queues": pending,
            }
        return {"pages": pages, "loader": self.loader_state(), "work": self.work.snapshot()}

    def quiet(self):
        """检查图片与布局工作均已排空，且每个可见页面的活动卡片都已就绪。"""
        state = self.state()
        return (
            not any(state["loader"].values())
            and not any(state["work"]["active"].values())
            and all(
                not any(page["queues"].values())
                and (not page["visible"] or page["active_cards"] == page["ready_cards"])
                for page in state["pages"].values()
            )
        )

    def settle(self):
        """等待所有页面和加载器达到静止状态并处理删除回调，超时先保存待办状态再抛错。"""
        try:
            wait_for(self.quiet, "all page/loader/decoder queues", timeout=45)
            flush_deletes()
        except RuntimeError:
            self.output.write("unsettled", state=self.state())
            raise

    def budget(self):
        """获取图片预算快照，任一分区或总占用超过额度时立即报告违规。"""
        snapshot = asdict(self.manager.snapshot())
        if snapshot["bytes_used"] > snapshot["limit_bytes"]:
            raise RuntimeError(f"Global image budget violated: {snapshot}")
        for partition in snapshot["partitions"]:
            if partition["bytes_used"] > partition["limit_bytes"]:
                raise RuntimeError(f"Image partition budget violated: {partition}")
        return snapshot

    def sample(self, phase, round_number=None, **details):
        """在连续静止窗口中采集进程计数的中位数，同时记录预算、页面状态和退役对象。"""
        self.settle()
        samples = []
        for _ in range(5):
            QTest.qWait(50)
            if not self.quiet():
                raise RuntimeError("Queues became active during quiescent sampling")
            samples.append(self.process.sample())
        counters = {
            key: statistics.median([sample[key] for sample in samples])
            for key in samples[0]
            if key != "counter_errors" and all(sample.get(key) is not None for sample in samples)
        }
        if self.args.trace_python:
            import tracemalloc

            current, peak = tracemalloc.get_traced_memory()
            counters.update(python_traced_bytes=current, python_traced_peak_bytes=peak)
        return self.output.write(
            "sample",
            phase=phase,
            round=round_number,
            counters=counters,
            counter_samples=samples,
            budget=self.budget(),
            state=self.state(),
            retired=self.retired.snapshot(),
            **details,
        )

    def scroll_round(self, number):
        """让各页面跨屏往返并经历尺寸变化，执行预览取消场景后记录一轮稳态采样。"""
        returned = {}
        for name, page in self.pages.items():
            self.stack.setCurrentWidget(page)
            QTest.qWait(5)
            online = name == "online"
            if not online:
                page._fresh_population = False
                page._vs_population_timer.stop()
            scroll_area = page.scroll_area if online else page._scroll_area
            row = page._row_h if online else page._vs_row_h
            rows = max(1, math.ceil(scroll_area.viewport().height() / row))
            bar = scroll_area.verticalScrollBar()
            origin = min(10 * row, bar.maximum())
            for position in (origin, origin + rows * row, origin + 2 * rows * row, origin):
                bar.setValue(position)
                (page._update_visible if online else page._vs_update_visible)()
                if position == origin:
                    cards = page._cards if online else page._vs_card_cache
                    returned[name] = {
                        "active": len(cards),
                        "ready_immediately": sum(self.ready(c, online) for c in cards.values()),
                    }
                self.settle()
            self.host.resize(*(int(value * 0.78) for value in SIZES[self.args.size]))
            QTest.qWait(10)
            self.settle()
            self.host.resize(*SIZES[self.args.size])
            QTest.qWait(10)
            self.settle()
        self.preview_and_cancel()
        return self.sample("steady", number, warmup=number <= self.args.warmup, return_readiness=returned)

    def preview_and_cancel(self):
        """打开详情与大图预览，再验证同一图片目标取消后重新加载能够交付当前内容。"""
        from src.services.gamebanana.models import GbModProfile

        profile = GbModProfile(**asdict(self.records[1]))
        # asdict 会递归转换作者对象，此处保留实际模型实例。
        profile.submitter = self.records[1].submitter
        profile.preview_images = [self.records[index].thumbnail_url for index in (1, 29, 53)]
        self.detail.load_profile(profile)
        self.detail.setGeometry(self.host.width() - 520, 0, 520, self.host.height())
        self.detail.show()
        self.detail.raise_()
        self.settle()
        self.detail.hide()
        self.lightbox.show_for_url(self.records[53].thumbnail_url)
        self.settle()
        self.lightbox.close()
        label = QLabel(self.host)
        self.retired.track("canceled_target", label)
        self.loader.load(self.records[7].thumbnail_url, label, size=QSize(96, 96))
        self.loader.cancel_for(label)
        self.loader.load(self.records[8].thumbnail_url, label, size=QSize(96, 96))
        self.settle()
        if label.pixmap() is None or label.pixmap().isNull():
            raise RuntimeError("Canceled/reloaded target never received its current image")
        label.deleteLater()

    def transient_local(self, name):
        """在本地图像解码被阻塞时销毁临时页面，跟踪其卡片及线程并保证释放屏障。"""
        from src.models.mod import Mod
        from src.ui.pages.gallery import GalleryPage
        from src.ui.pages.overview import OverviewPage

        page = (OverviewPage if name == "overview" else GalleryPage)(self.host)
        page.resize(640, 480)
        mod = Mod(id="transient", name="Transient", preview_images=[str(self.directory / "images/transient.png")])
        self.work.local_entered.clear()
        self.work.local_release.clear()
        try:
            if name == "overview":
                page.set_current_mods([mod], [mod])
            else:
                page.set_mods([mod])
            page.show()
            page._fresh_population = False
            page._vs_population_timer.stop()
            page._vs_update_visible()
            page._vs_load_deferred_pixmaps()
            wait_for(self.work.local_entered.is_set, f"{name} transient decode entry", timeout=10)
            self.retired.track(name + "_page", page)
            for card in page._vs_card_cache.values():
                self.retired.track(name + "_card", card)
            for attribute in ("_pixmap_worker", "_panel_preview_worker", "_key_bindings_worker"):
                worker = getattr(page, attribute, None)
                if worker is not None:
                    self.retired.track("local_worker", worker)
                    self.retired.thread(worker)
            page.deleteLater()
            flush_deletes()
            if shiboken6.isValid(page):
                raise RuntimeError(f"{name} page did not process DeferredDelete")
        finally:
            self.work.local_release.set()
            if shiboken6.isValid(page):
                page.close()
                page.deleteLater()

    def transient_online(self):
        """在异步获取被阻塞时销毁加载器所属控件，跟踪对象和线程并保证请求最终放行。"""
        from src.services.gamebanana.image_loader import ImageLoader

        owner = QWidget(self.host)
        loader = ImageLoader(self.cache, owner)
        label = QLabel(owner)
        url = "https://image-lifetime.invalid/gated/transient"
        self.cache.paths[url] = self.directory / "images/thumb.png"
        self.cache.entered.clear()
        self.cache.release.clear()
        self.retired.track("online_owner", owner)
        self.retired.track("online_loader", loader)
        self.retired.track("online_target", label)
        self.retired.thread(loader._loop_thread)
        try:
            loader.load(url, label, size=QSize(96, 96))
            wait_for(self.cache.entered.is_set, "transient online fetch entry", timeout=10)
            owner.deleteLater()
            flush_deletes()
        finally:
            self.cache.release.set()
            if shiboken6.isValid(owner):
                owner.deleteLater()

    def lifecycle_round(self, number):
        """运行本地及在线对象销毁场景，等待原生对象和线程退出后采集生命周期数据。"""
        self.transient_local("overview")
        self.transient_local("gallery")
        self.transient_online()

        def stopped():
            """处理级联删除后检查退役的原生对象和工作线程是否全部退出。"""
            flush_deletes()
            state = self.retired.snapshot()
            return not state["native_alive"] and not state["retired_threads_running"]

        wait_for(stopped, "retired native objects and threads", timeout=15)
        return self.sample("lifecycle", number)

    def pressure(self):
        """持续加载新图施加缓存压力，并验证在途任务经历缩容和清空后不会回填已清理缓存。"""
        target_bytes = max(16 * MIB, self.args.cache_mb * MIB * 2 + 1)
        output_size = QSize(1024, 768)
        labels = [QLabel(self.host) for _ in range(8)]
        delivered = count = 0
        try:
            while delivered < target_bytes:
                for label in labels:
                    path = self.directory / "images" / f"pressure-{count:06}.jpg"
                    path.write_bytes(self.templates["pressure.jpg"])
                    url = f"https://image-lifetime.invalid/pressure/{count}"
                    self.cache.paths[url] = path
                    self.loader.load(url, label, size=output_size)
                    count += 1
                self.settle()
                for label in labels:
                    pixmap = label.pixmap()
                    if pixmap is None or pixmap.isNull():
                        raise RuntimeError("Fresh pressure image did not load")
                    delivered += pixmap.toImage().sizeInBytes()
                    label.clear()
                self.budget()
            self.sample(
                "pressure_stream", unique_images=count, delivered_pixel_bytes=delivered, target_pixel_bytes=target_bytes
            )
            self.cache.entered.clear()
            self.cache.release.clear()
            for index, label in enumerate(labels):
                url = f"https://image-lifetime.invalid/gated/clear/{index}"
                self.cache.paths[url] = self.directory / "images/pressure.jpg"
                self.loader.load(url, label, size=QSize(1000, 750))
            wait_for(self.cache.entered.is_set, "clear/shrink pending fetch", timeout=10)
            pending = self.loader_state()
            if not pending["active"]:
                raise RuntimeError("Clear/shrink phase had no active work")
            self.manager.set_budget(self.args.cache_mb * MIB // 4)
            shrink = self.budget()
            self.manager.clear()
            self.cache.release.set()
            self.settle()
            if self.manager.snapshot().bytes_used != 0:
                raise RuntimeError("Work submitted before clear repopulated the image caches")
            self.sample("pending_clear_shrink", pending_before=pending, immediately_after_shrink=shrink)
            self.manager.set_budget(self.args.cache_mb * MIB)
            invalid = "https://image-lifetime.invalid/invalid"
            self.cache.paths[invalid] = self.directory / "images/invalid.png"
            labels[0].clear()
            self.loader.load(invalid, labels[0], size=QSize(64, 64))
            self.settle()
            invalid_pixmap = labels[0].pixmap()
            if invalid_pixmap is not None and not invalid_pixmap.isNull():
                raise RuntimeError("Invalid input unexpectedly produced pixels")
        finally:
            self.cache.release.set()
            self.manager.set_budget(self.args.cache_mb * MIB)
            for label in labels:
                self.retired.track("pressure_target", label)
                label.deleteLater()

    def close(self):
        """解除屏障、关闭页面及加载器，等待后台工作结束后释放宿主并恢复测量钩子。"""
        from src.ui.worker_lifecycle import finish_managed_workers

        self.cache.release.set()
        self.work.local_release.set()
        self.pages["online"].clear()
        for name in ("overview", "gallery"):
            self.pages[name].close()
        self.loader.shutdown()
        self.loader._executor.shutdown(wait=True, cancel_futures=True)
        for kind, obj in [
            ("host", self.host),
            ("main_loader", self.loader),
            ("detail", self.detail),
            ("lightbox", self.lightbox),
            *[(name + "_page", page) for name, page in self.pages.items()],
        ]:
            self.retired.track(kind, obj)
        self.retired.thread(self.loader._loop_thread)
        self.host.close()
        self.host.deleteLater()
        self.pages.clear()
        self.host = self.stack = self.detail = self.lightbox = self.loader = None
        finish_managed_workers()
        flush_deletes()
        self.work.restore()


def summarize(samples, warmup):
    """排除预热轮次，按稳态和销毁阶段汇总各项资源计数及每轮变化斜率。"""
    tails = [sample for sample in samples if sample["round"] > warmup]
    result = {}
    for phase in ("steady", "lifecycle"):
        rows = [sample for sample in tails if sample["phase"] == phase]
        metrics = {}
        for key in sorted({key for row in rows for key in row["counters"]}):
            values = [row["counters"][key] for row in rows if key in row["counters"]]
            if not values:
                continue
            center = (len(values) - 1) / 2
            divisor = sum((index - center) ** 2 for index in range(len(values)))
            slope = sum((index - center) * value for index, value in enumerate(values)) / divisor if divisor else None
            metrics[key] = {
                "samples": len(values),
                "min": min(values),
                "max": max(values),
                "first": values[0],
                "last": values[-1],
                "slope_per_round": slope,
            }
        result[phase] = metrics
    return result


def main():
    """配置隔离的 Qt 测量环境，执行稳态、销毁及压力场景，输出记录并在失败时保留诊断信息。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--size", choices=SIZES, default="2k")
    parser.add_argument("--cache-mb", type=int, default=768)
    parser.add_argument("--platform", choices=("offscreen", "windows"), default="offscreen")
    parser.add_argument("--hidden", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trace-python", action="store_true")
    args = parser.parse_args()
    if args.rounds < 1 or not 0 <= args.warmup < args.rounds or args.cache_mb < 0:
        parser.error("rounds must be positive, 0 <= warmup < rounds, and cache-mb nonnegative")
    if args.platform == "windows" and os.name != "nt":
        parser.error("the windows platform requires Windows")
    os.environ["QT_QPA_PLATFORM"] = args.platform
    sys.path.insert(0, str(ROOT))
    if args.trace_python:
        import tracemalloc

        tracemalloc.start(10)
    output = Jsonl(args.output.resolve())
    benchmark = None
    samples = []
    try:
        with tempfile.TemporaryDirectory(prefix="image-memory-lifecycle-") as temporary:
            from src.utils import paths

            directory = Path(temporary)
            original_app_dir = paths.get_app_dir
            paths.get_app_dir = lambda: directory
            try:
                # 先重定向路径，再导入读取配置和缓存的模块。
                from src.core.config import Config
                from src.ui import anim, theme
                from src.utils.logger import logger

                logger.setLevel(logging.WARNING)
                Config._instance = None
                app = QApplication([])
                app.setQuitOnLastWindowClosed(False)
                anim.set_enabled(False)
                theme.set_theme("dark")
                theme.set_background_active(True)
                app.setStyleSheet(theme.render_qss("dark") + theme.background_overlay_qss("dark"))
                benchmark = Benchmark(args, directory, output)
                output.write(
                    "metadata",
                    options={**vars(args), "output": str(args.output.resolve())},
                    backend="CPU / QImage worker; GUI QPixmap",
                    platform=app.platformName(),
                    device_pixel_ratio=benchmark.host.devicePixelRatioF(),
                    primary_screen=[app.primaryScreen().size().width(), app.primaryScreen().size().height()],
                    animation_mode="disabled; run separate native animation regression",
                    corpus=benchmark.corpus,
                    network="disabled; synthetic FileCache only",
                    gpu_memory_coverage=False,
                    strong_gc_during_rounds=False,
                    counters=benchmark.process.sample(),
                )
                for number in range(1, args.rounds + 1):
                    output.write("round_start", round=number)
                    samples.append(benchmark.scroll_round(number))
                    samples.append(benchmark.lifecycle_round(number))
                benchmark.pressure()
            finally:
                if benchmark is not None:
                    benchmark.close()
                paths.get_app_dir = original_app_dir
            flush_deletes()
            before_gc = benchmark.retired.snapshot()
            # 采样和最终销毁完成后仅执行一次垃圾回收，用于诊断循环引用，避免影响每轮内存曲线。
            collected = gc.collect()
            flush_deletes()
            after_gc = benchmark.retired.snapshot()
            output.write(
                "final_lifetime",
                before_diagnostic_gc=before_gc,
                collected=collected,
                after_diagnostic_gc=after_gc,
                counters=benchmark.process.sample(),
            )
            if after_gc["wrappers_alive"] or after_gc["native_alive"] or after_gc["retired_threads_running"]:
                raise RuntimeError(f"Expected-dead objects/threads survived teardown: {after_gc}")
            summary = output.write(
                "summary",
                status="passed",
                rounds=args.rounds,
                warmup=args.warmup,
                warm_tail=summarize(samples, args.warmup),
                final_retired=after_gc,
                interpretation="Observed resource trends; not a zero-leak or GPU-memory claim",
            )
            if args.trace_python:
                output.write(
                    "python_allocations",
                    top=[str(item) for item in tracemalloc.take_snapshot().statistics("lineno")[:20]],
                )
            print(
                json.dumps(
                    {
                        "status": summary["status"],
                        "output": str(args.output.resolve()),
                        "rounds": args.rounds,
                        "elapsed_seconds": summary["elapsed_seconds"],
                    }
                ),
                flush=True,
            )
        return 0
    except Exception as exc:
        output.write("failure", error=str(exc), exception=type(exc).__name__)
        traceback.print_exc()
        return 1
    finally:
        output.file.close()


if __name__ == "__main__":
    raise SystemExit(main())
