"""固定更新附件身份，独占管理当前会话的分片并校验 HTTP 字节区间。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
from PySide6.QtCore import QLockFile

from src.utils.i18n import t as tr
from src.utils.logger import logger

_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)", re.I)
_PARTIAL_FILES = ("update.zip.part", "resume.json", "resume.tmp")


def _is_link(path: Path) -> bool:
    """识别符号链接及 Windows 目录联接，避免清理进入其它目录或借用外部锁文件。"""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _discard_partial_files(directory: Path) -> None:
    """仅移除本下载器拥有的分片与旧续传记录，不递归删除目录内容。"""
    for name in _PARTIAL_FILES:
        (directory / name).unlink(missing_ok=True)


def cleanup_incomplete_downloads(root: Path) -> None:
    """回收空闲附件目录中的旧分片；跳过活动任务、重解析点及非下载器目录。"""
    try:
        resolved_root = root.resolve()
        directories = list(root.iterdir())
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("程序更新：无法检查遗留下载分片：%s", exc)
        return
    for directory in directories:
        try:
            if (
                re.fullmatch(r"[0-9a-f]{64}", directory.name) is None
                or _is_link(directory)
                or not directory.is_dir()
                or directory.resolve().parent != resolved_root
                or _is_link(directory / "download.lock")
            ):
                continue
            lock = QLockFile(str(directory / "download.lock"))
            lock.setStaleLockTime(0)
            if not lock.tryLock(0):
                continue
            try:
                _discard_partial_files(directory)
            finally:
                lock.unlock()
        except OSError as exc:
            logger.warning("程序更新：清理遗留下载分片失败（%s）：%s", directory.name, exc)


class DownloadError(RuntimeError):
    """下载无法继续，向调用方报告可显示的失败原因。"""


class InvalidResponse(DownloadError):
    """响应不能作为所请求的字节区间，须关闭后重新下载。"""


@dataclass(frozen=True)
class UpdateArtifact:
    """与线路无关的固定附件身份，摘要和精确大小是发布文件的必要条件。"""

    url: str
    size: int
    sha256: str
    repo: str = ""
    tag: str = ""
    asset_id: int = 0
    name: str = ""

    def __post_init__(self) -> None:
        """拒绝缺少完整性信息的附件，统一摘要大小写以稳定缓存身份。"""
        if (
            not self.url
            or type(self.size) is not int
            or self.size <= 0
            or not isinstance(self.sha256, str)
            or re.fullmatch(r"[a-fA-F0-9]{64}", self.sha256) is None
        ):
            raise ValueError(tr("更新包缺少有效的大小或 SHA-256，无法安全下载。"))
        object.__setattr__(self, "sha256", self.sha256.lower())


class DownloadCache:
    """以附件身份隔离缓存；锁覆盖整个会话，分片只能由持锁者写入。"""

    def __init__(self, root: Path, artifact: UpdateArtifact) -> None:
        """只计算受控缓存路径，不使用远程文件名构造本地路径。"""
        self.artifact = artifact
        encoded = json.dumps(asdict(artifact), sort_keys=True, ensure_ascii=True).encode("utf-8")
        self.directory = root / hashlib.sha256(encoded).hexdigest()
        self.part_path = self.directory / "update.zip.part"
        self.final_path = self.directory / "update.zip"

    @contextmanager
    def locked(self):
        """验证当前目标后取得跨进程锁；拒绝目录联接时不能进入任何清理路径。"""
        if (
            _is_link(self.directory)
            or self.directory.resolve().parent != self.directory.parent.resolve()
            or _is_link(self.directory / "download.lock")
        ):
            raise DownloadError(tr("下载缓存路径无效，无法安全写入。"))
        self.directory.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(self.directory / "download.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            raise DownloadError(tr("另一个更新任务正在使用此下载文件，请稍后重试。"))
        try:
            yield
        finally:
            lock.unlock()

    def prepare(self) -> None:
        """新会话从零开始，同时清理其它空闲版本的残留；不再保存或读取续传记录。"""
        self.reset()
        cleanup_incomplete_downloads(self.directory.parent)

    def offset(self) -> int:
        """从已关闭的分片读取续传起点，不依赖可能滞后的界面或元数据计数。"""
        try:
            return self.part_path.stat().st_size
        except FileNotFoundError:
            return 0

    def reset(self) -> None:
        """持锁且无写入请求时丢弃分片与旧续传记录，完整归档由安装流程使用。"""
        _discard_partial_files(self.directory)

    async def verify(self, path: Path) -> bool:
        """在工作线程内逐块核对精确大小和摘要，块间允许响应取消。"""
        try:
            if path.stat().st_size != self.artifact.size:
                return False
            digest = hashlib.sha256()
            with path.open("rb") as source:
                while block := source.read(1 << 20):
                    digest.update(block)
                    await asyncio.sleep(0)
            return digest.hexdigest() == self.artifact.sha256
        except FileNotFoundError:
            return False

    def publish(self) -> Path:
        """校验成功且文件已关闭后，在同一目录原子发布完整更新包。"""
        self.part_path.replace(self.final_path)
        return self.final_path


def response_range(response: httpx.Response, offset: int, total: int, *, probe: bool = False) -> tuple[int, int]:
    """验证响应表示及区间，返回起止位置；200 表示服务端要求从零接收完整文件。"""
    if response.status_code == 416:
        raise InvalidResponse(tr("下载源拒绝了续传范围。"))
    response.raise_for_status()
    if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
        raise InvalidResponse(tr("下载源返回了不支持的内容编码。"))
    if response.status_code == 200:
        start, end = 0, total - 1
    elif response.status_code == 206:
        match = _CONTENT_RANGE.fullmatch(response.headers.get("content-range", ""))
        if match is None:
            raise InvalidResponse(tr("下载源返回了无效的续传范围。"))
        start, end, full = map(int, match.groups())
        if start != offset or not start <= end < total or full != total:
            raise InvalidResponse(tr("下载源返回了无效的续传范围。"))
        if offset == 0 and not probe and end != total - 1:
            raise InvalidResponse(tr("下载源未返回完整更新包。"))
    else:
        raise InvalidResponse(tr("下载源未返回完整更新包。"))
    length = response.headers.get("content-length")
    if length is not None and (not length.isascii() or not length.isdecimal() or int(length) != end - start + 1):
        raise InvalidResponse(tr("下载源返回的文件长度不符。"))
    return start, end
