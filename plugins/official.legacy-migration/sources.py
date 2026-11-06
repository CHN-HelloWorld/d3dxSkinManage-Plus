"""只读比较原包与解压缓存，不执行旧代码或向旧目录释放文件。"""

from __future__ import annotations

import zipfile
import zlib
from pathlib import Path, PurePosixPath

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")


def _preview_file(name: str) -> bool:
    """识别用于预览的图片，不将其他纹理排除出本体比较。"""
    path = PurePosixPath(name.replace("\\", "/"))
    return path.suffix.lower() in IMAGE_SUFFIXES and (
        path.stem.lower().startswith("preview")
        or any(part.lower() in {"preview", "preview_screen"} for part in path.parts[:-1])
    )


def cache_entries(directory: Path, should_cancel) -> tuple[dict[str, tuple[int, int]], int]:
    """目录安全性先由共享指纹检查；比较本体，容量仍包含所有文件。"""
    entries = {}
    size_bytes = 0
    for path in sorted(directory.rglob("*")):
        if should_cancel():
            raise InterruptedError("旧版扫描已取消")
        if not path.is_file():
            continue
        name = path.relative_to(directory).as_posix()
        crc = 0
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                if should_cancel():
                    raise InterruptedError("旧版扫描已取消")
                size += len(chunk)
                crc = zlib.crc32(chunk, crc)
        size_bytes += size
        if not _preview_file(name):
            entries[name] = (size, crc & 0xFFFFFFFF)
    return entries, size_bytes


def archive_entries(path: Path) -> dict[str, tuple[int, int]]:
    """读取 ZIP/7z/RAR 成员清单；缺少可靠校验信息时不宣称缓存相同。"""
    with path.open("rb") as stream:
        header = stream.read(8)
    if header.startswith(b"PK"):
        with zipfile.ZipFile(path) as archive:
            members = [(item.filename, item.file_size, item.CRC) for item in archive.infolist() if not item.is_dir()]
    elif header.startswith(b"7z\xbc\xaf\x27\x1c"):
        import py7zr

        with py7zr.SevenZipFile(path) as archive:
            members = [
                (item.filename, item.uncompressed, item.crc32) for item in archive.list() if not item.is_directory
            ]
    elif header.startswith(b"Rar!"):
        import rarfile

        with rarfile.RarFile(path) as archive:
            members = [(item.filename, item.file_size, item.CRC) for item in archive.infolist() if not item.is_dir()]
    else:
        raise ValueError("无法识别原包格式")
    entries = {}
    for name, size, crc in members:
        name = name.replace("\\", "/")
        if _preview_file(name):
            continue
        if name in entries or crc is None or size is None:
            raise ValueError("原包成员校验信息不完整")
        entries[name] = (size, crc)
    return entries
