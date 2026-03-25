"""管理单次重建的预览候选与完整目录发布。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType

from src.utils import fs
from src.utils.logger import logger

PreviewCacheIndex = Mapping[str, tuple[Path, ...]]
CACHE_FINGERPRINT = ".content-fingerprint.json"


def cache_content_hash(cache: Path) -> str:
    """读取预览缓存记录的内容摘要，文件缺失或格式损坏时返回空字符串。"""
    try:
        data = json.loads(fs.read_text(cache / CACHE_FINGERPRINT, encoding="utf-8"))
        return data.get("content_hash", "") if isinstance(data, dict) else ""
    except (OSError, ValueError):
        return ""


def capture_source_version(path: str | Path, *, checkpoint=None) -> tuple:
    """在内容摘要和预览读取前后采集轻量变化证据，包括文件替换。"""
    if checkpoint is not None:
        checkpoint()
    root = Path(fs.normal_path(path))
    info = Path(fs.native_path(root)).stat()
    if not stat.S_ISDIR(info.st_mode):
        return (("", info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns),)
    values = []
    members = fs.iter_files(root) if checkpoint is None else _iter_version_files(root, checkpoint)
    for member in sorted(members):
        if checkpoint is not None:
            checkpoint()
        relative = member.relative_to(root)
        if ".d3dxmeta" in relative.parts or member.name == ".d3dxmod":
            continue
        info = Path(fs.native_path(member)).stat()
        values.append((relative.as_posix(), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    return tuple(values)


def _iter_version_files(root: Path, checkpoint):
    """按目录遍历规则选择文件，并在每次枚举中检查取消。"""
    pending = [root]
    while pending:
        checkpoint()
        directory = pending.pop()
        folders, files = [], []
        try:
            with os.scandir(fs.native_path(directory)) as children:
                for child in children:
                    checkpoint()
                    try:
                        is_directory = child.is_dir()
                    except OSError:
                        is_directory = False
                    path = directory / child.name
                    (folders if is_directory else files).append(path)
        except InterruptedError:
            raise
        except OSError:
            # 目录遍历可能跳过不可读或消失的目录，后续快照和导入仍按各自规则校验。
            continue
        for member in files:
            checkpoint()
            yield member
        for folder in folders:
            checkpoint()
            if not Path(fs.native_path(folder)).is_symlink():
                pending.append(folder)


def build_preview_cache_index(root: Path) -> PreviewCacheIndex:
    """根目录只枚举一次，包括缺失状态，之后不修改快照。"""
    candidates: dict[str, list[Path]] = {}
    try:
        with os.scandir(fs.native_path(root)) as entries:
            for entry in entries:
                try:
                    if entry.is_dir():
                        candidate = root / entry.name
                        digest = cache_content_hash(candidate)
                        if digest:
                            candidates.setdefault(digest, []).append(candidate)
                except OSError:
                    continue
    except OSError:
        pass
    return MappingProxyType({key: tuple(sorted(paths)) for key, paths in candidates.items()})


def read_cached_previews(cache: Path, extensions: set[str]) -> list[Path]:
    """仅已发布目录可作为候选，不创建实例专用元数据文件。"""
    try:
        return sorted(path for path in fs.iter_files(cache) if path.suffix.lower() in extensions)
    except OSError:
        return []


@contextmanager
def preview_cache_staging(cache: Path):
    """不完整文件保存在候选根之外，崩溃后也不作为有效缓存。

    暂存目录与目标同卷，便于原子改名；目标由在途保护覆盖，普通孤立缓存清理不进入暂存目录。
    """
    fs.ensure_dir(cache.parent)
    stage = Path(fs.normal_path(tempfile.mkdtemp(prefix=".preview-", dir=fs.native_path(cache.parent.parent))))
    try:
        yield stage
    finally:
        # 恢复失败时保留原有完整缓存。
        previous = stage.with_name(stage.name + ".previous")
        if fs.exists(previous):
            logger.warning("预览缓存备份仍保留在: %s", previous)
        fs.rmtree(stage, ignore_errors=True)


def publish_preview_cache(stage: Path, cache: Path, images: Sequence[Path], *, content_hash: str = "") -> list[Path]:
    """发布完整暂存目录，替换失败时恢复旧缓存。

    调用前校验成员完整性和源稳定性，读取者只能看到旧目录、短暂缺失或完整新目录。
    """
    relative = [Path(path).relative_to(stage) for path in images]
    if not relative or not all(fs.is_file(stage / path) for path in relative):
        raise OSError("Preview cache is incomplete")
    if content_hash:
        fs.write_text(stage / CACHE_FINGERPRINT, json.dumps({"content_hash": content_hash}), encoding="utf-8")
    previous = stage.with_name(stage.name + ".previous")
    had_previous = fs.exists(cache)
    if had_previous:
        os.replace(fs.native_path(cache), fs.native_path(previous))
    try:
        os.replace(fs.native_path(stage), fs.native_path(cache))
    except OSError:
        if had_previous:
            os.replace(fs.native_path(previous), fs.native_path(cache))
        raise
    if had_previous:
        fs.rmtree(previous, ignore_errors=True)
    return [cache / path for path in relative]
