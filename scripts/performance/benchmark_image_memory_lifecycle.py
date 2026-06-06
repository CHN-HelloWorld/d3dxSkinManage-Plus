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
