"""统计并清理 data/downloads 中的安装包。

scan_downloads 统计占用并标记已入库的文件，delete_if_download 清理已解除占用的安装包。
模块仅负责文件操作和判定，不依赖 Qt，也不共享主线程数据库连接。

下载文件名包含 MD5 和全站唯一的 file_id，匹配任一已入库标识即可确认安装包。
此判定不依赖下载队列，已超出队列保留数量的文件仍可识别。
后台统计通过独立只读数据库连接提取元数据。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from src.utils.logger import logger
from src.utils.paths import get_gb_download_dir

# 下载目录中的管理文件不计入安装包统计与清理。
# pending_installs.json 保存等待目标游戏恢复的安装请求，必须保留。
_NON_ARCHIVE_NAMES = {"queue.json", "pending_installs.json"}
_HEX = set("0123456789abcdef")


@dataclass(frozen=True)
class DownloadEntry:
    """下载目录中的单个安装包。"""

    path: Path
    size: int
    md5: str  # 无法解析时为空字符串，包括下载器使用的 nomd5 占位。
    file_id: int  # 无法解析时为 0。
    installed: bool


@dataclass(frozen=True)
class DownloadUsage:
    """一次扫描的汇总结果。"""

    total_bytes: int = 0
    total_count: int = 0
    purgeable_bytes: int = 0  # 已入库、可安全删除的部分
    purgeable_count: int = 0
    entries: tuple[DownloadEntry, ...] = ()


def scan_library_download_usage(db_path: Path, base: Path) -> DownloadUsage:
    """只读查询各游戏的来源元数据，再在工作线程中扫描下载目录。

    只读连接避免关闭或恢复期间创建空索引；不构造 Mod、不解析路径，也不访问应用单例。"""
    ids, md5s = set(), set()
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        for (raw,) in conn.execute("SELECT info_json FROM mods"):
            try:
                gb = (json.loads(raw).get("custom_data") or {}).get("gamebanana") or {}
                try:
                    file_id = int(gb.get("file_id") or 0)
                except (ValueError, TypeError):
                    file_id = 0
                md5 = str(gb.get("file_md5") or "").strip().lower()
                if file_id:
                    ids.add(file_id)
                if md5:
                    md5s.add(md5)
            except (ValueError, TypeError, AttributeError):
                continue
    finally:
        conn.close()
    return scan_downloads(ids, md5s, base=base)


def parse_download_name(name: str) -> tuple[str, int]:
    """从下载文件名解析 MD5 和文件编号。

    无法解析的字段分别返回空字符串和零，由调用方视为未确认安装。
    下载器的 nomd5 占位不符合 MD5 格式，不作为有效校验和。
    """
    parts = name.split("__", 2)
    if len(parts) < 2:
        return "", 0
    md5 = parts[0].strip().lower()
    if len(md5) != 32 or not set(md5) <= _HEX:
        md5 = ""
    try:
        file_id = int(parts[1])
    except (TypeError, ValueError):
        file_id = 0
    return md5, file_id


def scan_downloads(
    installed_file_ids: set[int] | None = None,
    installed_md5s: set[str] | None = None,
    *,
    base: Path | None = None,
) -> DownloadUsage:
    """扫描下载目录，标记安装包是否已入库并汇总占用。

    installed_file_ids 和 installed_md5s 提供已安装文件标识，base 可覆盖默认下载目录。
    子目录、队列记录、待安装记录及未完成分片不作为安装包统计。
    """
    ids = installed_file_ids or set()
    md5s = {m.lower() for m in (installed_md5s or set())}
    root = base if base is not None else get_gb_download_dir()

    entries: list[DownloadEntry] = []
    total_bytes = purge_bytes = purge_count = 0
    try:
        children = list(root.iterdir())
    except OSError as exc:
        logger.warning("扫描下载目录失败: %s", exc)
        return DownloadUsage()

    for p in children:
        name = p.name
        if name in _NON_ARCHIVE_NAMES or name.endswith(".part"):
            continue
        try:
            if not p.is_file():
                continue
            size = p.stat().st_size
        except OSError:
            continue
        md5, file_id = parse_download_name(name)
        installed = bool((file_id and file_id in ids) or (md5 and md5 in md5s))
        entries.append(DownloadEntry(p, size, md5, file_id, installed))
        total_bytes += size
        if installed:
            purge_bytes += size
            purge_count += 1

    return DownloadUsage(
        total_bytes=total_bytes,
        total_count=len(entries),
        purgeable_bytes=purge_bytes,
        purgeable_count=purge_count,
        entries=tuple(entries),
    )


def delete_if_download(path, *, base: Path | None = None) -> int:
    """仅删除下载目录内的指定文件，返回实际释放的字节数。

    商店安装和 Mod 更新成功后的源包清理共用此入口；用户手动导入的外部源文件不受影响。
    """
    root = base if base is not None else get_gb_download_dir()
    try:
        p = Path(str(path)).resolve()
        if not p.is_relative_to(root.resolve()) or not p.is_file():
            return 0
        size = p.stat().st_size
        p.unlink()
    except OSError as exc:
        logger.warning("删除源安装包失败 %s: %s", path, exc)
        return 0
    logger.info("已删除源安装包 %s（释放 %d 字节）", p.name, size)
    return size
