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

    def _emit(self, job: _UpdateTask) -> None:
        """仅在未关闭且任务属于当前游戏时发布状态快照。"""
        if job.game_id == self._manager.current_game_id and not self._closing:
            self.changed.emit(job.mid, job.snapshot())

    def start(self, mid: str, file, metadata, *, info=None, archive=None, game_id=None) -> None:
        """为本地条目创建更新任务并持有归档引用；已有活动任务时忽略重复请求。"""
        gid = self._manager.current_game_id if game_id is None else game_id
        previous = self._records.get((gid, mid))
        if previous is not None and previous.active:
            return
        mod = self._manager.get_mod(mid)
        if mod is None:
            self.failed.emit(mid, tr("找不到本地 Mod"))
            return
        profile = SimpleNamespace(mod_id=metadata.mod_id, name=mod.display_name)
        job = _UpdateTask(
            gid, mid, file, profile, metadata, info=info, archive=Path(archive) if archive is not None else None
        )
        self._records[job.key] = job
        if self.download_manager is not None:
            self.download_manager.retain(file.file_id, file.md5_checksum, owner=job.consumer_id, path=job.archive)
        self._emit(job)
        self._pump()

    def refresh_concurrency(self) -> None:
        """刷新各处理环节的并发限制，不中断已准入的工作。"""
        self._pump()

    def _pump(self) -> None:
        """串行执行调度轮次，将信号引起的重入合并为下一轮，关闭期间停止调度。"""
        if self._closing:
            return
        if self._pumping:
            self._pump_pending = True
            return
        self._pumping = True
        try:
            while not self._closing:
                self._pump_pending = False
                self._pump_stages()
                # 信号可能追加、恢复任务或释放名额，当前轮次结束后再重新调度。
                if not self._pump_pending:
                    break
        finally:
            self._pumping = False

    def _pump_stages(self) -> None:
        """分别按下载和解压并发上限推进任务，缓存归档可直接等待导入而不占网络名额。"""
        download_limit = effective_update_concurrency(self._config.get("gamebanana.update_check.concurrency"))
        import_limit = effective_archive_workers(self._config.get("import.parallel_workers"))
        downloading = sum(job.active and job.download_started and job.archive is None for job in self._records.values())
        for job in list(self._records.values()):
            if not job.active or job.cancel_requested or job.paused or job.download_started or job.archive is not None:
                continue
            # 已完成的下载不占网络名额，可独立等待导入。
            cached = self._cached_archive(job)
            if cached is not None:
                self._download_finished(job, cached)
                continue
            if downloading >= download_limit:
                continue
            job.download_started = True
            self._download(job)
            downloading += int(job.active and job.archive is None)

        # 缓存命中可能同步完成，每轮只统计一次各环节数量，避免逐任务重复求和。
        importing = sum(job.active and job.import_started for job in self._records.values())
        for job in list(self._records.values()):
            if not job.active or job.cancel_requested or job.paused or job.import_started or job.archive is None:
                continue
            if importing >= import_limit:
                break
            job.import_started = True
            self._prepare(job)
            importing += int(job.active)

    def _cached_archive(self, job) -> Path | None:
        """优先按 MD5 查找缓存，再检查已完成下载的归档；不可用时返回 None。"""
        dm = self.download_manager
        if dm is None:
            return None
        if job.file.md5_checksum:
            cached = dm.has_local_md5(job.file.md5_checksum)
            if cached is not None:
                return Path(cached)
        task = dm.get_task(job.file.file_id)
        if task is not None and task.state == "done":
            try:
                if Path(task.archive_path).is_file():
                    return Path(task.archive_path)
            except OSError:
                pass
        return None

    def _bind(self, job, signal, callback) -> None:
        """连接任务回调并记录连接，供任务转换阶段或结束时统一断开。"""
        signal.connect(callback)
        job.bindings.append((signal, callback))

    @staticmethod
    def _disconnect(job) -> None:
        """断开任务记录的全部信号连接，容忍 Qt 对象或连接已经释放。"""
        for signal, callback in job.bindings:
            try:
                signal.disconnect(callback)
            except (TypeError, RuntimeError):
                pass
        job.bindings.clear()

    def _download(self, job) -> None:
        """取得或复用共享下载并绑定进度，缓存命中直接转入导入等待，异常转为任务失败。"""
        dm = self.download_manager
        if dm is None:
            self._finish(job, "failed", tr("下载器不可用"))
            return
        try:
            task = dm.request(job.profile, job.file, game_id=job.game_id, consumer=job.consumer_id)
            if task is None:
                path = dm.has_local_md5(job.file.md5_checksum)
                if path is None:
                    raise OSError(tr("下载缓存不存在"))
                self._download_finished(job, path)
                return
            job.download = task
            self._bind(job, task.finished, lambda path: self._download_finished(job, path))
            self._bind(job, task.failed, lambda error: self._finish(job, "failed", error))
            if hasattr(task, "progress"):
                self._bind(job, task.progress, lambda done, total: self._download_progress(job, done, total))
            if hasattr(task, "state_changed"):
                self._bind(job, task.state_changed, lambda state: self._download_state(job, state))
            if hasattr(dm, "task_removed"):
                self._bind(job, dm.task_removed, lambda fid: self._download_removed(job, fid))
            state = getattr(task, "state", "preparing")
            job.last_download_state = state
            if state in {"failed", "canceled", "paused"} and not job.paused:
                dm.retry(job.file.file_id)
                state = getattr(task, "state", "queued")
            if state == "done":
                if Path(task.archive_path).is_file():
                    self._download_finished(job, task.archive_path)
                else:
                    self._disconnect(job)
                    dm.delete_task(job.file.file_id)
                    job.download = None
                    self._download(job)
                return
            job.phase = state if state in {"queued", "preparing", "downloading"} else "preparing"
            done, total = getattr(task, "last_progress", (0, job.file.size_bytes))
            job.data.update(
                downloaded_bytes=done,
                total_bytes=total,
                speed_bps=float(getattr(task, "speed_bps", 0.0)),
                eta_seconds=float(getattr(task, "eta_seconds", -1.0)),
            )
            if job.paused:
                self._pause_transfer(job)
            self._emit(job)
        except Exception as error:
            self._finish(job, "failed", tr("下载失败：{error}", error=error))

    def _download_removed(self, job, file_id) -> None:
        """共享下载记录被移除时，将依赖同一文件的更新标为失败。"""
        if job.file.file_id == file_id:
            self._finish(job, "failed", tr("更新下载任务已移除"))

    def _download_progress(self, job, done, total) -> None:
        """更新有效且未暂停任务的字节进度、速度和剩余时间，并通知界面。"""
        if not self._current(job) or job.cancel_requested or job.paused:
            return
        job.data.update(
            downloaded_bytes=done,
            total_bytes=total,
            speed_bps=float(getattr(job.download, "speed_bps", 0.0)),
            eta_seconds=float(getattr(job.download, "eta_seconds", -1.0)),
        )
        self._emit(job)

    def _download_state(self, job, state) -> None:
        """同步下载阶段及暂停来源；取消时等下载退出后收尾，恢复共享下载不解除独立暂停。"""
        if not self._current(job) or job.cancel_requested:
            return
        job.last_download_state = state
        if state == "canceled":
            job.cancel_requested = True
            self._disconnect(job)
            self._emit(job)
            self._wait_download_exit(job)
        elif state == "paused":
            if job.resume_pending and not job.paused:
                self._resume_transfer(job)
                return
            if not job.user_paused and not job.holds:
                job.transfer_paused = True
            self._emit(job)
            self._pump()
        elif state in {"queued", "preparing", "downloading"}:
            # 共享下载可因其他更新而恢复，但不能解除当前更新的用户暂停或确认等待。
            job.transfer_paused = False
            job.phase = state
            if job.paused:
                self._pause_transfer(job)
            self._emit(job)
