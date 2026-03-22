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

    def _expected_total(self) -> int:
        """返回已知文件总长度，优先使用接口元数据，其次使用之前收到的总长度。

        续传响应的 Content-Length 仅表示剩余内容长度，不能直接作为完整文件长度。
        """
        try:
            size = int(getattr(self.file, "size_bytes", 0) or 0)
        except (TypeError, ValueError):
            size = 0
        return size if size > 0 else max(self._last_total, 0)

    async def _prime_resume(self, tmp: Path) -> tuple[int, hashlib._Hash]:
        """检查分片并返回续传偏移与对应的 MD5 计算状态。

        续传前重新读取已有分片以恢复增量哈希；分片为空、长度无效或无法读取时重新下载。
        分块读取之间让出事件循环，保证处理大型分片时仍能响应暂停和取消。
        """
        fresh = hashlib.md5()
        try:
            size = tmp.stat().st_size
        except OSError:
            return 0, fresh
        if size <= 0:
            tmp.unlink(missing_ok=True)
            return 0, fresh
        total = self._expected_total()
        if total and size >= total:
            # 分片已达到或超过总长度但尚未通过校验，不能作为续传基础。
            logger.info("残留分片(%s B)不小于总长(%s B)，改为整体重下", size, total)
            tmp.unlink(missing_ok=True)
            return 0, fresh
        primed = hashlib.md5()
        try:
            with open(tmp, "rb") as f:
                while True:
                    block = f.read(1 << 20)
                    if not block:
                        break
                    primed.update(block)
                    await asyncio.sleep(0)
        except OSError as e:
            logger.warning("重算残留分片的 MD5 失败，改为整体重下：%s", e)
            tmp.unlink(missing_ok=True)
            return 0, fresh
        return size, primed

    def _resolve_range(self, resp: httpx.Response, want_from: int) -> tuple[int, int, bool]:
        """根据响应头判断续传是否有效，返回偏移、总长度及是否丢弃分片。

        仅接受状态码 206、匹配的 Content-Range 起点及一致的文件总长度。
        服务端未按请求提供有效分段响应时重新下载，完成后仍执行 MD5 校验。
        """
        length = int(resp.headers.get("content-length", 0) or 0)
        known = self._expected_total()
        if want_from <= 0:
            return 0, length or known, False
        if resp.status_code != 206:
            logger.info("服务端未接受断点续传（HTTP %s），改为整体重下", resp.status_code)
            return 0, length or known, True
        m = _CONTENT_RANGE.search(resp.headers.get("content-range", "") or "")
        if m is None:
            logger.warning("206 响应缺少可解析的 Content-Range，改为整体重下")
            return 0, length or known, True
        start = int(m.group(1))
        full = 0 if m.group(3) == "*" else int(m.group(3))
        if start != want_from:
            logger.warning("Content-Range 起点 %s 与请求 %s 不符，改为整体重下", start, want_from)
            return 0, full or known, True
        total = full or (want_from + length if length else 0) or known
        if known and total and total != known:
            logger.warning("续传总长 %s 与 API 记录 %s 不符，改为整体重下", total, known)
            return 0, known, True
        return want_from, total, False

    async def _run_async(self) -> None:
        """保证同轮任务只执行一次下载，处理续传、中断和摘要校验，成功后将分片发布为完整归档。"""
        with self._run_lock:
            if self._run_started:
                already_running = True
            else:
                self._run_started = True
                already_running = False
        if already_running:
            while not self._run_finished.is_set():
                await asyncio.sleep(0.01)
            return
        # 通过单次运行检查后才登记活动任务，避免重复协程覆盖任务引用，
        # 使取消请求始终指向实际下载的协程。
        self._aio_task = asyncio.current_task()
        # 处理调度后、协程开始前收到的取消或暂停请求。
        if self._cancelled:
            self._part_path().unlink(missing_ok=True)
            self._set_state("canceled")
            self._aio_task = None
            self._run_finished.set()
            return
        if self._state in ("paused", "canceled"):
            self._aio_task = None
            self._run_finished.set()
            return
        self._interrupt = None
        self._set_state("preparing")
        tmp = self._part_path()
        try:
            from src.utils.proxy import get_active_proxy_url

            proxy = get_active_proxy_url()
            self.archive_path.parent.mkdir(parents=True, exist_ok=True)
            # 续传偏移及已处理分片内容的哈希状态；偏移为零时重新下载。
            resume_from, md5 = await self._prime_resume(tmp)
            headers = {"Range": f"bytes={resume_from}-"} if resume_from > 0 else None
            interrupted: str | None = None  # "canceled" | "paused"——退出循环后据此收尾
            async with httpx.AsyncClient(timeout=120, follow_redirects=True, proxy=proxy) as client:
                async with client.stream(
                    "GET",
                    self.file.download_url,
                    headers=headers,
                ) as resp:
                    resp.raise_for_status()
                    start, total, discard = self._resolve_range(resp, resume_from)
                    if discard:
                        md5 = hashlib.md5()
                    downloaded = start
                    # 续传时追加写入，否则截断无效分片后重新写入。
                    self._set_state("downloading")
                    self._emit_progress(downloaded, total)
                    with open(tmp, "ab" if start > 0 else "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                            if self._cancelled:
                                interrupted = "canceled"
                                break
                            if self._pause_requested:
                                self._pause_requested = False
                                interrupted = "paused"
                                break
                            f.write(chunk)
                            md5.update(chunk)
                            downloaded += len(chunk)
                            self._emit_progress(downloaded, total)
            # 退出文件上下文后再删除，避免 Windows 文件占用错误。
            if interrupted == "canceled":
                # 取消时删除未完成分片，避免后续将其作为可安装文件使用。
                tmp.unlink(missing_ok=True)
                self._set_state("canceled")
                return
            if interrupted == "paused":
                # 暂停保留分片，仅在本次运行中支持续传；启动扫描会清理之前的分片。
                self._set_state("paused")
                return
            actual = md5.hexdigest()
            expected = (self.file.md5_checksum or "").lower()
            if expected and actual != expected:
                # 校验失败后删除无效分片，防止下一次继续在损坏内容上续传。
                tmp.unlink(missing_ok=True)
                self._set_state("failed")
                self.failed.emit(f"MD5 mismatch: {actual} != {expected}")
                return
            tmp.replace(self.archive_path)
            self._set_state("done")
            self.finished.emit(self.archive_path)
        except asyncio.CancelledError:
            # 根据已保存的中断意图区分暂停与取消，不将用户操作作为错误传播。
            if self._interrupt == "paused":
                self._set_state("paused")
            else:
                tmp.unlink(missing_ok=True)
                self._set_state("canceled")
        except Exception as exc:
            logger.exception("Download failed")
            self._set_state("failed")
            self.failed.emit(str(exc))
        finally:
            self._interrupt = None
            self._aio_task = None
            # 在协程退出时执行已登记清理，避免界面线程等待。
            if self._cleanup_part:
                self._do_cleanup_files()
            self._run_finished.set()


@dataclass
class _DownloadConsumer:
    """记录归档消费者关联的文件编号、摘要及已解析路径，供引用保护和延后删除使用。"""

    file_id: int
    md5: str
    path: Path | None


class GbDownloadManager(QObject):
    """管理下载任务、缓存归档、使用者引用和历史记录，并通过独立事件循环执行下载。"""

    task_added = Signal(GbDownloadTask)
    task_removed = Signal(int)
    queue_changed = Signal()

    def __init__(self, base_dir: Path | None = None, parent: QObject | None = None) -> None:
        """索引现有归档、清理启动前残留分片、恢复展示记录，并建立下载事件循环。"""
        super().__init__(parent)
        self._base = Path(base_dir) if base_dir else get_gb_download_dir()
        self._base.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[int, GbDownloadTask] = {}
        self._md5_to_path: dict[str, Path] = {}
        self._consumers: dict[str, _DownloadConsumer] = {}
        self._pending_deletions: dict[Path, set[str]] = {}
        self._removed_running_tasks: set[GbDownloadTask] = set()
        # 加载持久化记录用于任务列表展示。
        # 历史记录只恢复界面所需字段，不包含完整的在线详情和文件对象。
        self._loop: asyncio.AbstractEventLoop | None = None
        self._worker_thread: threading.Thread | None = None
        self._scan_cache(clean_partials=True)
        self._restore_history()
        self._start_loop()

    def shutdown(self) -> None:
        """请求停止后台事件循环并最多等待线程两秒，不在此处等待活动下载完成。"""
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                pass
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=2)

    def _start_loop(self) -> None:
        """启动下载专用事件循环线程，构造阶段最多等待五秒以便后续提交任务。"""
        ready = threading.Event()

        def run():
            """在线程中创建并运行下载事件循环，循环停止后关闭其资源。"""
            loop = asyncio.new_event_loop()
            # 将事件循环中未被调用方消费的异常交给本模块日志记录。
            loop.set_exception_handler(self._on_loop_error)
            self._loop = loop
            asyncio.set_event_loop(loop)
            ready.set()
            try:
                loop.run_forever()
            finally:
                loop.close()

        self._worker_thread = threading.Thread(
            target=run,
            name="gb-download-loop",
            daemon=True,
        )
        self._worker_thread.start()
        # 等待事件循环就绪后再返回，使构造完成后提交的任务能够被调度。
        ready.wait(timeout=5)

    @staticmethod
    def _on_loop_error(_loop, context: dict) -> None:
        """记录下载事件循环中未被任务调用方处理的异常。"""
        exc = context.get("exception")
        msg = context.get("message") or "asyncio 未处理错误"
        if exc is not None:
            logger.error("下载 loop 异常：%s", msg, exc_info=exc)
        else:
            logger.error("下载 loop 异常：%s（context=%r）", msg, context)

    def _scan_cache(self, *, clean_partials: bool = False) -> None:
        """按文件名摘要重建完整归档索引，排除分片，并可在启动时清理遗留分片。"""
        self._md5_to_path.clear()
        for p in self._base.glob("*__*"):
            name = p.name
            # 未完成分片不属于已下载文件，不加入 MD5 路径索引。
            # 启动时尚无活动下载，可清理之前运行留下的分片；任务上下文不支持跨进程续传。
            if name.endswith(".part"):
                if clean_partials:
                    try:
                        p.unlink()
                        logger.debug("清理残留的未完成下载: %s", name)
                    except OSError:
                        pass
                continue
            md5, _, _ = name.partition("__")
            if md5:
                self._md5_to_path[md5.lower()] = p

    def _restore_history(self) -> None:
        """将下载队列中的有效历史记录恢复到任务列表。

        历史记录用于展示及文件定位、链接复制、记录删除；重新下载仍创建新任务。
        必填字段缺失或类型错误的记录直接跳过。
        """
        for entry in self._read_queue_items():
            if not self._queue_entry_ok(entry):
                logger.warning("queue.json 历史条目字段不合法，已丢弃：%r", entry)
                continue
            file_id = entry["file_id"]
            if file_id in self._tasks:
                continue
            profile = _HistoryProfile(mod_id=entry["mod_id"], name=entry["name"])
            file = _HistoryFile(
                file_id=file_id,
                file_name=entry["name"],
                md5_checksum=entry["md5"],
                download_url=entry["download_url"],
            )
            t = GbDownloadTask(profile, file, Path(entry["path"]), game_id=entry["game_id"])
            t._state = "done"
            t._run_finished.set()
            self._tasks[file_id] = t

    # queue.json：唯一当前结构 {"schema_version": 1, "items": [...]}

    @property
    def _queue_file(self) -> Path:
        """返回下载目录内保存任务历史的队列文件路径。"""
        return self._base / "queue.json"

    @classmethod
    def _queue_entry_ok(cls, entry: object) -> bool:
        """校验历史记录的必填字段及类型。

        无校验和的来源允许 MD5 为空；路径、名称和游戏编号必须有效，
        才能用于文件操作及所属游戏筛选。
        """
        if not isinstance(entry, dict):
            return False
        for key in ("mod_id", "file_id"):
            value = entry.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return False
        if entry["file_id"] <= 0:
            return False
        for key in ("md5", "name", "path", "download_url", "game_id", "ts"):
            value = entry.get(key)
            if not isinstance(value, str):
                return False
            if key != "md5" and not value.strip():
                return False
        return True

    def _read_queue_items(self) -> list[dict]:
        """读取下载队列的 items 列表。

        JSON 或根结构无效时，将队列文件重命名为带时间戳的 invalid 备份，
        再创建空队列；已下载的压缩包保留并由缓存扫描识别。
        schema_version 不限制读取，单项有效性由字段校验确定。
        """
        queue_file = self._queue_file
        if not queue_file.exists():
            return []
        try:
            data = json.loads(queue_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            self._backup_invalid_queue(f"无法解析: {e}")
            return []
        if not isinstance(data, dict):
            self._backup_invalid_queue(f"根节点是 {type(data).__name__}，不是对象")
            return []
        version = data.get("schema_version")
        if version != QUEUE_SCHEMA_VERSION:
            logger.info(
                "queue.json schema_version=%r 与本程序的 %d 不同，按「缺失即默认」照常读取",
                version,
                QUEUE_SCHEMA_VERSION,
            )
        items = data.get("items")
        if not isinstance(items, list):
            self._backup_invalid_queue(f"items 是 {type(items).__name__}，不是列表")
            return []
        return [
            {**item, "path": resolve_stored_path(item["path"])} if self._queue_entry_ok(item) else item
            for item in items
        ]

    def _write_queue_items(self, items: list[dict]) -> None:
        """按当前版本写入队列记录，将有效路径转换为存储形式，写入失败时记录警告。"""
        stored = [
            {**item, "path": to_stored_path(item["path"])} if self._queue_entry_ok(item) else item for item in items
        ]
        payload = {"schema_version": QUEUE_SCHEMA_VERSION, "items": stored}
        try:
            self._queue_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            logger.warning("queue.json 写回失败：%s", e)

    def _backup_invalid_queue(self, reason: str) -> None:
        """为无效队列选择不冲突的时间戳文件名并尝试改名备份，备份失败时记录原因。"""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self._queue_file.with_name(f"queue.json.invalid-{stamp}")
        seq = 1
        while target.exists():
            target = self._queue_file.with_name(f"queue.json.invalid-{stamp}-{seq}")
            seq += 1
        logger.error("queue.json 无效（%s），已备份为 %s 并从空队列起步", reason, target)
        try:
            os.replace(self._queue_file, target)
        except OSError as e:
            logger.warning("备份无效 queue.json 失败：%s", e)

    def has_local_md5(self, md5: str) -> Path | None:
        """返回指定 MD5 对应的本地完成文件，不存在时返回 None。

        命中索引后再次确认文件存在；外部删除产生的失效索引须移除，以允许重新下载。
        """
        key = (md5 or "").lower()
        path = self._md5_to_path.get(key)
        if path is None:
            return None
        try:
            if path.exists():
                return path
        except OSError:
            pass
        self._md5_to_path.pop(key, None)
        return None

    def refresh_local_index(self) -> None:
        """重新扫描下载目录，同步本地文件索引并通知界面更新。

        外部清理下载文件后调用，避免安装流程使用已失效的缓存路径。
        """
        self._scan_cache()
        self.queue_changed.emit()

    def get_task(self, file_id: int) -> GbDownloadTask | None:
        """按在线文件编号返回当前任务，未登记时返回空值。"""
        return self._tasks.get(file_id)

    def retain(self, file_id: int, md5: str, *, owner: str, path: Path | None = None) -> None:
        """登记下载、更新或安装对文件的占用；同一 owner 重复登记只更新元数据。"""
        checksum = (md5 or "").lower()
        if path is None:
            path = self.has_local_md5(checksum) if checksum else None
            task = self._tasks.get(file_id)
            if path is None and task is not None:
                path = task.archive_path
            previous = self._consumers.get(owner)
            if path is None and previous is not None and (previous.file_id, previous.md5) == (file_id, checksum):
                path = previous.path
        self._consumers[owner] = _DownloadConsumer(
            file_id,
            checksum,
            Path(path).resolve() if path is not None else None,
        )
        self._delete_unused_archives()

    def release(self, owner: str) -> None:
        """释放一个消费者；最后一个占用释放后执行此前登记的删包请求。"""
        self._consumers.pop(owner, None)
        self._delete_unused_archives()

    def has_other_consumers(self, file_id: int, owners: set[str]) -> bool:
        """同一网络任务是否还有 owners 之外的消费者（网络任务按 file ID 共享）。"""
        return any(consumer.file_id == file_id and owner not in owners for owner, consumer in self._consumers.items())

    def _bind_consumer_paths(self, file_id: int, path: Path) -> None:
        """为同一文件尚未取得路径的使用者绑定解析后的归档绝对路径。"""
        resolved = path.resolve()
        for consumer in self._consumers.values():
            if consumer.file_id == file_id and consumer.path is None:
                consumer.path = resolved

    def _archive_md5s(self, path: Path) -> set[str]:
        """从已有元数据识别共享缓存，避免在 GUI 线程重读大文件算 MD5。"""
        parsed_md5, _ = download_store.parse_download_name(path.name)
        checksums = {parsed_md5} if parsed_md5 else set()
        checksums.update(
            md5 for md5, cached in self._md5_to_path.items() if md5 and md5 != "nomd5" and cached.resolve() == path
        )
        checksums.update(
            consumer.md5 for consumer in self._consumers.values() if consumer.md5 and consumer.path == path
        )
        checksums.update(
            (task.file.md5_checksum or "").lower()
            for task in (*self._tasks.values(), *self._removed_running_tasks)
            if task.file.md5_checksum and task.archive_path.resolve() == path
        )
        return checksums

    def delete_archive_when_unused(self, path: Path) -> None:
        """仅在下载目录内登记删包；同路径或同非空 MD5 的消费者全部释放后才删除。"""
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self._base.resolve()):
            return
        self._pending_deletions.setdefault(resolved, set()).update(self._archive_md5s(resolved))
        self._delete_unused_archives()

    def _delete_unused_archives(self) -> None:
        """仅在没有使用者或活动下载占用时删除已登记的归档，删除后刷新本地索引。"""
        changed = False
        for path, checksums in list(self._pending_deletions.items()):
            checksums.update(self._archive_md5s(path))
            if any(
                consumer.path == path or (consumer.md5 and consumer.md5 in checksums)
                for consumer in self._consumers.values()
            ):
                continue
            # 删条目后协程可能尚未结束；等终态信号后再清理成品，不阻塞 GUI。
            if any(
                task.state not in TASK_TERMINAL_STATES and task.archive_path.resolve() == path
                for task in (*self._tasks.values(), *self._removed_running_tasks)
            ):
                continue
            existed = path.is_file()
            deleted_bytes = download_store.delete_if_download(path, base=self._base)
            if not path.exists():
                self._pending_deletions.pop(path, None)
                # 空文件删除返回 0，也需要刷新缓存和下载面板。
                changed = changed or existed or bool(deleted_bytes)
        if changed:
            self.refresh_local_index()

    def request(
        self,
        profile: GbModProfile,
        file: GbModFile,
        *,
        game_id: str,
        consumer: str | None = None,
    ) -> GbDownloadTask | None:
        """登记文件使用者并复用缓存或任务；缓存命中返回空值，否则创建或重试下载任务。"""
        md5 = (file.md5_checksum or "").lower()
        previous = self._tasks.get(file.file_id)
        if previous is not None and previous.state == "done" and not previous.archive_path.is_file():
            # 归档已清理时，完成记录不能作为可复用的下载结果。
            self.delete_task(file.file_id)
        self.retain(file.file_id, md5, owner=f"download:{file.file_id}" if consumer is None else consumer)
        # 通过 has_local_md5 同时检查磁盘状态并移除失效索引。
        if md5 and self.has_local_md5(md5) is not None:
            return None
        if file.file_id in self._tasks:
            task = self._tasks[file.file_id]
            if task.state in ("failed", "canceled", "paused"):
                self.retry(file.file_id)
            return task
        safe = _safe_name(file.file_name)
        archive = self._base / f"{md5 or 'nomd5'}__{file.file_id}__{safe}"
        task = GbDownloadTask(profile, file, archive, game_id=game_id)
        task.finished.connect(lambda p, t=task: self._on_task_done(t, p))
        task.state_changed.connect(lambda state, t=task: self._on_task_state_changed(t, state))
        self._tasks[file.file_id] = task
        self._bind_consumer_paths(file.file_id, archive)
        self.task_added.emit(task)
        self.queue_changed.emit()
        self._schedule(task)
        return task

    def _schedule(self, task: GbDownloadTask) -> None:
        """将任务绑定到下载循环并跨线程提交协程，通过完成回调消费未被等待的异常。"""
        if self._loop is None:
            logger.error("下载 loop 未就绪，任务无法调度：file_id=%s", task.file.file_id)
            return
        # 绑定事件循环后，等待响应头的任务也能接收取消请求。
        task.bind_loop(self._loop)

        async def runner():
            """在下载管理器的事件循环中运行单个任务的下载协程。"""
            await task._run_async()

        try:
            fut = asyncio.run_coroutine_threadsafe(runner(), self._loop)
        except RuntimeError as e:
            logger.error("提交下载协程失败：%s", e)
            return

        def _log_result(f) -> None:
            """记录异步提交结果中未被调用方等待的异常。

            下载主体以外的异常处理或清理分支仍可能失败，须通过完成回调消费异常。
            """
            try:
                f.result()
            except concurrent.futures.CancelledError:
                pass
            except Exception:
                logger.exception("下载协程异常退出：file_id=%s", task.file.file_id)

        fut.add_done_callback(_log_result)

    def _on_task_state_changed(self, task: GbDownloadTask, state: str) -> None:
        """终态时释放已移除任务的引用；失败或取消仅解除同一任务的占用，避免影响后续重试。"""
        if state in TASK_TERMINAL_STATES:
            self._removed_running_tasks.discard(task)
        if state in ("failed", "canceled"):
            # 被删除的旧任务晚到的信号不能释放同 file ID 的新任务占用。
            if self._tasks.get(task.file.file_id) is task:
                self.release(f"download:{task.file.file_id}")
            else:
                self._delete_unused_archives()

    def _on_task_done(self, task: GbDownloadTask, path: Path) -> None:
        """索引完成归档并绑定使用者路径，仅为当前任务保存历史，再清理无引用归档并通知队列变化。"""
        md5 = (task.file.md5_checksum or "").lower()
        if md5:
            self._md5_to_path[md5] = path
        self._bind_consumer_paths(task.file.file_id, path)
        if self._tasks.get(task.file.file_id) is task:
            self._persist_history(task.file, path, task.profile, task.game_id)
        self._delete_unused_archives()
        # 等待中的更新可直接导入缓存，不占用新的下载并发名额。
        self.queue_changed.emit()

    def _persist_history(
        self,
        file: GbModFile,
        path: Path,
        profile: GbModProfile,
        game_id: str,
    ) -> None:
        """把一条完成的下载写进 queue.json（始终带版本号与全部字段）。"""
        entry = {
            "mod_id": getattr(profile, "mod_id", 0),
            "file_id": file.file_id,
            "md5": (file.md5_checksum or "").lower(),
            "name": file.file_name,
            "path": str(path),
            "download_url": getattr(file, "download_url", ""),
            "game_id": game_id,
            "ts": datetime.now().isoformat(),
        }
        items = [e for e in self._read_queue_items() if not (isinstance(e, dict) and e.get("file_id") == file.file_id)]
        items.insert(0, entry)
        self._write_queue_items(items[:50])  # 历史记录上限 50

    def _drop_from_history(self, file_id: int) -> None:
        """从持久化队列移除指定文件的记录，仅在内容确有变化时写回。"""
        items = self._read_queue_items()
        kept = [e for e in items if not (isinstance(e, dict) and e.get("file_id") == file_id)]
        if len(kept) == len(items):
            return
        self._write_queue_items(kept)

    def cancel(self, file_id: int) -> None:
        """向已登记的指定下载任务转发取消请求。"""
        task = self._tasks.get(file_id)
        if task is not None:
            task.cancel()

    def pause(self, file_id: int) -> None:
        """向已登记的指定任务请求暂停，由任务保留可续传分片。"""
        task = self._tasks.get(file_id)
        if task is not None:
            task.pause()

    def resume(self, file_id: int) -> None:
        """恢复指定任务的运行状态并重新提交下载，任务不存在时忽略。"""
        task = self._tasks.get(file_id)
        if task is None:
            return
        task.resume()
        self._schedule(task)

    def retry(self, file_id: int) -> None:
        """仅对失败、取消或暂停的任务重置状态并重新调度。"""
        task = self._tasks.get(file_id)
        if task and task.state in ("failed", "canceled", "paused"):
            task.resume()
            self._schedule(task)

    def delete_task(self, file_id: int, *, delete_file: bool = False) -> None:
        """删除任务，并按需请求清理下载文件。

        活动协程负责退出时清理文件，界面线程不等待其结束。
        """
        task = self._tasks.pop(file_id, None)
        if task is None:
            self.release(f"download:{file_id}")
            return
        if task.is_running:
            # 任务删除后不再保留下载分片，完成文件是否删除由参数决定。
            if task.state not in TASK_TERMINAL_STATES:
                self._removed_running_tasks.add(task)
            task.request_cleanup(delete_product=False)
            task.cancel()
        else:
            # 没有活动协程时直接清理对应文件。
            try:
                task._part_path().unlink(missing_ok=True)
            except OSError as e:
                logger.warning("delete archive 失败: %s", e)
        if delete_file:
            self.delete_archive_when_unused(task.archive_path)
        self.release(f"download:{file_id}")
        self._drop_from_history(file_id)
        self.task_removed.emit(file_id)
        self.queue_changed.emit()

    def clear_completed(self, *, game_id: str | None = None) -> None:
        """清理已结束的任务记录；指定 game_id 时仅处理该游戏，与面板过滤范围一致。"""
        done = [
            fid
            for fid, t in self._tasks.items()
            if t.state in TASK_TERMINAL_STATES and (game_id is None or t.game_id == game_id)
        ]
        for fid in done:
            del self._tasks[fid]
            self.release(f"download:{fid}")
            self._drop_from_history(fid)
            self.task_removed.emit(fid)
        self.queue_changed.emit()

    def clear_all(self, *, delete_files: bool = False, game_id: str | None = None) -> None:
        """取消并清除任务，delete_files 控制是否同时删除文件。

        指定 game_id 时只处理该游戏，保持操作范围与下载面板一致。
        """
        for fid in [fid for fid, t in self._tasks.items() if game_id is None or t.game_id == game_id]:
            self.delete_task(fid, delete_file=delete_files)

    def active(self, *, game_id: str | None = None) -> list[GbDownloadTask]:
        """返回当前任务；game_id 为 None 时不筛选，否则精确匹配所属游戏。"""
        tasks = list(self._tasks.values())
        if game_id is not None:
            tasks = [t for t in tasks if t.game_id == game_id]
        return tasks


# 历史任务仅恢复界面展示和文件操作所需的字段。


class _HistoryProfile:
    """供历史任务展示使用的详情字段。"""

    def __init__(self, *, mod_id: int, name: str) -> None:
        """恢复下载历史展示需要的资源编号与名称。"""
        self.mod_id = mod_id
        self.name = name


class _HistoryFile:
    """供历史任务展示使用的文件字段。"""

    def __init__(
        self,
        *,
        file_id: int,
        file_name: str,
        md5_checksum: str,
        download_url: str,
    ) -> None:
        """恢复历史下载的文件身份、名称、摘要和地址，供文件定位与记录展示使用。"""
        self.file_id = file_id
        self.file_name = file_name
        self.md5_checksum = md5_checksum
        self.download_url = download_url
