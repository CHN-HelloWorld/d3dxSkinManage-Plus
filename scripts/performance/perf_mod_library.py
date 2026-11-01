"""通过应用入口执行隔离、可重复的 Mod 库基准测试。

使用项目 Python 环境运行，无需 pytest。界面模式启动真实 MainWindow，
不接受 offscreen/minimal 平台。每种库规模默认采样三轮，每轮 60 秒。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import logging
import os
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from weakref import WeakSet

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
GAME = "perf"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
PREVIEW = PNG + bytes(120 * 1024 - len(PNG))
_GUI_APP = None


def emit(event, **fields):
    """输出一条包含事件类型的 JSON 记录并立即刷新，供外部采集运行进度。"""
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def owned_remove(path: Path, anchor: Path):
    """仅删除路径解析后仍位于当前测试根目录内的子项。"""
    target, root = path.resolve(), anchor.resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError(f"refusing cleanup outside/at anchor: {target}")
    if target.exists():
        shutil.rmtree(target)


@contextmanager
def isolated_manager(root: Path):
    """创建服务前重定向全部可写应用目录。

    退出时恢复已有单例和路径别名，包括上下文期间新导入的别名。
    只读资源仍从项目目录加载。
    """
    from src.utils import paths

    root = root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    originals = {
        name: getattr(paths, name)
        for name in ("get_app_dir", "get_data_dir", "get_config_dir", "get_cache_dir", "get_qss_icon_cache_dir")
    }

    def directory(name):
        """在当前隔离根下创建并返回所需数据目录，空名称对应隔离根本身。"""
        result = root / name if name else root
        result.mkdir(parents=True, exist_ok=True)
        return result

    replacements = {
        name: (lambda sub=sub: directory(sub))
        for name, sub in (
            ("get_app_dir", ""),
            ("get_data_dir", "data"),
            ("get_config_dir", "config"),
            ("get_cache_dir", "cache"),
            ("get_qss_icon_cache_dir", "cache/qss_icons"),
        )
    }
    # 应用根路径已有缓存，不会重复访问文件系统。
    replacements["get_app_dir"] = lambda: root
    saved = []
    manager = None
    with ExitStack() as stack:
        stack.enter_context(patch.object(tempfile, "tempdir", str(directory("tmp"))))
        for module in list(sys.modules.values()):
            if module is None or not getattr(module, "__name__", "").startswith("src."):
                continue
            for name, replacement in replacements.items():
                if getattr(module, name, None) is originals[name]:
                    stack.enter_context(patch.object(module, name, replacement))
        for module_name, class_name in (
            ("config", "Config"),
            ("game_config_store", "GameConfigStore"),
            ("game_service", "GameService"),
            ("mod_manager", "ModManager"),
        ):
            cls = getattr(importlib.import_module(f"src.core.{module_name}"), class_name)
            saved.append((cls, cls._instance))
            cls._instance = None
        from src.core.config import Config
        from src.core.game_config_store import GameConfigStore
        from src.core.mod_manager import ModManager
        from src.utils.logger import logger

        stack.enter_context(patch.object(logger, "level", logging.WARNING))
        try:
            config = Config()
            config.set("gamebanana.enabled", False)
            config.set("gamebanana.update_check.enabled", False)
            config.set("app_update.on_startup", False)
            config.set("mods.delete_method", "permanent")
            deploy = directory("Mods")
            store = GameConfigStore()
            store.set_games(
                [
                    {
                        "id": GAME,
                        "name": "Benchmark",
                        "name_en": "Benchmark",
                        "mods_path": str(deploy),
                        "resource_dir": GAME,
                    }
                ]
            )
            store.set_last_game_id(GAME)
            manager = ModManager()
            manager.set_game(GAME)
            manager.set_deploy_dir(str(deploy))
            if not manager._index._db_path.resolve().is_relative_to(root):
                raise RuntimeError("benchmark database escaped isolation")
            yield manager
        finally:
            if manager is not None:
                manager.stop_ini_sync()
                manager._index.close()
            for cls, instance in reversed(saved):
                cls._instance = instance
            # 此处恢复的模块在建立 ExitStack 时尚未导入。
            for module in list(sys.modules.values()):
                if module is None or not getattr(module, "__name__", "").startswith("src."):
                    continue
                for name, replacement in replacements.items():
                    if getattr(module, name, None) is replacement:
                        setattr(module, name, originals[name])


class Meter:
    """只在目标操作期间启用计数器，排除准备和校验步骤。

    文件系统计数记录实际 stat 请求，包含存在性和类型检查；不清空操作系统缓存。
    线程耗时存在重叠，其总和不能作为实际经过时间。
    """

    def __init__(self, manager, root):
        """保存待测管理器和路径范围，初始化操作计数、耗时样本及可恢复的补丁上下文。"""
        self.manager, self.root = manager, root
        self.counts = Counter()
        self.times = defaultdict(list)
        self.stack = ExitStack()
        self.prefix = os.path.normcase(os.path.abspath(root)) + os.sep
        self.cache = os.path.normcase(os.path.abspath(root / "data" / GAME / "preview_cache"))

    def _key(self, value):
        """将可识别的路径规范化为不带 Windows 长路径前缀的绝对比较键。"""
        if not isinstance(value, (str, bytes, os.PathLike)):
            return ""
        raw = os.fsdecode(value)
        if raw.startswith("\\\\?\\"):
            raw = raw[4:]
        return os.path.normcase(os.path.abspath(raw))

    def wrap(self, obj, name, label=None):
        """在当前测量上下文中包装指定方法，记录耗时并在退出时恢复原入口。"""
        original = getattr(obj, name)
        key = label or name

        def measured(*args, **kwargs):
            """完整转发被测方法调用，在成功或异常返回后记录其耗时。"""
            start = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                self.times[key].append(time.perf_counter() - start)

        self.stack.enter_context(patch.object(obj, name, measured))

    def __enter__(self):
        """启用 Mod 实体化、SQL、文件状态及预览缓存目录枚举计数，只统计当前隔离范围。"""
        index = self.manager._index
        row_to_mod = index._row_to_mod

        def materialize(row, cache=None):
            """累计完整 Mod 对象的创建次数，再调用原始数据库行转换方法。"""
            self.counts["full_mod_objects"] += 1
            return row_to_mod(row, cache)

        self.stack.enter_context(patch.object(index, "_row_to_mod", materialize))

        def trace(sql):
            """统计涉及 Mod 或部署数据的查询，并区分整行查询与字段投影查询。"""
            normalized = " ".join(sql.lower().split())
            if normalized.startswith("select") and any(name in normalized for name in ("mods", "deployments")):
                self.counts["sql_selects"] += 1
                self.counts["full_row_sql" if normalized.startswith("select *") else "lightweight_sql"] += 1

        index._db().set_trace_callback(trace)
        self.stack.callback(index._db().set_trace_callback, None)
        original_stat = os.stat

        def stat(path, *args, **kwargs):
            """仅为隔离根内的路径累计文件状态请求，再执行原系统调用。"""
            if self._key(path).startswith(self.prefix):
                self.counts["file_stat_calls"] += 1
            return original_stat(path, *args, **kwargs)

        self.stack.enter_context(patch.object(os, "stat", stat))
        for name in ("scandir", "listdir"):
            original = getattr(os, name)

            def listing(path=".", *args, _original=original, **kwargs):
                """只统计预览缓存根目录的枚举次数，并保留原枚举函数的参数及返回值。"""
                if self._key(path) == self.cache:
                    self.counts["cache_root_enumerations"] += 1
                return _original(path, *args, **kwargs)

            self.stack.enter_context(patch.object(os, name, listing))
        return self

    def __exit__(self, *exc):
        """退出测量范围时恢复方法补丁和数据库跟踪回调。"""
        return self.stack.__exit__(*exc)

    def result(self):
        """返回固定计数项及各方法的调用数、累计和最大耗时，未发生的计数按零报告。"""
        counters = {
            name: self.counts[name]
            for name in (
                "full_mod_objects",
                "sql_selects",
                "full_row_sql",
                "lightweight_sql",
                "file_stat_calls",
                "cache_root_enumerations",
            )
        }
        return {
            "counts": counters,
            "timings": {
                name: {"calls": len(values), "sum_elapsed_s": sum(values), "max_elapsed_s": max(values, default=0)}
                for name, values in self.times.items()
            },
        }


def write_archive(path: Path, ordinal: int):
    """生成含独立序号、预览和纹理的 COPY 模式 7z 样本，减少压缩算法对核心测量的影响。"""
    import py7zr

    with py7zr.SevenZipFile(path, "w", filters=[{"id": py7zr.FILTER_COPY}]) as archive:
        archive.writestr(f"[Constants]\nglobal $bench = {ordinal}\n".encode(), "Skin/mod.ini")
        archive.writestr(b"b" * 4096, "Skin/pos.buf")
        archive.writestr(PREVIEW, "Skin/preview.png")
        for number in range(5):
            archive.writestr(bytes([number]) * 256, f"Skin/tex{number:03d}.dds")


def build_library(manager, root, count):
    """创建合成分类及带身份标记和侧车元数据的容器库，返回构造耗时和归档大小统计。"""
    from src.core.mod_package import build_local_manifest, write_container_marker, write_sidecar
    from src.models.mod import Mod, ModInfo

    character = manager.add_character(name="Bench", node_type="character")
    library = manager._get_character_library(character.id)
    sizes = []
    start = time.perf_counter()
    for ordinal in range(count):
        container = library / f"M{ordinal:05d}"
        container.mkdir()
        body = container / "body.7z"
        write_archive(body, ordinal)
        if not write_container_marker(
            container, body.name, Mod.compute_sha1(body), imported_at=datetime.now().isoformat()
        ):
            raise RuntimeError("could not create sample identity")
        manifest = build_local_manifest(
            {
                "name": container.name,
                "info": Mod(
                    info=ModInfo(author="Synthetic", description="d" * 256, tags=["benchmark", f"group-{ordinal % 16}"])
                ).to_dict()["info"],
            },
            {"classification": ["Bench"]},
        )
        if not write_sidecar(body, manifest):
            raise RuntimeError("could not create sample sidecar")
        sizes.append(body.stat().st_size)
        if (ordinal + 1) % 1000 == 0 or ordinal + 1 == count:
            emit("build", count=count, done=ordinal + 1)
    return {
        "build_s": time.perf_counter() - start,
        "body_bytes_total": sum(sizes),
        "body_bytes_min": min(sizes),
        "body_bytes_max": max(sizes),
        "node_id": character.id,
    }


def check_library(manager, count, expected_identities=None, preset=None):
    """核验条目数量、持久身份、内容摘要、标签及预览，可同时检查重建前身份和预设引用。"""
    from PySide6.QtGui import QImage

    from src.core.mod_identity import read_source_mod_id, validate_mod_id

    mods = manager._index.get_all_mods(GAME)
    assert len(mods) == count, f"registered {len(mods)} != {count}"
    identities = {mod.mod_id for mod in mods}
    assert len(identities) == count
    assert len({mod.content_hash for mod in mods}) == count
    for mod in mods:
        validate_mod_id(mod.mod_id)
        assert read_source_mod_id(mod.path) == mod.mod_id
        assert Path(mod.path).is_file()
        assert len(mod.info.tags) == 2
        assert len(mod.preview_images) == 1
        assert not QImage(mod.preview_images[0]).isNull(), mod.preview_images
    if expected_identities is not None:
        assert identities == expected_identities
    if preset is not None:
        actual = manager.preset_manager.get_preset(preset[0])
        selected = {mid for ids in actual.character_mods.values() for mid in ids}
        assert {manager.get_mod(mid).mod_id for mid in selected} == preset[1]
    return {
        "registered": len(mods),
        "unique_persistent_ids": len(identities),
        "unique_content_hashes": count,
        "decoded_previews": count,
        "tags_verified": True,
        "preset_remapped": preset is not None,
    }, mods


def rebuild(manager, root, count, label, identities=None, preset=None):
    """测量实际索引重建及关键步骤，随后核验内容正确性并返回报告与重建后的 Mod。"""
    from src.core.mod_manager import ModManager

    with Meter(manager, root) as meter:
        for name in (
            "_hash_rebuild_descriptors",
            "_enrich_rebuild_item",
            "_find_previews",
        ):
            meter.wrap(ModManager, name)
        start = time.perf_counter()
        report = manager.rebuild_index_from_filesystem(GAME)
        elapsed = time.perf_counter() - start
    assert report.registered == count and not report.invalid_metadata, report
    correctness, mods = check_library(manager, count, identities, preset)
    result = {
        "label": label,
        "total_wall_s": elapsed,
        **meter.result(),
        "correctness": correctness,
        "report": asdict(report) if is_dataclass(report) else repr(report),
    }
    emit("rebuild", count=count, label=label, seconds=elapsed)
    return result, mods


def poll(manager, root, label, rounds=3):
    """重复测量无变化的资源库与部署目录对账，核验未产生修改或跳过结果。"""
    result = {"scenario": label, "source": [], "deployment": []}
    for kind in ("source", "deployment"):
        for _ in range(rounds):
            with Meter(manager, root) as meter:
                start = time.perf_counter()
                if kind == "source":
                    report = manager.reconcile_library(GAME, trigger="poll", plan_intakes=False)
                    assert not report.changed and not report.skipped, report
                else:
                    assert not manager.reconcile_missing_deployments(GAME)
                elapsed = time.perf_counter() - start
            result[kind].append({"total_wall_s": elapsed, **meter.result()})
    return result


def mixed_deployments(manager, mods, root):
    """将真实解压部署分布到四个类别目录。

    准备不计时；通过管理器的移动和启用入口维护资源身份、索引及部署状态。
    """
    characters = [manager.add_character(name=f"Group{i}", node_type="character") for i in range(1, 4)]
    character_ids = [mods[0].node_id] + [char.id for char in characters]
    enabled = 0
    for number, mod in enumerate(mods):
        if number % 4:
            assert manager.move_mod(mod.mod_id, character_ids[number % 4])
        if number % 10 == 0:
            updated = manager.toggle_mod(mod.mod_id)
            assert updated is not None and updated.is_enabled
            enabled += 1
    # 调用方已校验删除范围；移动仅在当前隔离库及其类别目录内进行。
    manager.stop_ini_sync()
    return {
        "characters": len(character_ids),
        "enabled_mods": enabled,
        "deployed_mods": len(manager._storage.scan_deployments(force=True)),
    }


def intake_case(manager, root, count):
    """准备独立归档样本，测量连续真实导入准备与提交，并在计时结束后验证完整入库结果。"""
    from src.core.mod_intake import ModIntakeService

    service = ModIntakeService(manager)
    char = manager.add_character(name="Intake", node_type="character")
    incoming = root / "incoming"
    incoming.mkdir()
    start = time.perf_counter()
    for number in range(count):
        write_archive(incoming / f"M{number:05d}.7z", number)
    built = time.perf_counter() - start
    template = service.plan_request(
        source_path=incoming / "M00000.7z", game_id=GAME, node_id=char.id, target_format="7z"
    )
    with Meter(manager, root) as meter:
        meter.wrap(service, "prepare")
        meter.wrap(service, "commit")
        meter.wrap(manager._index, "find_by_content_hash", "content_duplicate_query")
        meter.wrap(manager._index, "find_mod_id_by_path", "path_duplicate_guard")
        start = time.perf_counter()
        for number in range(count):
            request = replace(template, source_path=incoming / f"M{number:05d}.7z", display_name=f"M{number:05d}")
            prepared = service.prepare(request)
            # 两个描述标签属于测试样本，在事务提交前通过应用元数据写入器持久化。
            prepared.mod.info.tags = ["benchmark", f"group-{number % 16}"]
            assert manager._write_mod_sidecar(prepared.mod)
            service.commit(prepared)
            if (number + 1) % 500 == 0 or number + 1 == count:
                emit("intake", count=count, done=number + 1, seconds=time.perf_counter() - start)
        elapsed = time.perf_counter() - start
    correctness, _ = check_library(manager, count)
    sizes = [path.stat().st_size for path in incoming.iterdir()]
    return {
        "total_wall_s": elapsed,
        "sample_build_s": built,
        "body_bytes_total": sum(sizes),
        "body_bytes_min": min(sizes),
        "body_bytes_max": max(sizes),
        "correctness": correctness,
        **meter.result(),
        "pipeline": "planned request snapshot; sequential real prepare -> commit",
    }


def percentiles(values):
    """对非空心跳样本返回插值得到的中位数、第九十五百分位和最大值。"""
    ordered = sorted(values)

    def percentile(fraction):
        """按相邻有序样本线性插值计算指定分位数。"""
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    if not ordered:
        raise RuntimeError("no heartbeat samples")
    return {"p50": percentile(0.5), "p95": percentile(0.95), "max": max(ordered)}


def close_gui_window(window, app):
    """恢复路径或删除测试目录前完成延迟关闭。

    关闭耗时不纳入采样；若关闭超时则本轮无效，但仍保留测试文件直到所有者线程结束。
    """
    import shiboken6
    from PySide6.QtCore import QEvent, QEventLoop, QThread, QTimer

    workers = set(window.findChildren(QThread))
    # manage_page_worker 允许页面任务独立于页面存活，须等图像和元数据读写结束再清理。
    for page_name in ("_overview_page", "_gallery_page"):
        page = getattr(window, page_name, None)
        for name in ("_pixmap_worker", "_panel_preview_worker", "_key_bindings_worker"):
            worker = getattr(page, name, None)
            if worker is not None:
                workers.add(worker)

    def finished():
        """同时检查窗口隐藏、库探测提交结束和仍有效工作线程退出，判定夹具能否安全清理。"""
        probe = getattr(window, "_library_probe", None)
        return (
            not window.isVisible()
            and (probe is None or (probe.worker is None and not probe.applying))
            and not any(worker.isRunning() for worker in workers if shiboken6.isValid(worker))
        )

    window.close()
    loop = QEventLoop()
    poll = QTimer()
    poll.setInterval(10)
    poll.timeout.connect(lambda: loop.quit() if finished() else None)
    deadline = QTimer()
    deadline.setSingleShot(True)
    overdue = []

    def slow_shutdown():
        """记录窗口关闭等待超时并报告仍在等待工作线程，保留后续清理所需状态。"""
        overdue.append(True)
        emit("gui_shutdown_slow", message="waiting for worker before fixture cleanup")

    deadline.timeout.connect(slow_shutdown)
    poll.start()
    deadline.start(30000)
    try:
        if not finished():
            loop.exec()
        app.processEvents()
    finally:
        poll.stop()
        deadline.stop()
    # 商店异步任务和下载管理器使用不同线程，即使未下载也须分别关闭。
    download_manager = getattr(getattr(window, "_gb_store_page", None), "_download_manager", None)
    if download_manager is not None:
        download_manager.shutdown()
    # close 仅隐藏窗口；须在 QApplication 和隔离服务仍存活时销毁 QObject 树。
    if shiboken6.isValid(window):
        window.deleteLater()
        app.sendPostedEvents(window, QEvent.Type.DeferredDelete)
    if overdue:
        raise TimeoutError("GUI shutdown exceeded 30 seconds; fixture retained until worker stopped")


class RunningWorkerCounter:
    """观察任务启动，避免每次心跳遍历控件树。"""

    def __init__(self, window, page, app):
        """记录应用和窗口所有者，以弱集合观察 QThread，并关联全局线程池统计。"""
        import shiboken6
        from PySide6.QtCore import QThread, QThreadPool

        self._owners = (window, page, app)
        self._threads = WeakSet()
        self._thread_type = QThread
        self._pool = QThreadPool.globalInstance()
        self._isdeleted = lambda obj: not shiboken6.isValid(obj)
        self._patch = None

    def __enter__(self):
        """观察新的 QThread 启动并补充已有任务，初始化失败时恢复启动入口。"""
        original_start = self._thread_type.start

        def start(thread, *args, **kwargs):
            """将新启动线程加入弱引用集合，再调用原始线程启动方法。"""
            self._threads.add(thread)
            return original_start(thread, *args, **kwargs)

        self._patch = patch.object(self._thread_type, "start", start)
        self._patch.__enter__()
        try:
            # 首次扫描纳入观察前已启动的线程；弱引用注册表不会延长独立任务的生命周期。
            for owner in self._owners:
                self._threads.update(owner.findChildren(self._thread_type))
            self._discover_owned_threads()
        except BaseException:
            self._patch.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, *exc):
        """退出统计范围时恢复 QThread 原始启动方法。"""
        return self._patch.__exit__(*exc)

    def _discover_owned_threads(self):
        """补充固定控制器字段中保存的线程，覆盖已脱离 QObject 父子关系的任务。"""
        # 通过固定字段补充发现首次扫描时已脱离父对象的控制器任务。
        for owner in self._owners:
            for name, value in vars(owner).items():
                if not any(part in name for part in ("worker", "reconcile", "probe", "discovery")):
                    continue
                candidates = value.values() if isinstance(value, dict) else (value,)
                for candidate in candidates:
                    if isinstance(candidate, self._thread_type):
                        self._threads.add(candidate)
                    elif candidate is not None and hasattr(candidate, "__dict__"):
                        self._threads.update(
                            item for item in vars(candidate).values() if isinstance(item, self._thread_type)
                        )

    def count(self):
        """返回仍有效且运行中的已观察线程数，加上全局线程池的活动工作数。"""
        self._discover_owned_threads()
        return (
            sum(thread.isRunning() for thread in tuple(self._threads) if not self._isdeleted(thread))
            + self._pool.activeThreadCount()
        )


def gui_case(manager, root, count, options):
    """在真实主窗口中持续滚动和选择条目，统计心跳、后台工作及对账开销，结束后等待安全关闭。"""
    global _GUI_APP
    from PySide6.QtCore import QEventLoop, Qt, QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    _GUI_APP = app
    if app.platformName().lower() in ("offscreen", "minimal"):
        raise RuntimeError("GUI benchmark requires a real desktop platform")
    from src.ui.main_window import MainWindow

    errors = []
    old_hook = sys.excepthook

    def exception_hook(kind, value, tb):
        """收集 Qt 回调中逃逸的异常，供测量轮次结束时判定失败。"""
        errors.append("".join(traceback.format_exception(kind, value, tb)))

    sys.excepthook = exception_hook
    window = None
    worker_stack = ExitStack()
    try:
        window = MainWindow()
        window.resize(1280, 800)
        window.show()
        window.raise_()
        window.activateWindow()
        # 采样前等待启动对账和文件监控进入稳定状态。
        settle = QEventLoop()
        QTimer.singleShot(3000, settle.quit)
        settle.exec()
        assert window.isVisible() and window._mod_manager is manager
        page = window._overview_page
        first = manager._index.get_all_mods(GAME)[0]
        page._navigate_to(first.node_id)
        settle = QEventLoop()
        QTimer.singleShot(1500, settle.quit)
        settle.exec()
        worker_counter = worker_stack.enter_context(RunningWorkerCounter(window, page, app))
        rounds = []
        for ordinal in range(options.gui_rounds):
            delays, intervals, workers = [], [], []
            actions = Counter()
            metrics_before = copy.deepcopy(getattr(window, "_library_probe_metrics", {}))
            previous = time.perf_counter()
            started = previous
            loop = QEventLoop()
            heartbeat = QTimer()
            heartbeat.setTimerType(Qt.TimerType.PreciseTimer)
            heartbeat.setInterval(16)

            def beat():
                """记录实际心跳间隔、相对目标的延迟和当前活动工作数量。"""
                nonlocal previous
                now = time.perf_counter()
                gap = (now - previous) * 1000
                previous = now
                intervals.append(gap)
                delays.append(max(0.0, gap - 16))
                workers.append(worker_counter.count())

            heartbeat.timeout.connect(beat)
            interaction = QTimer()
            interaction.setInterval(250)

            def interact():
                """周期性推进滚动并选择合成 Mod，维持可重复的界面交互负载。"""
                bar = page._scroll_area.verticalScrollBar()
                step = max(1, bar.pageStep() // 2)
                bar.setValue((bar.value() + step) % (bar.maximum() + 1))
                actions["scroll"] += 1
                visible = [item for item in page._display_items if item["type"] == "mod"]
                if visible:
                    item = visible[(actions["selection"] * 17) % len(visible)]
                    page._on_mod_card_clicked(item["id"])
                    actions["selection"] += 1

            interaction.timeout.connect(interact)
            stop = QTimer()
            stop.setTimerType(Qt.TimerType.PreciseTimer)
            stop.setSingleShot(True)
            stop.timeout.connect(loop.quit)
            with Meter(manager, root) as meter:
                meter.wrap(manager, "reconcile_library", "source_poll")
                meter.wrap(manager, "reconcile_missing_deployments", "deployment_poll")
                for name in ("create_library_probe_request", "apply_library_probe_result"):
                    meter.wrap(manager, name)
                heartbeat.start()
                interaction.start()
                stop.start(round(options.gui_seconds * 1000))
                loop.exec()
                elapsed = time.perf_counter() - started
                heartbeat.stop()
                interaction.stop()
                stop.stop()
            if errors:
                raise RuntimeError("Qt callback failed:\n" + "\n".join(errors))
            assert actions["scroll"] and actions["selection"]
            metrics_after = copy.deepcopy(getattr(window, "_library_probe_metrics", {}))
            metrics_delta = {}
            for key, value in metrics_after.items():
                previous_value = metrics_before.get(key)
                if isinstance(value, list):
                    metrics_delta[key] = value[len(previous_value or []) :]
                elif isinstance(value, (float, int)) and key != "max_active":
                    metrics_delta[key] = value - (previous_value or 0)
                else:
                    metrics_delta[key] = value
            rounds.append(
                {
                    "round": ordinal + 1,
                    "actual_wall_s": elapsed,
                    "heartbeat_samples": len(delays),
                    "heartbeat_delay_ms": percentiles(delays),
                    "heartbeat_interval_ms": percentiles(intervals),
                    "active_workers": {
                        "max": max(workers),
                        "mean": statistics.mean(workers),
                        "definition": "running initially discovered/application-owned/"
                        "observed-start QThreads + global QThreadPool",
                    },
                    "interactions": dict(actions),
                    "library_probe_metrics": metrics_after,
                    "library_probe_metrics_during_round": metrics_delta,
                    **meter.result(),
                }
            )
            emit("gui_round", count=count, round=ordinal + 1, delay_ms=rounds[-1]["heartbeat_delay_ms"])
        return {
            "platform": app.platformName(),
            "window": "src.ui.main_window.MainWindow",
            "window_size": [window.width(), window.height()],
            "heartbeat_target_ms": 16,
            "source_interval_ms": window._source_state_poll.interval(),
            "deployment_interval_ms": window._deploy_state_poll.interval(),
            "acceptance_duration": options.gui_seconds >= 60 and options.gui_rounds >= 3,
            "library_probe_metrics": copy.deepcopy(getattr(window, "_library_probe_metrics", {})),
            "rounds": rounds,
            "correctness": check_library(manager, count)[0],
        }
    finally:
        try:
            if window is not None:
                close_gui_window(window, app)
        finally:
            worker_stack.close()
            sys.excepthook = old_hook


def run_case(root, count, options, checkpoint):
    """按库规模执行所选的重建、轮询、界面或导入场景，逐步保存进度并清理各自隔离目录。"""
    result = {
        "count": count,
        "sample": {
            "members": 8,
            "preview_bytes": len(PREVIEW),
            "tags_per_mod": 2,
            "archive_format": "7z COPY",
            "content": "distinct ordinal ini",
            "os_caches_flushed": False,
        },
    }
    checkpoint(result)
    if options.mode in ("all", "core", "gui"):
        with isolated_manager(root / "library") as manager:
            result["sample"].update(build_library(manager, root / "library", count))
            first, mods = rebuild(manager, root / "library", count, "empty_preview_cache")
            result["rebuild"] = [first]
            identities = {mod.mod_id for mod in mods}
            selected = mods[:: max(1, count // 10)]
            preset = manager.preset_manager.create_preset(
                GAME, "Benchmark remap", {mods[0].node_id: [mod.mod_id for mod in selected]}
            )
            preset_check = (preset.id, {mod.mod_id for mod in selected})
            if options.mode in ("all", "core"):
                for ordinal in range(options.warm_rounds):
                    measured, mods = rebuild(
                        manager, root / "library", count, f"warm_{ordinal + 1}", identities, preset_check
                    )
                    result["rebuild"].append(measured)
                    checkpoint(result)
                with Meter(manager, root / "library") as meter:
                    start = time.perf_counter()
                    assert manager._cleanup_orphaned_managed_artifacts(GAME)
                    elapsed = time.perf_counter() - start
                result["cleanup"] = {"total_wall_s": elapsed, **meter.result()}
                result["poll"] = [poll(manager, root / "library", "single_character_disabled")]
                result["mixed_sample"] = mixed_deployments(manager, mods, root / "library")
                result["poll"].append(poll(manager, root / "library", "four_characters_10pct_deployed"))
                result["correctness"] = check_library(manager, count, identities, preset_check)[0]
                checkpoint(result)
            else:
                result["mixed_sample"] = mixed_deployments(manager, mods, root / "library")
            if options.mode in ("all", "gui"):
                result["gui"] = gui_case(manager, root / "library", count, options)
                checkpoint(result)
        owned_remove(root / "library", root)
    if options.mode in ("all", "intake"):
        with isolated_manager(root / "intake") as manager:
            result["intake"] = intake_case(manager, root / "intake", count)
            checkpoint(result)
        owned_remove(root / "intake", root)
    result["completed"] = True
    checkpoint(result)
    return result


def parse_args(argv=None):
    """解析库规模、运行模式和采样参数，拒绝无效数量、时长及轮次。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", type=int, nargs="+", default=[300, 1000, 5000])
    parser.add_argument("--warm-rounds", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("core", "gui", "intake", "all"), default="all")
    parser.add_argument(
        "--gui-seconds", type=float, default=60, help="shorter values are smoke only, never GUI acceptance"
    )
    parser.add_argument("--gui-rounds", type=int, default=3)
    options = parser.parse_args(argv)
    if min(options.counts) < 1 or options.warm_rounds < 0 or options.gui_seconds <= 0 or options.gui_rounds < 1:
        parser.error("counts/duration/GUI rounds must be positive; warm rounds must be nonnegative")
    return options


