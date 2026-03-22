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
