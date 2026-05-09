"""更新任务状态、共享下载、进程控制与确认暂停。"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication, QObject, Signal

from src.core.archive_concurrency import effective_archive_workers
from src.core.mod_update_concurrency import effective_update_concurrency
from src.core.mod_update_transaction import cleanup_update, commit_update, make_update_plan
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.qt_timers import call_later

TERMINAL_PHASES = frozenset({"done", "failed", "canceled", "deferred"})


@dataclass(eq=False)
class _UpdateTask:
    """保存单个游戏内更新的阶段、暂停来源、共享下载持有权及安装暂存资源。"""

    game_id: str
    mid: str
    file: object
    profile: object
    metadata: object
    consumer_id: str = field(default_factory=lambda: f"mod-update:{uuid.uuid4().hex}")
    info: object = None
    phase: str = "queued"
    download_started: bool = False
    import_started: bool = False
    user_paused: bool = False
    transfer_paused: bool = False
    holds: set[str] = field(default_factory=set)
    cancel_requested: bool = False
    download: object = None
    process: object = None
    plan: dict | None = None
    archive: Path | None = None
    prepared: dict | None = None
    bindings: list = field(default_factory=list)
    data: dict = field(default_factory=dict)
    last_download_state: str = ""
    resume_pending: bool = False

    @property
    def key(self) -> tuple[str, str]:
        """返回游戏 ID 与 Mod ID 组成的队列键，隔离不同游戏的任务。"""
        return self.game_id, self.mid

    @property
    def active(self) -> bool:
        """判断任务阶段是否尚未进入完成、失败、取消或延后等终态。"""
        return self.phase not in TERMINAL_PHASES

    @property
    def paused(self) -> bool:
        """合并用户暂停、下载暂停及确认持有状态；终态和取消中的任务不算暂停。"""
        return (
            self.active and not self.cancel_requested and (self.user_paused or self.transfer_paused or bool(self.holds))
        )

    def snapshot(self) -> dict:
        """生成界面快照，保留实际恢复阶段，并为暂停或停止状态清零速度及剩余时间估计。"""
        snapshot = {
            "phase": self.phase,
            "resume_phase": self.phase,
            "game_id": self.game_id,
            "file_name": self.file.file_name,
            "downloaded_bytes": 0,
            "total_bytes": max(0, self.file.size_bytes),
            "speed_bps": 0.0,
            "eta_seconds": -1.0,
            "stage_progress": None,
            "detail": "",
            "error": "",
            **self.data,
        }
        if self.paused:
            snapshot.update(phase="paused", speed_bps=0.0, eta_seconds=-1.0)
        elif self.cancel_requested and self.active:
            snapshot.update(phase="stopping", speed_bps=0.0, eta_seconds=-1.0)
        return snapshot


@dataclass
class StopConfirmation:
    """持有一组待确认停止的任务及独立暂停令牌，防止重复结算影响任务状态。"""

    token: str
    tasks: tuple[_UpdateTask, ...]
    settled: bool = False

    @property
    def count(self) -> int:
        """返回发起确认时纳入凭据的任务数量。"""
        return len(self.tasks)


class ModUpdateTasks(QObject):
    """在 Qt 线程调度下载与导入阶段，管理共享归档引用、暂停确认及异步暂存清理。"""

    changed = Signal(str, dict)
    applied = Signal(str, object)
    failed = Signal(str, str)
    canceled = Signal(str)
    deferred = Signal(str)

    def __init__(self, manager, config, parent=None):
        """建立跨游戏任务表和清理线程记录，并在存在 Qt 应用时连接退出清理。"""
        super().__init__(parent)
        self._manager, self._config = manager, config
        self.download_manager = None
        self._records: dict[tuple[str, str], _UpdateTask] = {}
        self._pumping = False
        self._pump_pending = False
        self._closing = False
        self._cleanup_threads: list[threading.Thread] = []
        self._finished_archives: dict[Path, tuple[int, str]] = {}
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def snapshots(self) -> dict[str, dict]:
        """按 Mod ID 返回当前游戏所有任务的独立状态快照。"""
        return {
            job.mid: job.snapshot() for job in self._records.values() if job.game_id == self._manager.current_game_id
        }

    def infos(self) -> dict:
        """返回当前游戏具有更新详情的任务映射，详情对象沿用任务持有的实例。"""
        return {
            job.mid: job.info
            for job in self._records.values()
            if job.game_id == self._manager.current_game_id and job.info is not None
        }

    def active_ids(self, *, all_games: bool = False) -> list[str]:
        """列出非终态任务的 Mod ID，默认只覆盖当前游戏。"""
        return [
            job.mid
            for job in self._records.values()
            if (all_games or job.game_id == self._manager.current_game_id) and job.active
        ]

    def clear_completed(self) -> bool:
        """当前游戏记录的更新全部成功后才移除历史。"""
        jobs = [job for job in self._records.values() if job.game_id == self._manager.current_game_id]
        if not jobs or any(job.phase != "done" for job in jobs):
            return False
        for job in jobs:
            del self._records[job.key]
        # 任务结束负责释放消费者和清理文件，移除显示历史不重复清理，也不影响其他游戏。
        return True

    def _current(self, job: _UpdateTask) -> bool:
        """校验回调仍对应队列中的活动实例，关闭期间拒绝继续推进。"""
        return self._records.get(job.key) is job and job.active and not self._closing
