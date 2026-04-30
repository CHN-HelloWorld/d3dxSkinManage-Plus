"""预设删除候选与独立的文件暂存工作。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event


class StalePresetDelete(RuntimeError):
    """表示删除准备后预设、文件版本或当前选择已变化，原候选不可提交。"""

    pass


def file_version(path):
    """提取文件身份、大小和时间版本，文件不存在时返回 None。"""
    try:
        value = Path(path).stat()
    except FileNotFoundError:
        return None
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


@dataclass
class PreparedPresetDelete:
    """保存预设文件变更候选、原始版本、暂存状态和提交后的部署清理范围。"""

    owner_id: int
    mod_id: str
    game_id: str
    target: Path
    temporary: Path
    token: tuple
    target_version: tuple | None
    data: dict
    runtime: dict
    updated_at: dict
    durable: dict
    resolved: dict
    cancelled: Event = field(default_factory=Event, repr=False)
    written: Event = field(default_factory=Event, repr=False)
    staged_version: tuple | None = None
    committed: bool = False
    removed_presets: tuple[str, ...] = ()
    cleanup_records: tuple = ()


def discard_prepared_preset_delete(candidate):
    """暂存期间也可请求清理，工作线程关闭文件后会再次清理。"""
    if candidate.committed:
        return
    candidate.cancelled.set()
    try:
        candidate.temporary.unlink(missing_ok=True)
    except PermissionError:
        # Windows 可能在工作线程关闭文件前继续占用临时文件。
        if candidate.written.is_set():
            raise


def write_prepared_preset_delete(candidate):
    """工作线程只序列化、写入并刷新本次唯一的临时文件。"""
    if candidate.written.is_set() or candidate.committed:
        raise StalePresetDelete("Preset deletion candidate was already staged")

    def check():
        """检查预设暂存任务的取消事件，取消时抛出中断异常。"""
        if candidate.cancelled.is_set():
            raise InterruptedError("Preset deletion staging cancelled")

    try:
        check()
        payload = json.dumps(candidate.data, indent=2, ensure_ascii=False)
        check()
        candidate.temporary.parent.mkdir(parents=True, exist_ok=True)
        with candidate.temporary.open("x", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            check()
            os.fsync(stream.fileno())
        check()
        candidate.staged_version = file_version(candidate.temporary)
        candidate.written.set()
        return candidate
    except BaseException:
        candidate.cancelled.set()
        candidate.temporary.unlink(missing_ok=True)
        raise


def clean_unused_preset_deployments(records, root, storage, progress=None):
    """只检查已记录且没有引用的禁用目录，不扫描全库。"""
    from src.core.mod_identity import read_deployment_identity
    from src.utils.constants import MIGOTO_DISABLED_PREFIX

    removed, errors = [], []
    root = Path(root).resolve() if root is not None else None
    for index, record in enumerate(records, 1):
        target = Path(record.actual_path)
        try:
            prefix = f"{MIGOTO_DISABLED_PREFIX}_{record.mod_id[:12]}_".upper()
            if root is None or target.parent.resolve() != root or not target.name.upper().startswith(prefix):
                continue
            marker = read_deployment_identity(target)
            if marker is None or marker["mod_id"] != record.mod_id:
                continue
            if storage.remove_deploy_path(target):
                removed.append(record)
            else:
                errors.append(record.mod_id)
        except OSError:
            errors.append(record.mod_id)
        finally:
            if progress is not None:
                progress(index, len(records), target.name)
    return removed, errors