def source_version():
    """记录源码及基准脚本摘要、当前提交和工作区状态，标识每次测量使用的代码。"""
    files = sorted((REPO_ROOT / "src").rglob("*.py"))
    hashes = {str(path.relative_to(REPO_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}

    def git(*args):
        """在目标仓库执行只读 Git 查询，以 UTF-8 返回去除末尾空白的输出。"""
        return subprocess.run(
            ["git", "-c", f"safe.directory={REPO_ROOT.as_posix()}", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout.strip()

    return {
        "repository": str(REPO_ROOT),
        "commit": git("rev-parse", "HEAD"),
        "status": git("status", "--short"),
        "source_sha256": hashes,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def main(argv=None):
    """创建临时测试根并按规模运行基准，原子保存结果、错误及清理状态，失败时返回非零状态。"""
    options = parse_args(argv)
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "results.json").exists():
        raise FileExistsError(f"choose a new output directory; preserving {output / 'results.json'}")
    # 使用操作系统临时目录为 QSS 提供可用路径；样本、配置、缓存、日志和插件均位于其下。
    with tempfile.TemporaryDirectory(prefix="d3dx-perf-") as temporary:
        root = Path(temporary).resolve()
        state = {
            "completed": False,
            "version": source_version(),
            "environment": {
                "python": sys.version,
                "platform": platform.platform(),
                "sqlite": sqlite3.sqlite_version,
                "logical_cpus": os.cpu_count(),
                "free_disk_bytes": shutil.disk_usage(root).free,
            },
            "arguments": {
                key: str(value) if isinstance(value, Path) else value for key, value in vars(options).items()
            },
            "isolation": {"temporary_root": str(root), "cleaned": False},
            "measurement_notes": [
                "wall times include identical instrumentation overhead",
                "thread timing sums overlap; never add them to derive total time",
                "file_stat_calls count target-root OS stat requests including exists/type checks",
                "core poll measurements exclude setup, validation, startup and GUI",
            ],
            "cases": [],
        }

        def save():
            """先写入待发布 JSON 文件，再替换结果文件，减少中断造成的记录截断。"""
            pending = output / "results.pending.json"
            pending.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            pending.replace(output / "results.json")

        save()
        try:
            for count in options.counts:
                case_root = root / f"count-{count}"
                case_root.mkdir()
                state["cases"].append({"count": count, "completed": False})

                def checkpoint(case):
                    """更新当前库规模的结果快照并立即保存。"""
                    state["cases"][-1] = case
                    save()

                run_case(case_root, count, options, checkpoint)
                owned_remove(case_root, root)
            state["completed"] = True
        except BaseException as error:
            state["error"] = {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}
            emit("failed", error=str(error))
        finally:
            # 每次递归清理前都校验路径范围，包含中断和失败场景；临时根目录由 TemporaryDirectory 管理。
            try:
                for child in root.iterdir():
                    if child.is_dir():
                        owned_remove(child, root)
                    else:
                        child.unlink()
                state["isolation"]["cleaned"] = True
            except OSError as error:
                state["completed"] = False
                state["cleanup_error"] = repr(error)
            save()
    emit("finished", completed=state["completed"], output=str(output / "results.json"))
    return 0 if state["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
