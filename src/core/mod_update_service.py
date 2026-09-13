"""编排 Mod 更新候选收集、检查、状态持久化、忽略规则、安装和回滚。"""

from __future__ import annotations

import json
import time
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from src.core.mod_update_tasks import ModUpdateTasks
from src.services.gamebanana.models import GbInstallMetadata, GbModel
from src.services.gamebanana.update_checker import (
    ModUpdateChecker,
    UpdateInfo,
    pick_default_file,
)
from src.services.gamebanana.url_helper import parse_gb_url
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_data_dir

_GB = "gamebanana"
_CFG = "gamebanana.update_check"
_PREPARATION_SLICE_SECONDS = 0.004


def _idle_check_snapshot(*, checking: bool = False) -> dict:
    """构造空闲检查快照，计数和时间归零，允许调用方指定忙碌标记。"""
    return {
        "checking": checking,
        "phase": "idle",
        "started_at": 0.0,
        "last_progress_at": 0.0,
        "finished_at": 0.0,
        "total": 0,
        "done": 0,
        "found": 0,
        "failed": 0,
        "retry_only": False,
    }


@dataclass(frozen=True)
class _IgnoreUndo:
    """保存忽略操作前后的字段及来源身份，供撤销时校验条目是否仍适用。"""

    game_id: str
    mod_id: str
    before: dict
    after: dict
    source: dict
    info: UpdateInfo | None


def _hours_since(iso: str, now: datetime) -> float:
    """计算距 ISO 时间的小时数；时间无效或时区不兼容时返回足够大的间隔以允许重查。"""
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds() / 3600.0
    except Exception:
        return 1e9  # 解析失败当作"很久没查"，不跳过


class ModUpdateService(QObject):
    """在 Qt 所在线程编排更新检查与安装，隔离游戏切换后的结果并持久化忽略状态。"""

    check_started = Signal(int)
    progress = Signal(int, int)
    status_changed = Signal()
    check_finished = Signal(dict)
    update_applied = Signal(str, object)
    update_failed = Signal(str, str)
    update_deferred = Signal(str)
    update_task_changed = Signal(str, dict)
    update_cancelled = Signal(str)

    def __init__(self, mod_manager, config, client, parent: QObject | None = None, *, submit=None) -> None:
        """连接检查器和安装队列信号；可注入共享循环调度器，避免 HTTP 连接池跨循环使用。"""
        super().__init__(parent)
        self._mm = mod_manager
        self._config = config
        self._client = client
        self._download_manager = None
        self._status: dict[str, UpdateInfo] = {}
        self._failures: dict[str, str] = {}
        self._status_game_id = mod_manager.current_game_id
        self._check_game_id: str | None = None
        self._check_in_progress = False
        self._check_token: object | None = None
        self._check_preparation = None
        self._preparation_timer = QTimer(self)
        self._preparation_timer.setSingleShot(True)
        self._preparation_timer.timeout.connect(self._prepare_check_slice)
        self._check_snapshot = _idle_check_snapshot()
        self._snapshot_game_id = mod_manager.current_game_id
        self._status_before_check: dict[str, UpdateInfo] = {}
        self._failures_before_check: dict[str, str] = {}
        self._check_jobs: dict[str, GbInstallMetadata] = {}
        self._check_outcomes: dict[str, str] = {}
        self._check_superseded: set[str] = set()
        self._pending_last_checked: set[str] = set()
        self._update_tasks = ModUpdateTasks(mod_manager, config, self)
        self._update_tasks.changed.connect(self.update_task_changed.emit)
        self._update_tasks.applied.connect(self._on_task_applied)
        self._update_tasks.failed.connect(self.update_failed.emit)
        self._update_tasks.canceled.connect(self.update_cancelled.emit)
        self._update_tasks.deferred.connect(self.update_deferred.emit)
        # 通过共享调度器使更新检查与商店客户端使用同一事件循环，
        # 避免跨循环复用 HTTP 连接池。没有调度器时检查器使用独立客户端循环。
        self._checker = ModUpdateChecker(client, self, submit=submit)
        self._checker.mod_update_found.connect(self._on_update_found)
        self._checker.mod_up_to_date.connect(self._on_up_to_date)
        self._checker.mod_check_failed.connect(self._on_failed)
        self._checker.progress.connect(self._on_check_progress)
        self._checker.finished.connect(self._on_finished)

    # 注入
    def set_download_manager(self, dm) -> None:
        """替换共享下载器并重绑并发刷新通知，重复设置同一实例不产生额外连接。"""
        if self._download_manager is dm:
            return
        previous_signal = getattr(self._download_manager, "queue_changed", None)
        if previous_signal is not None:
            try:
                previous_signal.disconnect(self._update_tasks.refresh_concurrency)
            except (TypeError, RuntimeError):
                pass
        self._download_manager = dm
        self._update_tasks.download_manager = dm
        queue_changed = getattr(dm, "queue_changed", None)
        if queue_changed is not None:
            queue_changed.connect(self._update_tasks.refresh_concurrency)
        self._update_tasks.refresh_concurrency()

    # 状态查询
    def get_update_status(self, *, completed_only: bool = False) -> dict[str, UpdateInfo]:
        """返回更新状态字典副本；要求完整结果时，在当前检查期间返回检查前快照。"""
        if completed_only and self._is_current_check():
            return dict(self._status_before_check)
        return dict(self._status)

    def updatable_ids(self) -> set[str]:
        """返回当前已发现更新的本地 Mod ID 集合副本。"""
        return set(self._status.keys())

    def failures(self) -> dict[str, str]:
        """返回本轮保留的检查失败原因副本，供界面展示和重试筛选。"""
        return dict(self._failures)

    def is_checking(self) -> bool:
        """报告是否仍在准备或执行检查，包括切换游戏后尚未收尾的检查。"""
        return self._check_in_progress

    def check_snapshot(self) -> dict:
        """供关闭再打开的窗口恢复检查状态；所有时间都是 monotonic 秒。"""
        if self._snapshot_game_id != self._mm.current_game_id:
            # 旧游戏的任务仍占用 checker，但不能把它的耗时和统计套到新游戏。
            return _idle_check_snapshot(checking=self._check_in_progress)
        return dict(self._check_snapshot)

    # status.json
    def _status_path(self, game_id: str | None = None) -> Path:
        """按指定游戏的资源目录定位更新状态文件；省略游戏时使用当前选择。"""
        gid = self._mm.current_game_id if game_id is None else game_id
        res = self._mm._res_dir(gid) if hasattr(self._mm, "_res_dir") else gid
        return get_data_dir() / res / "update_status.json"
