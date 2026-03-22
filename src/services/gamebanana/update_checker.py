"""基于安装文件指纹检查 Mod 更新。

detect_update 执行无网络访问的更新判断，UpdateInfo 保存可持久化的检查结果。
ModUpdateChecker 在后台批量获取活动文件，再与各本地安装基线比较。
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import CancelledError as FutureCancelledError
from dataclasses import dataclass, field

import httpx
from PySide6.QtCore import QObject, Signal, Slot
from rapidfuzz import fuzz

from src.services.gamebanana.client import UpdateCheckError
from src.services.gamebanana.models import (
    GbInstallMetadata,
    GbModel,
    GbModFile,
    GbModProfile,
    GbModUpdateEntry,
)
from src.utils.logger import logger
from src.utils.paths import resolve_stored_path, to_stored_path


def _file_to_dict(f: GbModFile) -> dict:
    """序列化更新候选文件，保留下载地址、版本、摘要和统计信息。"""
    return {
        "file_id": f.file_id,
        "file_name": f.file_name,
        "description": f.description,
        "download_url": f.download_url,
        "size_bytes": f.size_bytes,
        "date_added_ts": f.date_added_ts,
        "md5_checksum": f.md5_checksum,
        "av_result": f.av_result,
        "version": f.version,
        "download_count": f.download_count,
    }


def _file_from_dict(d: dict) -> GbModFile:
    """从缓存字段恢复候选文件模型，缺失值使用默认值，非法数值由转换异常报告。"""
    return GbModFile(
        file_id=int(d.get("file_id") or 0),
        file_name=str(d.get("file_name") or ""),
        description=str(d.get("description") or ""),
        download_url=str(d.get("download_url") or ""),
        size_bytes=int(d.get("size_bytes") or 0),
        date_added_ts=int(d.get("date_added_ts") or 0),
        md5_checksum=str(d.get("md5_checksum") or ""),
        av_result=str(d.get("av_result") or ""),
        version=str(d.get("version") or ""),
        download_count=int(d.get("download_count") or 0),
    )


def _entry_to_dict(u: GbModUpdateEntry) -> dict:
    """序列化更新日志，并复制标签与受影响文件列表以隔离可变数据。"""
    return {
        "title": u.title,
        "date_added_ts": u.date_added_ts,
        "description_html": u.description_html,
        "tags": list(u.tags),
        "version": u.version,
        "affected_files": list(u.affected_files),
    }


def _entry_from_dict(d: dict) -> GbModUpdateEntry:
    """从缓存字段恢复更新日志，缺失列表和文本使用空值。"""
    return GbModUpdateEntry(
        title=str(d.get("title") or ""),
        date_added_ts=int(d.get("date_added_ts") or 0),
        description_html=str(d.get("description_html") or ""),
        tags=list(d.get("tags") or []),
        version=str(d.get("version") or ""),
        affected_files=list(d.get("affected_files") or []),
    )


@dataclass
class UpdateInfo:
    """关联本地 Mod 与在线更新证据，保存当前版本、候选文件、日志及界面展示信息。"""

    local_mod_id: str
    gb_mod_id: int
    current_version: str | None
    current_file_ts: int
    latest_file: GbModFile
    latest_version: str
    candidate_files: list[GbModFile]
    changelog: list[GbModUpdateEntry]
    page_url: str
    local_name: str = ""
    local_preview: str = ""

    def to_dict(self) -> dict:
        """序列化完整更新结果，将本地预览路径转换为可随应用迁移的存储形式。"""
        return {
            "local_mod_id": self.local_mod_id,
            "gb_mod_id": self.gb_mod_id,
            "current_version": self.current_version,
            "current_file_ts": self.current_file_ts,
            "latest_file": _file_to_dict(self.latest_file),
            "latest_version": self.latest_version,
            "candidate_files": [_file_to_dict(f) for f in self.candidate_files],
            "changelog": [_entry_to_dict(u) for u in self.changelog],
            "page_url": self.page_url,
            "local_name": self.local_name,
            "local_preview": to_stored_path(self.local_preview),
        }

    @classmethod
    def from_dict(cls, d: dict) -> UpdateInfo:
        """恢复缓存更新结果及嵌套文件、日志模型，并解析本地预览的存储路径。"""
        return cls(
            local_mod_id=str(d.get("local_mod_id") or ""),
            gb_mod_id=int(d.get("gb_mod_id") or 0),
            current_version=d.get("current_version"),
            current_file_ts=int(d.get("current_file_ts") or 0),
            latest_file=_file_from_dict(d.get("latest_file") or {}),
            latest_version=str(d.get("latest_version") or ""),
            candidate_files=[_file_from_dict(x) for x in (d.get("candidate_files") or [])],
            changelog=[_entry_from_dict(x) for x in (d.get("changelog") or [])],
            page_url=str(d.get("page_url") or ""),
            local_name=str(d.get("local_name") or ""),
            local_preview=resolve_stored_path(str(d.get("local_preview") or "")),
        )


def detect_update(
    gb_meta: GbInstallMetadata,
    profile: GbModProfile | None,
    files: list[GbModFile],
    updates: list[GbModUpdateEntry],
    ignored_file_ids,
) -> UpdateInfo | None:
    """按文件时间、内容指纹和忽略规则判断更新，无有效更新时返回 None。"""
    if not files:
        return None
    ignored = set(ignored_file_ids or set())
    cur_md5 = (gb_meta.file_md5 or "").lower()
    candidates = [
        f
        for f in files
        if f.date_added_ts > gb_meta.file_ts and (f.md5_checksum or "").lower() != cur_md5 and f.file_id not in ignored
    ]
    if not candidates:
        return None
    candidates = sorted(candidates, key=lambda f: f.date_added_ts, reverse=True)
    latest = candidates[0]
    # 缺少安装时间和指纹时，若本地版本号与最新文件版本一致则视为已更新，
    # 避免仅有来源 URL 的记录反复提示同一版本。
    baseline_unknown = gb_meta.file_ts == 0 and not (gb_meta.file_md5 or "")
    if baseline_unknown and (gb_meta.version or "") and latest.version == gb_meta.version:
        return None
    changelog = sorted(
        [u for u in (updates or []) if u.date_added_ts > gb_meta.file_ts],
        key=lambda u: u.date_added_ts,
        reverse=True,
    )
    page_url = (getattr(profile, "page_url", "") or gb_meta.page_url) if profile else gb_meta.page_url
    return UpdateInfo(
        local_mod_id="",
        gb_mod_id=gb_meta.mod_id,
        current_version=gb_meta.version,
        current_file_ts=gb_meta.file_ts,
        latest_file=latest,
        latest_version=latest.version or "",
        candidate_files=candidates,
        changelog=changelog,
        page_url=page_url,
    )


def pick_default_file(candidate_files: list[GbModFile], local_original_name: str) -> GbModFile:
    """使用 rapidfuzz 选择与本地文件名最接近的更新文件，供界面默认选中。"""
    if not candidate_files:
        raise ValueError("candidate_files is empty")
    hay = (local_original_name or "").lower()
    if not hay:
        return candidate_files[0]

    def score(f: GbModFile) -> float:
        """按文件名包含关系和模糊匹配计算相似度，返回用于默认候选排序的零到一分数。"""
        cl = (f.file_name or "").lower()
        if cl and cl in hay or (hay and hay in cl):
            return 1.0
        return max(fuzz.partial_ratio(cl, hay), fuzz.token_set_ratio(cl, hay)) / 100.0

    return max(candidate_files, key=score)


@dataclass
class _CheckRun:
    """保存单轮更新检查的任务、取消标记、计数与异步句柄，用于隔离迟到结果。"""

    jobs: list
    cancelled: threading.Event = field(default_factory=threading.Event)
    counts: dict[str, int] = field(default_factory=lambda: {"found": 0, "up_to_date": 0, "failed": 0})
    completed_ids: set[str] = field(default_factory=set)
    finished: bool = False
    future: object = None
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task | None = None


class ModUpdateChecker(QObject):
    """批量文件级检测；相同远端 ID 共用数据，详情请求有界且只按需发送。"""

    _DETAIL_CONCURRENCY = 2

    progress = Signal(int, int)  # 参数：已完成数量、总数。
    mod_update_found = Signal(str, object)  # 参数：本地 Mod 标识、更新信息。
    mod_up_to_date = Signal(str)  # 参数：本地 Mod 标识。
    mod_check_failed = Signal(str, str)  # 参数：本地 Mod 标识、错误信息。
    finished = Signal(dict)  # 结果包含发现更新、已是最新和失败三类计数。
    _event = Signal(object, str, object)  # 参数：运行标识、事件和数据；在此对象所属线程投递。

    def __init__(self, client, parent: QObject | None = None, *, submit=None) -> None:
        """保存在线客户端与可选共享调度器，建立单轮检查状态及跨线程结果分发入口。"""
        super().__init__(parent)
        self._client = client
        # 可注入异步提交函数，返回 concurrent.futures.Future。
        # 注入后由商店提供的长期事件循环执行检查，
        # 使共用的 httpx 客户端及连接池始终属于同一循环。
        self._submit = submit
        self._thread: threading.Thread | None = None
        # 通过显式状态防止重复启动检查。
        self._running = False
        self._run: _CheckRun | None = None
        self._shared_requests: set[asyncio.Task] = set()
        self._event.connect(self._dispatch_event)

    def _begin_run(self, jobs) -> _CheckRun:
        """复制本轮任务并登记运行状态，使后续结果携带可核对的轮次身份。"""
        run = _CheckRun(list(jobs))
        self._run = run
        self._running = True
        return run

    @Slot(object, str, object)
    def _dispatch_event(self, run: _CheckRun, event: str, payload) -> None:
        """在界面线程核对轮次和取消状态，去重单项结果并仅发布一次本轮终态。"""
        # 不能只在 worker 发信号前检查取消：那时的信号可能已经排进 Qt 队列，
        # 用户取消并开始新一轮后才送达。轮次身份必须在 GUI 线程实际分发时再次校验。
        if run is not self._run or run.finished:
            return
        if run.cancelled.is_set() and event != "cancelled":
            return
        if event == "result":
            mid, kind, value = payload
            if mid in run.completed_ids:
                return
            run.completed_ids.add(mid)
            run.counts[kind] += 1
            if kind == "found":
                self.mod_update_found.emit(mid, value)
            elif kind == "failed":
                self.mod_check_failed.emit(mid, value)
            else:
                self.mod_up_to_date.emit(mid)
            if run is self._run and not run.finished:
                self.progress.emit(len(run.completed_ids), len(run.jobs))
        elif event == "progress":
            self.progress.emit(*payload)
        elif event == "error":
            for mid, _ in run.jobs:
                self._dispatch_event(run, "result", (mid, "failed", payload))
            self._dispatch_event(run, "finished", None)
        elif event in {"finished", "cancelled"}:
            counts = dict(payload if payload is not None else run.counts)
            if event == "cancelled":
                run.cancelled.set()
                counts["cancelled"] = True
            run.finished = True
            self._running = False
            self.finished.emit(counts)
