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

    def _current_mod_ids(self) -> set[str]:
        """从索引取得当前游戏的 Mod ID；未选择游戏时返回空集合。"""
        game_id = self._mm.current_game_id
        return self._mm._index.get_mod_ids(game_id) if game_id else set()

    def load_status(self) -> None:
        """切换状态所属游戏并读取仍在库中的更新；读取失败仅记录日志并通知界面刷新。"""
        # 切走时保留已经收到的部分结果；此时 ModManager 已指向新游戏，不能再借它选写入目录。
        if self._check_in_progress:
            self._save_status()
        self._status_game_id = self._mm.current_game_id
        self._status.clear()
        self._failures.clear()
        if self._check_preparation is not None and not self._is_current_check():
            self._on_finished({"found": 0, "up_to_date": 0, "failed": 0, "cancelled": True})
        if not self._mm.current_game_id:
            self.status_changed.emit()
            return
        p = self._status_path()
        if not p.exists():
            self.status_changed.emit()
            return
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            current_ids = self._current_mod_ids()
            for mid, d in (raw or {}).items():
                if mid not in current_ids:
                    # get_mod 按 ID 跨游戏查询；只接收当前游戏仍在库内的条目。
                    continue
                self._status[mid] = UpdateInfo.from_dict(d)
        except Exception as e:
            logger.warning("读取 update_status.json 失败: %s", e)
        self.status_changed.emit()

    def _save_status(self) -> None:
        """将更新结果写入状态所属游戏的文件，写入失败记录日志而不向调用方抛出。"""
        if not self._status_game_id:
            return
        p = self._status_path(self._status_game_id)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {mid: info.to_dict() for mid, info in self._status.items()}
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("写 update_status.json 失败: %s", e)

    # 收集候选
    def _collect_jobs(self, *, force: bool):
        """收集符合本地检查策略的条目及忽略文件 ID，返回检查任务列表和忽略映射。"""
        jobs: list[tuple[str, GbInstallMetadata]] = []
        ignored_map: dict[str, set[int]] = {}
        for mid, meta, ignored in self._iter_candidates(force=force, game_id=self._mm.current_game_id):
            if meta is not None:
                jobs.append((mid, meta))
                ignored_map[mid] = ignored
        return jobs, ignored_map

    def _iter_candidates(self, *, force: bool, game_id: str, only_ids=None):
        """每条本地记录都让出一次执行，包括跳过的记录，以支持分批扫描。"""
        if not game_id:
            return
        enabled = self._config.get(f"{_GB}.enabled", True) and self._config.get(f"{_CFG}.enabled", True)
        min_hours = int(self._config.get(f"{_CFG}.min_recheck_hours", 24)) if enabled else 0
        now = datetime.now()
        # 候选只需要元数据，避免构造全部 Mod 并解析路径。
        # 后台检查不需要显示顺序，也不执行阻塞首条结果的全库排序。
        with closing(self._mm._index.iter_mod_info(game_id, ordered=False)) as rows:
            for mid, info in rows:
                meta, ignored = None, set()
                if enabled and (only_ids is None or mid in only_ids):
                    gb = (info.get("custom_data") or {}).get(_GB) or {}
                    if not gb.get("ignored"):
                        meta = self._resolve_update_source(info, gb)
                        last = gb.get("last_checked_iso") or ""
                        if meta is not None and not force and last and _hours_since(last, now) < min_hours:
                            meta = None
                        if meta is not None:
                            ignored = set(int(x) for x in (gb.get("ignored_file_ids") or []))
                yield mid, meta, ignored

    def _resolve_update_source(self, info: dict, gb: dict) -> GbInstallMetadata | None:
        """解析 Mod 的 GameBanana 更新来源。

        优先采用包含文件时间和指纹的结构化元数据；缺失时从来源 URL 解析在线编号，
        并标记安装基线未知。没有有效来源时返回 None，不参与更新检查。
        """
        if gb.get("mod_id"):
            return GbInstallMetadata.from_dict(gb)
        parsed = parse_gb_url(info.get("url") or "")
        if parsed is None:
            return None
        model, mod_id = parsed
        if model != GbModel.MOD:  # 仅检查 Mod 类型的更新。
            return None
        return GbInstallMetadata(
            mod_id=mod_id,
            file_id=0,
            file_md5="",
            file_ts=0,
            page_url=info["url"],
            version=(info.get("version") or None),
            submitter_id=0,
            submitter_name=(info.get("author") or ""),
            last_checked_iso=(gb.get("last_checked_iso") or ""),
        )

    def check_updates(self, *, force: bool, only_failed: bool = False) -> bool:
        """同步准备候选，供需要最终数量的调用方使用。"""
        if not self._begin_check(force=force, only_failed=only_failed):
            return False
        return self._advance_check_preparation(sliced=False)

    def schedule_check_updates(self, *, force: bool, only_failed: bool = False) -> bool:
        """预留检查任务，以 4 毫秒时间片准备本地候选。"""
        token = self._begin_check(force=force, only_failed=only_failed)
        if token is None:
            return False
        self.status_changed.emit()
        if self._check_token is token:
            self._preparation_timer.start(0)
        return True

    def _begin_check(self, *, force: bool, only_failed: bool):
        """保存检查前状态并创建本轮身份令牌及候选生成器；已有检查时返回 None。"""
        if self._check_in_progress:
            return None
        self._status_before_check = dict(self._status)
        self._failures_before_check = dict(self._failures)
        self._check_jobs = {}
        self._check_outcomes = {}
        self._check_superseded = set()
        self._pending_last_checked = set()
        self._check_game_id = self._mm.current_game_id
        self._check_in_progress = True
        token = self._check_token = object()
        now = time.monotonic()
        self._snapshot_game_id = self._mm.current_game_id
        self._check_snapshot = {
            "checking": True,
            "phase": "preparing",
            "started_at": now,
            "last_progress_at": now,
            "finished_at": 0.0,
            "total": 0,
            "done": 0,
            "found": 0,
            "failed": 0,
            "retry_only": only_failed,
        }
        self._check_preparation = self._prepare_candidates(force=force, only_failed=only_failed)
        return token

    def _prepare_candidates(self, *, force: bool, only_failed: bool):
        """逐条收集候选并清理失效结果，每步让出控制权，结束时返回忽略映射及强制标记。"""
        current_ids: set[str] = set()
        ignored_map: dict[str, set[int]] = {}
        # 重试范围固定为本次检查，跳过本地节流；原强制标记仍控制网络缓存与共享限制。
        with closing(
            self._iter_candidates(
                force=force or only_failed,
                game_id=self._check_game_id,
                only_ids=self._failures_before_check if only_failed else None,
            )
        ) as candidates:
            for mid, meta, ignored in candidates:
                current_ids.add(mid)
                if meta is not None and mid not in self._check_superseded:
                    self._check_jobs[mid] = meta
                    ignored_map[mid] = ignored
                yield
        # 不重复扫描整个库；清理已保存结果时同样分批让出执行。
        for state in (self._status, self._failures, self._status_before_check, self._failures_before_check):
            for mid in tuple(state):
                if mid not in current_ids:
                    state.pop(mid, None)
                yield
        if only_failed:
            for mid in tuple(self._check_jobs):
                self._failures.pop(mid, None)
                yield
        else:
            self._failures.clear()
        return ignored_map, force

    def _prepare_check_slice(self) -> None:
        """由 Qt 单次定时器驱动一段有时间预算的候选准备工作。"""
        self._advance_check_preparation(sliced=True)

    def _advance_check_preparation(self, *, sliced: bool) -> bool:
        """推进候选生成器并按需续约定时器；游戏失效或准备异常时取消收尾。"""
        preparation, token = self._check_preparation, self._check_token
        if preparation is None:
            return False
        if not self._is_current_check():
            self._on_finished({"found": 0, "up_to_date": 0, "failed": 0, "cancelled": True})
            return False
        deadline = time.perf_counter() + _PREPARATION_SLICE_SECONDS if sliced else None
        try:
            while True:
                try:
                    next(preparation)
                except StopIteration as ready:
                    ignored_map, force = ready.value
                    break
                if deadline is not None and time.perf_counter() >= deadline:
                    self._preparation_timer.start(0)
                    return True
        except Exception as exc:
            logger.warning("准备更新检测失败: %s", exc)
            self._on_finished(
                {"found": 0, "up_to_date": 0, "failed": 0, "cancelled": True, "error": str(exc) or type(exc).__name__}
            )
            return False
        self._check_preparation = None
        return self._start_prepared_check(token, ignored_map, force=force)

    def _start_prepared_check(self, token, ignored_map, *, force: bool) -> bool:
        """发布开始信号后复核令牌，启动网络检查；空任务立即完成，启动失败逐项登记错误。"""
        jobs = list(self._check_jobs.items())
        self._check_snapshot.update(phase="connecting", total=len(jobs), last_progress_at=time.monotonic())
        self.check_started.emit(len(jobs))
        # check_started 的同步接收者也可能取消或启动了下一轮。
        if self._check_token is not token:
            return True
        if not self._is_current_check():
            self._on_finished({"found": 0, "up_to_date": 0, "failed": 0, "cancelled": True})
            return True
        if not jobs:
            self._on_finished({"found": 0, "up_to_date": 0, "failed": 0})
            return True
        try:
            started = self._checker.start(jobs, ignored_map, force=force)
            error = tr("更新检测未能启动")
        except Exception as exc:
            started = False
            error = str(exc) or type(exc).__name__
        if started is False and self._check_token is token:
            for mid, _ in jobs:
                self._on_failed(mid, error)
            self._on_finished({"found": 0, "up_to_date": 0, "failed": len(jobs)})
            return False
        return True

    def cancel_check(self) -> bool:
        """取消当前检查并立即收尾尚未提交给检查器的轮次；没有活动检查时返回 False。"""
        if not self._check_in_progress:
            return False
        token = self._check_token
        self._checker.cancel()
        if self._check_token is token:
            # started 信号期间取消，或尚未进入 checker 的空轮次也要立即收尾。
            counts = {
                kind: list(self._check_outcomes.values()).count(kind) for kind in ("found", "up_to_date", "failed")
            }
            self._on_finished({**counts, "cancelled": True})
        return True

    # checker 信号处理
    def _is_current_check(self) -> bool:
        """判断活动检查是否仍属于当前游戏，作为结果写入和界面通知的前置条件。"""
        return self._check_in_progress and self._check_game_id == self._mm.current_game_id

    def _on_check_progress(self, done: int, total: int) -> None:
        """仅将当前游戏的检查进度写入快照并转发给界面。"""
        if self._is_current_check():
            self._check_snapshot.update(
                phase="checking",
                done=done,
                total=total,
                last_progress_at=time.monotonic(),
            )
            self.progress.emit(done, total)

    def _record_check_outcome(self, mid: str, outcome: str) -> None:
        """替换单个 Mod 的检查结论，并相应修正发现更新与失败计数。"""
        previous = self._check_outcomes.get(mid)
        if previous in {"found", "failed"}:
            self._check_snapshot[previous] -= 1
        self._check_outcomes[mid] = outcome
        if outcome in {"found", "failed"}:
            self._check_snapshot[outcome] += 1

    def _check_mod(self, mid: str):
        """校验结果属于本轮且未被用户操作覆盖，再按 ID 读取条目；失效时返回 None。"""
        if not self._is_current_check() or mid not in self._check_jobs or mid in self._check_superseded:
            return None
        # jobs 已按发起游戏收集；每条结果只查对应 ID，不能反复载入整个 Mod 库。
        return self._mm.get_mod(mid)

    def _preserve_user_change(self, mid: str) -> None:
        """取消只撤销本轮检测，不能撤销期间已完成的更新、忽略或撤销忽略。"""
        if not self._is_current_check():
            return
        self._check_superseded.add(mid)
        if self._check_preparation is not None:
            self._check_jobs.pop(mid, None)
        self._pending_last_checked.discard(mid)
        if mid in self._status:
            self._status_before_check[mid] = self._status[mid]
        else:
            self._status_before_check.pop(mid, None)
        if mid in self._failures:
            self._failures_before_check[mid] = self._failures[mid]
        else:
            self._failures_before_check.pop(mid, None)

    def _write_last_checked(self, mid: str) -> None:
        """为仍存在的 Mod 写入本地检查时间并交由管理器持久化。"""
        mod = self._mm.get_mod(mid)
        if not mod:
            return
        gb = mod.info.custom_data.setdefault(_GB, {})
        gb["last_checked_iso"] = datetime.now().isoformat()
        self._mm.update_mod(mod)

    def _on_update_found(self, mid: str, info: UpdateInfo) -> None:
        """接收有效更新结果，补充本地展示信息并暂存检查时间，随后通知状态变更。"""
        mod = self._check_mod(mid)
        if mod is None:
            return
        info.local_name = mod.display_name
        info.local_preview = mod.primary_preview or ""
        self._status[mid] = info
        self._failures.pop(mid, None)
        self._pending_last_checked.add(mid)
        self._record_check_outcome(mid, "found")
        self.status_changed.emit()

    def _on_up_to_date(self, mid: str) -> None:
        """清除有效条目的更新和失败状态，并将检查时间留待整轮提交。"""
        if self._check_mod(mid) is None:
            return
        self._status.pop(mid, None)
        self._failures.pop(mid, None)
        self._pending_last_checked.add(mid)
        self._record_check_outcome(mid, "up_to_date")
        self.status_changed.emit()

    def _on_failed(self, mid: str, err: str) -> None:
        """登记有效条目的失败原因，不更新检查时间，以便后续重新尝试。"""
        if self._check_mod(mid) is None:
            return
        # 失败不写 last_checked（下次仍会重试）
        self._failures[mid] = err
        self._pending_last_checked.discard(mid)
        self._record_check_outcome(mid, "failed")
        self.status_changed.emit()

    def _on_finished(self, counts: dict) -> None:
        """关闭准备生成器并结束本轮；取消时恢复检查前结果，跨游戏结果不发布到当前界面。"""
        if not self._check_in_progress:
            return
        self._preparation_timer.stop()
        if self._check_preparation is not None:
            self._check_preparation.close()
            self._check_preparation = None
        current = self._is_current_check()
        cancelled = bool(counts.get("cancelled"))
        if current:
            current_ids = self._current_mod_ids()
            if cancelled:
                self._status = {mid: info for mid, info in self._status_before_check.items() if mid in current_ids}
                self._failures = {
                    mid: error for mid, error in self._failures_before_check.items() if mid in current_ids
                }
            else:
                self._status = {mid: info for mid, info in self._status.items() if mid in current_ids}
                self._failures = {mid: error for mid, error in self._failures.items() if mid in current_ids}
                for mid in self._pending_last_checked & current_ids:
                    self._write_last_checked(mid)
            self._check_snapshot.update(
                checking=False,
                phase=(
                    "failed"
                    if counts.get("error")
                    else "cancelled"
                    if cancelled
                    else "partial"
                    if counts.get("failed")
                    else "done"
                ),
                done=sum(int(counts.get(key, 0)) for key in ("found", "up_to_date", "failed")),
                found=int(counts.get("found", 0)),
                failed=int(counts.get("failed", 0)),
                finished_at=time.monotonic(),
            )
            if counts.get("error"):
                self._check_snapshot["error"] = str(counts["error"])
        else:
            self._snapshot_game_id = self._mm.current_game_id
            self._check_snapshot = _idle_check_snapshot()
        self._check_in_progress = False
        self._check_game_id = None
        self._check_token = None
        self._check_jobs.clear()
        self._check_outcomes.clear()
        self._check_superseded.clear()
        self._pending_last_checked.clear()
        self._status_before_check.clear()
        self._failures_before_check.clear()
        if not current:
            # 只通知忙碌状态结束，不把旧游戏的统计结果带入当前游戏。
            self.status_changed.emit()
            return
        self._save_status()
        self.status_changed.emit()
        self.check_finished.emit(counts)

    # 忽略
    def ignore_version(self, local_mod_id: str) -> _IgnoreUndo | None:
        """将当前推荐文件加入忽略列表并返回撤销凭据；条目或更新不存在时返回 None。"""
        info = self._status.get(local_mod_id)
        mod = self._mm.get_mod(local_mod_id)
        if not mod or not info:
            return
        gb = mod.info.custom_data.setdefault(_GB, {})
        ids = set(int(x) for x in (gb.get("ignored_file_ids") or []))
        ids.add(int(info.latest_file.file_id))
        return self._apply_ignore(mod, {"ignored_file_ids": sorted(ids)})

    def ignore_mod(self, local_mod_id: str) -> _IgnoreUndo | None:
        """持久化整个 Mod 的更新忽略标记并返回撤销凭据，条目不存在时不操作。"""
        mod = self._mm.get_mod(local_mod_id)
        if not mod:
            return
        return self._apply_ignore(mod, {"ignored": True})

    def _apply_ignore(self, mod, changes: dict) -> _IgnoreUndo | None:
        """仅修改当前游戏条目，保存忽略字段后移除更新提示，并返回带来源校验信息的撤销凭据。"""
        game_id = self._mm.current_game_id
        if not game_id or mod.game_id != game_id:
            return None
        gb = mod.info.custom_data.setdefault(_GB, {})
        token = _IgnoreUndo(
            game_id,
            mod.mod_id,
            {key: deepcopy(gb[key]) for key in changes if key in gb},
            deepcopy(changes),
            {key: gb.get(key) for key in ("mod_id", "file_id", "file_md5", "file_ts")},
            deepcopy(self._status.get(mod.mod_id)),
        )
        gb.update(changes)
        if not self._mm.update_mod(mod):
            return None
        self._status.pop(mod.mod_id, None)
        self._preserve_user_change(mod.mod_id)
        self._save_status()
        self.status_changed.emit()
        return token

    def undo_ignore(self, token: _IgnoreUndo) -> bool:
        """恢复本次修改的字段，保留此前忽略的版本和后续其他元数据修改。"""
        if not isinstance(token, _IgnoreUndo) or token.game_id != self._mm.current_game_id:
            return False
        mod = self._mm.get_mod(token.mod_id)
        if mod is None or mod.game_id != token.game_id:
            return False
        gb = mod.info.custom_data.setdefault(_GB, {})
        if any(gb.get(key) != value for key, value in (token.after | token.source).items()):
            return False
        for key in token.after:
            if key in token.before:
                gb[key] = deepcopy(token.before[key])
            else:
                gb.pop(key, None)
        if not self._mm.update_mod(mod):
            return False
        if token.info is not None:
            self._status.setdefault(token.mod_id, deepcopy(token.info))
        self._preserve_user_change(token.mod_id)
        self._save_status()
        self.status_changed.emit()
        return True

    # 一键更新 / 回滚
    def apply_update(self, local_mod_id: str, chosen_file=None) -> None:
        """选定显式或默认更新文件并入队；缺少本地条目或可用文件时通过失败信号报告。"""
        mod = self._mm.get_mod(local_mod_id)
        if mod is None:
            self.update_failed.emit(local_mod_id, tr("找不到本地 Mod"))
            return
        info = self._status.get(local_mod_id)
        if chosen_file is None and info is not None:
            chosen_file = (
                pick_default_file(info.candidate_files, mod.folder_name)
                if len(info.candidate_files) > 1
                else info.latest_file
            )
        if chosen_file is None:
            self.update_failed.emit(local_mod_id, tr("没有可用的更新文件"))
            return
        gb = dict((mod.info.custom_data or {}).get(_GB) or {})
        self._update_tasks.start(local_mod_id, chosen_file, self._update_metadata(chosen_file, gb), info=info)

    @staticmethod
    def _update_metadata(file, gb: dict) -> GbInstallMetadata:
        """由选定文件和原安装来源组装新的安装元数据，保留页面及作者信息。"""
        return GbInstallMetadata(
            mod_id=int(gb.get("mod_id") or 0),
            file_id=int(file.file_id),
            file_md5=file.md5_checksum,
            file_ts=int(file.date_added_ts),
            page_url=gb.get("page_url") or "",
            version=(file.version or None),
            submitter_id=int(gb.get("submitter_id") or 0),
            submitter_name=gb.get("submitter_name") or "",
            last_checked_iso=datetime.now().isoformat(),
        )

    def _finish_apply(self, mid: str, archive_path, chosen_file, gb: dict, *, game_id: str) -> None:
        """将已下载归档连同原游戏身份提交安装队列，沿用对应条目的更新信息。"""
        self._update_tasks.start(
            mid,
            chosen_file,
            self._update_metadata(chosen_file, gb),
            info=self._status.get(mid),
            archive=archive_path,
            game_id=game_id,
        )

    def _on_task_applied(self, mid: str, mod) -> None:
        """安装提交后移除过期检查状态，保存结果并发布库更新及界面完成通知。"""
        self._status.pop(mid, None)
        self._failures.pop(mid, None)
        self._preserve_user_change(mid)
        self._save_status()
        self._mm._emit("mod_updated", mod_id=mid, game_id=self._mm._index.get_mod_game_id(mid) or "")
        self.update_applied.emit(mid, mod)
        self.status_changed.emit()

    def update_task_snapshots(self) -> dict[str, dict]:
        """返回当前游戏安装任务的展示快照，包括已结束任务。"""
        return self._update_tasks.snapshots()
