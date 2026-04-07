"""协调后台文件准备、界面线程索引提交及导入批次的暂停和清理。

工作线程接收已规划的 IntakeRequest，通过结果信号与主线程逐项确认提交；
并行准备完成后仍串行交接数据库操作，控制器使用宿主的任务中心展示状态。
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QMessageBox,
    QWidget,
)

from src.core.archive_concurrency import effective_archive_workers
from src.core.mod_intake import (
    DuplicateIntakeError,
    IntakeRequest,
    IntakeTargetMissingError,
    ModIntakeService,
    PreparedIntake,
)
from src.models.mod import Mod
from src.ui.widgets.task_center import (
    ST_DONE,
    ST_FAILED,
    ST_QUEUED,
    ST_ROLLED_BACK,
    ST_RUNNING,
)
from src.utils import fs
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_data_dir
from src.utils.qt_timers import call_later

# 限制可传递到导入计划的附加参数，避免无效键导致整个批次失败。
_PLAN_EXTRA_KEYS = frozenset(
    {
        "source_name",
        "target_format",
        "strict_format",
        "auto_preview",
        "excluded_previews",
        "gamebanana_metadata",
        "force_age_rating",
        "context",
    }
)


def _item_label(item, source) -> str:
    """选择任务明细使用的显示名称。

    优先使用独立的显示字段，避免展示下载缓存文件名；该字段不写入 Mod 的正式名称。
    """
    try:
        if len(item) >= 6 and item[5]:
            return str(item[5])
        if len(item) >= 2 and item[1]:
            return str(item[1])
    except TypeError:
        pass
    return Path(str(source)).name


def _wait_for_worker(worker) -> bool:
    """协作式暂停点；终止会唤醒 Event 并返回 False。"""
    if not worker._paused.is_set():
        worker._paused.wait()
    return not worker._terminated


def _require_intake_requests(items) -> list[IntakeRequest]:
    """将输入具体化为请求列表，包含非 IntakeRequest 元素时抛出 TypeError。"""
    requests = list(items)
    if any(not isinstance(item, IntakeRequest) for item in requests):
        raise TypeError("import workers accept IntakeRequest items only")
    return requests


def _rollback_prepared(service, result) -> None:
    """仅回滚尚未提交的准备结果，忽略其他结果类型，并记录回滚异常而不阻断终止收尾。"""
    if not isinstance(result, PreparedIntake) or result.committed:
        return
    try:
        service.rollback(result)
    except Exception:
        logger.exception("取消导入时回滚准备结果失败")


class _CreatedImportArtifacts:
    """线程安全地登记本批实际创建的容器和预览缓存，使清理范围有明确依据。"""

    def __init__(self) -> None:
        """创建由同一互斥锁保护的容器与预览缓存路径集合。"""
        self._lock = threading.Lock()
        self._containers: set[str] = set()
        self._preview_caches: set[str] = set()

    def add_container(self, path: str | Path) -> None:
        """规范化并登记本批新建容器路径，可由多个准备线程并发调用。"""
        with self._lock:
            self._containers.add(fs.normal_path(path))

    def add_preview_cache(self, path: str | Path) -> None:
        """规范化并登记本批新建预览缓存路径，使用锁避免并发修改集合。"""
        with self._lock:
            self._preview_caches.add(fs.normal_path(path))

    def containers(self) -> list[Path]:
        """在锁内取得容器路径快照，返回独立列表供完成后的清理使用。"""
        with self._lock:
            return [Path(path) for path in self._containers]
