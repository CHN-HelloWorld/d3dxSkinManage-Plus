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
            config.set("app_update.enabled", False)
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
