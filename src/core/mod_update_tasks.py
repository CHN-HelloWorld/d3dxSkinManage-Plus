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

    @staticmethod
    def _retire_terminal_resources(job) -> None:
        """销毁已退出的工作进程，并释放历史展示不再需要的准备资源。"""
        process, job.process = job.process, None
        job.plan = None
        job.prepared = None
        job.download = None
        if process is not None:
            process.deleteLater()

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

    def _download_finished(self, job, path) -> None:
        """接收一次归档完成结果并保留文件引用，发布等待导入状态后重新调度。"""
        if not self._current(job) or job.cancel_requested or job.archive is not None:
            return
        self._disconnect(job)
        job.archive = Path(path)
        job.phase = "queued"
        try:
            size = job.archive.stat().st_size
        except OSError as error:
            self._finish(job, "failed", tr("更新文件无法读取：{error}", error=error))
            return
        job.data.update(downloaded_bytes=size, total_bytes=size, speed_bps=0.0, eta_seconds=-1.0, detail=tr("等待导入"))
        # 缓存保留策略可能同步唤醒调度器，先发布完整状态，避免覆盖调度结果。
        if self.download_manager is not None:
            self.download_manager.retain(
                job.file.file_id, job.file.md5_checksum, owner=job.consumer_id, path=job.archive
            )
        if not self._current(job):
            return
        self._emit(job)
        if self._current(job):
            self._pump()

    def _prepare(self, job) -> None:
        """保护库资源并创建安装计划与工作进程；切换游戏时延后安装，准备异常转为失败。"""
        if not self._current(job) or job.paused or job.cancel_requested or job.process is not None:
            return
        if job.game_id != self._manager.current_game_id:
            self._finish(job, "deferred", tr("请切回原游戏后重试，更新文件已保留"))
            return
        reservation = "update-preparing:" + job.consumer_id
        try:
            # 创建工作目录或进入可重入检查前预留资源。
            self._manager.protect_library_operation(reservation, mod_ids=(job.mid,))
            from src.core.mod_update_process import UpdateProcess

            job.plan = make_update_plan(
                self._manager,
                job.mid,
                job.archive,
                job.metadata,
            )
            self._manager.protect_library_operation(
                "update:" + job.plan["job_id"],
                mod_ids=(job.mid,),
                paths=tuple(
                    job.plan.get(key, "") for key in ("container", "work_root", "deploy_target", "deploy_work")
                ),
            )
            job.process = UpdateProcess(job.plan, parent=self)
            job.process.progress.connect(lambda snapshot: self._process_progress(job, snapshot))
            job.process.succeeded.connect(lambda result: self._process_succeeded(job, result))
            job.process.failed.connect(lambda error: self._finish(job, "failed", error))
            job.process.canceled.connect(lambda: self._finish(job, "canceled"))
            job.phase = "verifying"
            job.data["detail"] = tr("正在校验更新文件")
            self._emit(job)
            if self._current(job) and not job.paused and not job.cancel_requested:
                job.process.start()
        except Exception as error:
            self._finish(job, "failed", str(error))
        finally:
            self._manager.release_library_operation(reservation)

    def _process_progress(self, job, snapshot) -> None:
        """将工作进程的有效进度字段合并到任务快照，保留队列持有的文件身份。"""
        if not self._current(job) or job.cancel_requested or job.paused:
            return
        job.phase = snapshot.get("phase", job.phase)
        job.data.update(
            {
                key: value
                for key, value in snapshot.items()
                if key not in {"phase", "resume_phase", "sequence", "file_name"}
            }
        )
        self._emit(job)

    def _process_succeeded(self, job, result) -> None:
        """保存准备结果，未暂停时提交更新；提交失败按当前游戏状态标记失败或延后。"""
        if not self._current(job) or job.cancel_requested:
            return
        job.prepared = result
        if job.paused:
            return
        try:
            updated = commit_update(self._manager, job.plan, result)
        except Exception as error:
            phase = "deferred" if job.game_id != self._manager.current_game_id else "failed"
            self._finish(job, phase, str(error))
            return
        archive = job.archive
        self._finished_archives[archive] = (job.file.file_id, (job.file.md5_checksum or "").lower())
        if not self._finish(job, "done"):
            self._finished_archives.pop(archive, None)
            return
        self.applied.emit(job.mid, updated)

    def _finish(self, job, phase: str, error: str = "") -> bool:
        """使已退出的活动任务进入终态并回收资源，成功结算时返回 True。"""
        if self._records.get(job.key) is not job or not job.active:
            return False
        if job.process is not None and job.process.running:
            logger.warning("更新工作进程仍在运行，延后终态资源回收: %s", job.mid)
            return False
        self._disconnect(job)
        job.data["resume_phase"] = job.phase
        job.phase = phase
        job.cancel_requested = False
        if self.download_manager is not None:
            self.download_manager.release(job.consumer_id)
        job.data.update(error=error, detail=error, speed_bps=0.0, eta_seconds=-1.0)
        if phase == "done":
            job.data.update(stage_progress=1.0, detail=tr("更新完成"), completed_at=monotonic())
        self._emit(job)
        if job.game_id == self._manager.current_game_id:
            if phase == "failed":
                self.failed.emit(job.mid, error)
            elif phase == "canceled":
                self.canceled.emit(job.mid)
            elif phase == "deferred":
                self.deferred.emit(job.mid)
        plan = job.plan
        if plan is not None:
            self._cleanup(plan)
        self._retire_terminal_resources(job)
        self._collect_archives()
        self._pump()
        return True

    def _cleanup(self, plan) -> None:
        """启动后台线程清理安装暂存，并记录线程以供应用退出时等待。"""

        def clean():
            """清理计划资源并记录异常；没有待恢复标记时释放该更新的库操作保护。"""
            try:
                cleanup_update(plan)
            except Exception:
                logger.exception("清理更新暂存失败：%s", plan["work_root"])
            finally:
                from src.utils import fs

                if not fs.exists(Path(plan["work_root"]) / "recovery_required"):
                    self._manager.release_library_operation("update:" + plan["job_id"])

        worker = threading.Thread(target=clean, name="mod-update-cleanup", daemon=True)
        self._cleanup_threads = [thread for thread in self._cleanup_threads if thread.is_alive()]
        self._cleanup_threads.append(worker)
        worker.start()

    def _collect_archives(self) -> None:
        """按配置回收成功安装且不再被活动更新使用的归档，共享下载器负责最终引用检查。"""
        if not self._config.get("gamebanana.delete_archive_after_install", True):
            return
        from src.services.gamebanana.download_store import delete_if_download

        for path, (fid, checksum) in list(self._finished_archives.items()):
            if any(
                job.active
                and (
                    job.file.file_id == fid
                    or job.archive == path
                    or (checksum and (job.file.md5_checksum or "").lower() == checksum)
                )
                for job in self._records.values()
            ):
                continue
            self._finished_archives.pop(path, None)
            if self.download_manager is not None:
                self.download_manager.delete_archive_when_unused(path)
            else:
                delete_if_download(path)

    def _lookup(self, mid) -> _UpdateTask | None:
        """查找当前游戏的活动更新，记录不存在或已结束时返回 None。"""
        job = self._records.get((self._manager.current_game_id, mid))
        return job if job is not None and job.active else None

    def _transfer_users(self, job) -> list[_UpdateTask]:
        """收集仍依赖相同文件且未请求取消的活动更新，供共享传输控制判断。"""
        return [
            other
            for other in self._records.values()
            if other.active and other.file.file_id == job.file.file_id and not other.cancel_requested
        ]

    def _associated_transfer(self, job):
        """查找任务已绑定或尚未绑定的共享下载；已有归档时不再关联传输。"""
        if job.archive is not None or self.download_manager is None:
            return None
        # 排队更新在连接信号前已拥有共享文件，最后一个消费者暂停或停止时也须控制下载。
        return job.download if job.download is not None else self.download_manager.get_task(job.file.file_id)

    def _pause_transfer(self, job) -> None:
        """仅在全部更新消费者均暂停且没有外部消费者时暂停共享下载。"""
        if self._associated_transfer(job) is None:
            return
        users = self._transfer_users(job)
        if all(other.paused for other in users) and not self.download_manager.has_other_consumers(
            job.file.file_id, {other.consumer_id for other in users}
        ):
            self.download_manager.pause(job.file.file_id)

    def _pause_job(self, job) -> bool:
        """暂停运行中的安装进程并按引用关系暂停下载，进程拒绝暂停时返回 False。"""
        if job.process is not None and job.process.running:
            if not job.process.pause():
                return False
        self._pause_transfer(job)
        self._emit(job)
        return True

    def pause(self, mid) -> bool:
        """设置用户暂停并尝试停止任务推进；暂停失败恢复原标记并返回 False。"""
        job = self._lookup(mid)
        if job is None or job.cancel_requested:
            return False
        was_paused = job.user_paused
        job.user_paused = True
        if not self._pause_job(job):
            job.user_paused = was_paused
            return False
        self._pump()
        return True

    def _resume_transfer(self, job) -> None:
        """等待下载协程完成暂停清理后再恢复传输，仍有暂停来源或取消请求时不操作。"""
        if not self._current(job) or job.paused or job.cancel_requested or job.archive is not None:
            return
        state = getattr(job.download, "state", "")
        if state in {"queued", "preparing", "downloading"} and not getattr(job.download, "pause_pending", False):
            # 其他消费者继续传输时，当前更新可独立暂停，无需重新启动下载。
            job.resume_pending = False
            return
        # 协程关闭前可能先发出暂停信号，释放文件和单次执行锁后才能重新调度。
        if getattr(job.download, "is_running", False):
            job.resume_pending = True
            call_later(10, self, lambda: self._resume_transfer(job))
            return
        job.resume_pending = False
        if getattr(job.download, "state", "") == "paused":
            self.download_manager.resume(job.file.file_id)

    def _resume_job(self, job) -> None:
        """在全部暂停解除后提交已准备结果、恢复工作进程或下载，并通过调度器推进排队任务。"""
        if job.paused or job.cancel_requested or not job.active:
            return
        if job.prepared is not None:
            self._process_succeeded(job, job.prepared)
        elif job.process is not None:
            if job.process.running:
                job.process.resume()
            else:
                job.process.start()
        elif job.archive is None and job.download is not None:
            self._resume_transfer(job)
        # 恢复等待下载和等待导入的任务时均通过统一调度器。
        self._emit(job)
        self._pump()

    def resume(self, mid) -> bool:
        """解除用户及传输暂停，确认凭据的持有仍保留；任务失效或正取消时返回 False。"""
        job = self._lookup(mid)
        if job is None or job.cancel_requested:
            return False
        if not job.paused:
            return True
        job.user_paused = False
        job.transfer_paused = False
        self._resume_job(job)
        return True

    def pause_all(self) -> bool:
        """先标记当前游戏全部任务再暂停共享资源，任一失败返回 False 并恢复该任务原标记。"""
        jobs = [
            (job, job.user_paused)
            for mid in self.active_ids()
            if (job := self._lookup(mid)) is not None and not job.cancel_requested
        ]
        for job, _ in jobs:
            job.user_paused = True
        paused = True
        for job, was_paused in jobs:
            if not self._pause_job(job):
                job.user_paused = was_paused
                paused = False
        return paused

    def resume_all(self) -> None:
        """逐个恢复当前游戏的活动任务，仍被确认凭据暂停的任务保持等待。"""
        for mid in self.active_ids():
            self.resume(mid)

    def begin_stop_confirmation(self, mids=None, *, all_games: bool = False) -> StopConfirmation:
        """为筛选任务添加独立暂停令牌；任一暂停失败即撤销本次持有并抛出 OSError。"""
        selected = None if mids is None else set(mids)
        jobs = tuple(
            job
            for job in self._records.values()
            if job.active
            and not job.cancel_requested
            and (all_games or job.game_id == self._manager.current_game_id)
            and (selected is None or job.mid in selected)
        )
        confirmation = StopConfirmation(uuid.uuid4().hex, jobs)
        for job in jobs:
            job.holds.add(confirmation.token)
        try:
            for job in jobs:
                if not self._pause_job(job):
                    raise OSError(tr("无法暂停更新任务，尚未终止"))
        except Exception:
            self.finish_stop_confirmation(confirmation, confirmed=False)
            raise
        return confirmation

    def finish_stop_confirmation(self, confirmation: StopConfirmation, *, confirmed: bool) -> None:
        """幂等结算暂停凭据；确认停止时请求取消，否则解除该凭据后尝试恢复任务。"""
        if confirmation.settled:
            return
        confirmation.settled = True
        for job in confirmation.tasks:
            if confirmed:
                self._cancel_job(job)
            job.holds.discard(confirmation.token)
            if not confirmed:
                self._resume_job(job)

    def _cancel_job(self, job) -> None:
        """请求取消任务，仅在没有其他消费者时取消共享下载，并等待占用资源的执行退出。"""
        if not job.active or job.cancel_requested:
            return
        job.cancel_requested = True
        self._emit(job)
        self._disconnect(job)
        transfer = self._associated_transfer(job)
        if (
            transfer is not None
            and not self._transfer_users(job)
            and not self.download_manager.has_other_consumers(job.file.file_id, {job.consumer_id})
        ):
            job.download = transfer
            self.download_manager.cancel(job.file.file_id)
            if getattr(job.download, "is_running", False):
                self._wait_download_exit(job)
                return
        if job.process is not None and job.process.running:
            job.process.cancel()
        else:
            self._finish(job, "canceled")

    def _wait_download_exit(self, job) -> None:
        """通过 Qt 延迟回调等待下载释放资源，退出后才将仍有效的取消任务标为终态。"""
        if self._records.get(job.key) is not job or not job.active or not job.cancel_requested:
            return
        if getattr(job.download, "is_running", False):
            call_later(10, self, lambda: self._wait_download_exit(job))
            return
        self._finish(job, "canceled")

    def cancel(self, mid) -> None:
        """请求取消当前游戏的指定活动任务，缺失或已结束记录不操作。"""
        job = self._lookup(mid)
        if job is not None:
            self._cancel_job(job)

    def shutdown(self) -> bool:
        """阻止新调度并停止全部任务，限时等待下载与清理线程；执行未退出时返回 False 供再次关闭。"""
        self._closing = True
        stopped = True
        for job in list(self._records.values()):
            if job.active:
                self._cancel_job(job)
            process = job.process
            if process is not None:
                process.shutdown()
                if process.running:
                    stopped = False
                elif job.active:
                    self._finish(job, "canceled")
            elif job.active and job.download is not None:
                # 仅应用关闭可同步等待；正常停止操作在网络清理期间保持响应。
                job.download.wait_finished(timeout=2)
                if getattr(job.download, "is_running", False):
                    stopped = False
                else:
                    self._finish(job, "canceled")
        for thread in self._cleanup_threads:
            thread.join(timeout=2)
        if not stopped:
            # 操作系统释放全部工作进程前保留事件循环和暂存原件，后续关闭请求可继续完成清理。
            self._closing = False
        return stopped
