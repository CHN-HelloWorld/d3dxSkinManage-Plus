"""管理 GameBanana 下载、完整性校验、并发队列与已完成任务记录。

任务按 queued、preparing、downloading、done/failed/canceled 转换状态。
暂停通过取消正在等待的协程及时中断；恢复时按临时文件长度请求 Range，
服务端不接受分段响应时重新下载。

已完成任务保存在 data/downloads/queue.json。损坏记录保留备份后重新初始化。
未完成任务只在当前进程中支持续传，启动时清理上次遗留的部分下载文件。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from PySide6.QtCore import QObject, Signal

from src.services.gamebanana import download_store
from src.services.gamebanana.models import GbModFile, GbModProfile
from src.utils.logger import logger
from src.utils.paths import get_gb_download_dir, resolve_stored_path, to_stored_path

# 下载队列版本标记用于诊断；读取时按字段校验，缺失字段使用默认值。
QUEUE_SCHEMA_VERSION = 1

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(name: str) -> str:
    """替换非法文件名字符并限制长度，清理后为空时使用默认文件名。"""
    return _INVALID.sub("_", name).strip().rstrip(".")[:160] or "file"


# 任务状态决定可用的界面操作。
# queued：等待调度。
# preparing：等待重定向或响应头，尚未接收文件数据。
# downloading：接收文件数据。
# paused：保留分片，恢复时通过 Range 请求续传。
# done：下载完成且校验通过。
# failed：HTTP、网络或完整性校验失败。
# canceled：用户取消。
TASK_STATES = ("queued", "preparing", "downloading", "paused", "done", "failed", "canceled")
TASK_TERMINAL_STATES = ("done", "failed", "canceled")
# 具有活动协程的状态，暂停或取消时须中断协程。
TASK_RUNNING_STATES = ("preparing", "downloading")

_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.I)


class GbDownloadTask(QObject):
    """维护单个在线文件的下载状态、分片、进度及中断意图，通过 Qt 信号发布结果。"""

    progress = Signal("qint64", "qint64")
    state_changed = Signal(str)
    finished = Signal(Path)
    failed = Signal(str)

    def __init__(
        self,
        profile: GbModProfile,
        file: GbModFile,
        archive_path: Path,
        *,
        game_id: str,
    ) -> None:
        """固定下载来源、归档路径与所属游戏，初始化单次运行保护、取消状态及速度采样。"""
        super().__init__()
        self.profile = profile
        self.file = file
        self.archive_path = archive_path
        # 任务所属的本地游戏编号，必填，用于下载管理的精确过滤。
        self.game_id = game_id
        self._state = "queued"
        self._cancelled = False
        self._pause_requested = False
        # 记录中断意图，由 CancelledError 处理分支区分暂停与取消。
        self._interrupt: str | None = None
        # 管理器记录协程所属的事件循环及任务对象，
        # 以便等待响应头期间也能直接中断 await。
        self._loop: asyncio.AbstractEventLoop | None = None
        self._aio_task: asyncio.Task | None = None
        # 记录文件清理请求，由协程退出时执行。
        self._cleanup_part = False
        self._cleanup_product = False
        self._run_lock = threading.Lock()
        self._run_started = False
        self._run_finished = threading.Event()
        self._last_downloaded: int = 0
        self._last_total: int = 0
        # 按时间窗口估算字节速度及剩余秒数，-1 表示剩余时间未知。
        self._speed_bps: float = 0.0
        self._eta_seconds: float = -1.0
        self._spd_anchor_t: float = 0.0
        self._spd_anchor_bytes: int = 0

    @property
    def state(self) -> str:
        """返回任务当前的排队、运行、暂停或终态标识。"""
        return self._state

    @property
    def pause_pending(self) -> bool:
        """是否已请求暂停（协程可能仍在关闭当前连接）。"""
        return self._interrupt == "paused" or self._pause_requested

    @property
    def last_progress(self) -> tuple[int, int]:
        """返回最近一次记录的已下载字节数与总字节数。"""
        return (self._last_downloaded, self._last_total)

    @property
    def speed_bps(self) -> float:
        """实时下载速度（字节/秒）；非下载态为 0。"""
        return self._speed_bps

    @property
    def eta_seconds(self) -> float:
        """预计剩余秒；未知 / 非下载态为 -1。"""
        return self._eta_seconds

    def _set_state(self, s: str) -> None:
        """仅在状态变化时发信号，进入下载阶段重置采样锚点，离开时清空速度与剩余时间。"""
        if self._state == s:
            return
        self._state = s
        # 切状态时维护速度采样：进入 downloading 重置锚点重新计速；离开则清零
        # （暂停 / 完成 / 失败时不再显示残留速度与剩余时间）。
        if s == "downloading":
            self._spd_anchor_t = 0.0
            self._spd_anchor_bytes = 0
        else:
            self._speed_bps = 0.0
            self._eta_seconds = -1.0
            self._spd_anchor_t = 0.0
        self.state_changed.emit(s)

    def _emit_progress(self, downloaded: int, total: int) -> None:
        """保存最新字节进度、更新速度估计，并向界面发出进度信号。"""
        self._last_downloaded = downloaded
        self._last_total = total
        self._update_speed(downloaded, total)
        self.progress.emit(downloaded, total)

    def _update_speed(self, downloaded: int, total: int) -> None:
        """按时间窗口(≥0.4s)采样估算速度，EMA 平滑后推算剩余时间。"""
        now = time.monotonic()
        if self._spd_anchor_t <= 0.0:
            self._spd_anchor_t = now
            self._spd_anchor_bytes = downloaded
            return
        dt = now - self._spd_anchor_t
        if dt < 0.4:
            return
        inst = (downloaded - self._spd_anchor_bytes) / dt
        if inst < 0:
            inst = 0.0
        # EMA 平滑：新样本占 40%，抑制网络抖动带来的数字跳变。
        self._speed_bps = inst if self._speed_bps <= 0 else self._speed_bps * 0.6 + inst * 0.4
        self._spd_anchor_t = now
        self._spd_anchor_bytes = downloaded
        remain = total - downloaded
        self._eta_seconds = (remain / self._speed_bps) if (self._speed_bps > 1 and remain > 0) else -1.0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """由管理器在调度前记录下载协程所属的事件循环。"""
        self._loop = loop

    def _abort_running(self) -> bool:
        """请求取消正在等待的协程，返回是否已发送中断请求。

        准备期间可能尚未进入数据接收循环，因此不能仅依赖循环内的标志检查。
        """
        loop, aio = self._loop, self._aio_task
        if loop is None or aio is None or aio.done():
            return False
        try:
            loop.call_soon_threadsafe(aio.cancel)
            return True
        except RuntimeError:
            return False

    def cancel(self) -> None:
        """取消任务，并按协程状态清理下载分片。

        排队或暂停中的任务没有活动协程，可立即修改状态并删除分片。
        活动任务记录取消意图后中断 await，由协程退出时统一清理；
        尚未开始执行的协程在入口检查取消标志。
        """
        self._cancelled = True
        if self._state in ("queued", "paused"):
            self._part_path().unlink(missing_ok=True)
            self._set_state("canceled")
            return
        if self._state in TASK_RUNNING_STATES:
            self._interrupt = "canceled"
            self._abort_running()

    def _part_path(self) -> Path:
        """下载分片路径；完成校验后重命名为正式归档。"""
        return self.archive_path.with_suffix(self.archive_path.suffix + ".part")

    def pause(self) -> None:
        """请求暂停。

        准备和下载阶段都中断当前 await；即使服务器迟迟不返回下一块数据也能暂停。
        已写入的分片随文件上下文关闭后保留；queued 时直接置 paused。
        """
        if self._state == "downloading":
            self._pause_requested = True
            self._interrupt = "paused"
            self._abort_running()
        elif self._state == "preparing":
            self._interrupt = "paused"
            self._abort_running()
        elif self._state == "queued":
            self._set_state("paused")

    def resume(self) -> None:
        """将暂停、失败或取消的任务重新入队，由管理器调度执行。"""
        if self._state not in ("paused", "failed", "canceled"):
            return
        self._cancelled = False
        self._pause_requested = False
        self._interrupt = None
        with self._run_lock:
            self._run_started = False
            self._run_finished.clear()
        # 根据分片实际长度恢复进度，使界面与续传起点一致。
        # 分片已被取消或校验失败流程删除时从零开始。
        try:
            self._last_downloaded = self._part_path().stat().st_size
        except OSError:
            self._last_downloaded = 0
        if self._last_total <= 0:
            self._last_total = self._expected_total()
        self._set_state("queued")

    def wait_finished(self, timeout: float = 3.0) -> bool:
        """等待下载协程退出，仅供关闭流程等后台等待场景使用。

        界面线程应请求异步清理，由协程完成文件删除；从未启动的任务视为已结束。
        """
        with self._run_lock:
            if not self._run_started:
                return True
        return self._run_finished.wait(timeout)

    @property
    def is_running(self) -> bool:
        """判断协程是否仍在执行或清理资源。"""
        if self._state in TASK_RUNNING_STATES:
            return True
        with self._run_lock:
            started = self._run_started
        return started and not self._run_finished.is_set()

    def request_cleanup(self, *, delete_product: bool) -> None:
        """请求协程退出时清理分片，并可同时删除完成文件。

        清理由文件所有者执行，避免主线程等待或在协程仍写入时提前删除。
        """
        self._cleanup_part = True
        self._cleanup_product = delete_product

    def _do_cleanup_files(self) -> None:
        """执行文件清理请求，记录失败但不打断协程退出。"""
        targets = [self._part_path()]
        if self._cleanup_product:
            targets.append(self.archive_path)
        for p in targets:
            try:
                p.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("清理下载文件失败：%s（%s）", p, e)
