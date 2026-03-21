"""持久化下载完成后等待目标游戏恢复的安装请求。

下载期间切换游戏时，保留源包并暂存安装请求，避免使用其他游戏的目录树匹配目标。
记录只保存安装所需的标量字段和预览地址，重启后无需重新获取完整在线详情。

数据写入 data/downloads/pending_installs.json，同一 (game_id, file_id) 保留最新记录。
必填字段缺失或类型错误的条目不参与恢复。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.utils.logger import logger
from src.utils.paths import get_gb_download_dir, resolve_stored_path, to_stored_path

# 字段语义发生不兼容变化时递增版本号；新增可选字段不改变版本。
PENDING_SCHEMA_VERSION = 1

# 记录文件不属于安装包，下载扫描须跳过。
PENDING_FILE_NAME = "pending_installs.json"


@dataclass
class PendingInstall:
    """持久化安装流程所需的详情、文件及目标游戏信息。"""

    game_id: str
    archive_path: str
    # 安装目标匹配、来源元数据恢复和预览显示所需的详情字段。
    mod_id: int
    mod_name: str
    page_url: str = ""
    category_name: str = ""
    super_category_id: int = 0
    submitter_id: int = 0
    submitter_name: str = ""
    is_nsfw: bool = False
    # 保存详情预览 URL 列表，安装时采用首图。
    preview_images: list[str] = field(default_factory=list)
    # 下载文件的身份、版本和校验信息。
    file_id: int = 0
    file_name: str = ""
    md5_checksum: str = ""
    date_added_ts: int = 0
    version: str = ""
    download_url: str = ""
    size_bytes: int = 0
    saved_at: str = ""

    @classmethod
    def capture(cls, game_id: str, profile, file, archive_path) -> PendingInstall:
        """根据创建下载任务时的详情和文件信息建立安装快照。"""
        sub = getattr(profile, "submitter", None)
        previews = [str(u) for u in (getattr(profile, "preview_images", None) or []) if u]
        return cls(
            game_id=game_id,
            archive_path=str(archive_path),
            mod_id=int(getattr(profile, "mod_id", 0) or 0),
            mod_name=str(getattr(profile, "name", "") or ""),
            page_url=str(getattr(profile, "page_url", "") or ""),
            category_name=str(getattr(profile, "category_name", "") or ""),
            super_category_id=int(getattr(profile, "super_category_id", 0) or 0),
            submitter_id=int(getattr(sub, "id", 0) or 0),
            submitter_name=str(getattr(sub, "name", "") or ""),
            is_nsfw=bool(getattr(profile, "is_nsfw", False)),
            preview_images=previews,
            file_id=int(getattr(file, "file_id", 0) or 0),
            file_name=str(getattr(file, "file_name", "") or ""),
            md5_checksum=str(getattr(file, "md5_checksum", "") or ""),
            date_added_ts=int(getattr(file, "date_added_ts", 0) or 0),
            version=str(getattr(file, "version", "") or ""),
            download_url=str(getattr(file, "download_url", "") or ""),
            size_bytes=int(getattr(file, "size_bytes", 0) or 0),
            saved_at=datetime.now().isoformat(),
        )

    def to_dict(self) -> dict:
        """序列化待安装文件及在线来源，归档路径转换为支持应用目录迁移的存储形式。"""
        return {
            "game_id": self.game_id,
            "archive_path": to_stored_path(self.archive_path),
            "mod_id": self.mod_id,
            "mod_name": self.mod_name,
            "page_url": self.page_url,
            "category_name": self.category_name,
            "super_category_id": self.super_category_id,
            "submitter_id": self.submitter_id,
            "submitter_name": self.submitter_name,
            "is_nsfw": self.is_nsfw,
            "preview_images": list(self.preview_images),
            "file_id": self.file_id,
            "file_name": self.file_name,
            "md5_checksum": self.md5_checksum,
            "date_added_ts": self.date_added_ts,
            "version": self.version,
            "download_url": self.download_url,
            "size_bytes": self.size_bytes,
            "saved_at": self.saved_at,
        }

    @classmethod
    def from_dict(cls, d: object) -> PendingInstall | None:
        """校验待安装记录；必填字段缺失或类型错误时返回 None。"""
        if not isinstance(d, dict):
            return None
        game_id = d.get("game_id")
        archive = d.get("archive_path")
        if not isinstance(game_id, str) or not game_id.strip():
            return None
        if not isinstance(archive, str) or not archive.strip():
            return None
        try:
            file_id = int(d.get("file_id") or 0)
            mod_id = int(d.get("mod_id") or 0)
        except (TypeError, ValueError):
            return None
        if file_id <= 0 or mod_id <= 0:
            return None
        previews = d.get("preview_images")
        if not isinstance(previews, list):
            previews = []

        def _s(key: str) -> str:
            """读取文本字段，非字符串或缺失值按空文本处理。"""
            v = d.get(key)
            return v if isinstance(v, str) else ""

        def _i(key: str) -> int:
            """读取整数字段，缺失或转换失败时返回零。"""
            try:
                return int(d.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        return cls(
            game_id=game_id,
            archive_path=resolve_stored_path(archive),
            mod_id=mod_id,
            mod_name=_s("mod_name"),
            page_url=_s("page_url"),
            category_name=_s("category_name"),
            super_category_id=_i("super_category_id"),
            submitter_id=_i("submitter_id"),
            submitter_name=_s("submitter_name"),
            is_nsfw=bool(d.get("is_nsfw")),
            preview_images=[str(u) for u in previews if u],
            file_id=file_id,
            file_name=_s("file_name"),
            md5_checksum=_s("md5_checksum"),
            date_added_ts=_i("date_added_ts"),
            version=_s("version"),
            download_url=_s("download_url"),
            size_bytes=_i("size_bytes"),
            saved_at=_s("saved_at"),
        )


class _RestoredAuthor:
    """安装快照中的作者编号与名称。"""

    def __init__(self, author_id: int, name: str) -> None:
        """从待安装记录恢复安装流程需要的作者标识和名称。"""
        self.id = author_id
        self.name = name


class RestoredProfile:
    """从安装快照恢复的详情字段。

    仅提供安装流程需要的属性，不包含完整描述或文件列表。
    """

    def __init__(self, rec: PendingInstall) -> None:
        """恢复安装目标匹配所需的资源摘要与预览列表，无需重新获取完整在线详情。"""
        self.mod_id = rec.mod_id
        self.name = rec.mod_name
        self.page_url = rec.page_url
        self.category_name = rec.category_name
        self.super_category_id = rec.super_category_id
        self.submitter = _RestoredAuthor(rec.submitter_id, rec.submitter_name)
        self.is_nsfw = rec.is_nsfw
        self.preview_images = list(rec.preview_images)


class RestoredFile:
    """从安装快照恢复的下载文件字段。"""

    def __init__(self, rec: PendingInstall) -> None:
        """恢复已下载文件的安装元数据，保留服务端文件身份和摘要证据。"""
        self.file_id = rec.file_id
        self.file_name = rec.file_name
        self.md5_checksum = rec.md5_checksum
        self.date_added_ts = rec.date_added_ts
        self.version = rec.version
        self.download_url = rec.download_url
        self.size_bytes = rec.size_bytes


class PendingInstallStore:
    """读写待安装记录，每次变更立即持久化。"""

    def __init__(self, base_dir: Path | None = None) -> None:
        """定位下载目录中的待安装记录文件，暂不创建目录或读取记录。"""
        self._base = Path(base_dir) if base_dir else get_gb_download_dir()
        self._file = self._base / PENDING_FILE_NAME

    # 读写
    def _read(self) -> list[PendingInstall]:
        """读取待安装队列，文件损坏时返回空列表，单条无效记录单独丢弃并记录原因。"""
        if not self._file.exists():
            return []
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("待安装记录不可解析，按空处理：%s", e)
            return []
        items = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return []
        out: list[PendingInstall] = []
        for entry in items:
            rec = PendingInstall.from_dict(entry)
            if rec is None:
                logger.warning("待安装记录条目不合法，已丢弃：%r", entry)
                continue
            out.append(rec)
        return out

    def _write(self, records: list[PendingInstall]) -> None:
        """按当前格式版本保存待安装队列，创建目录或写入失败时记录错误。"""
        try:
            self._base.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": PENDING_SCHEMA_VERSION,
                "items": [r.to_dict() for r in records],
            }
            self._file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.error("写待安装记录失败：%s", e)

    # 公开操作接口
    def records(self) -> list[PendingInstall]:
        """读取待安装快照，不移除任何记录。"""
        return self._read()

    def add(self, rec: PendingInstall) -> None:
        """保存安装请求，替换具有相同游戏 ID 和文件 ID 的记录。"""
        kept = [r for r in self._read() if not (r.game_id == rec.game_id and r.file_id == rec.file_id)]
        kept.append(rec)
        self._write(kept)

    def take(self, game_id: str) -> list[PendingInstall]:
        """取出并移除指定游戏的全部待安装记录。"""
        all_recs = self._read()
        mine = [r for r in all_recs if r.game_id == game_id]
        if mine:
            self._write([r for r in all_recs if r.game_id != game_id])
        return mine

    def pending_game_ids(self) -> set[str]:
        """当前有待安装记录的游戏集合（供 UI 提示用）。"""
        return {r.game_id for r in self._read()}
