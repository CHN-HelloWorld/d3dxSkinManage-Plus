"""展开归档内的受管 Mod 容器，使配置读取和物理部署采用同一文件结构。"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path

from src.core.mod_package import (
    INI_OVERRIDES_SUBDIR,
    META_DIR,
    _ini_stat_token,
    _strict_ini_tree,
    read_container_marker,
)
from src.services.archive import ArchiveService
from src.utils import fs
from src.utils.constants import MOD_CONTAINER_MARKER, is_resident_archive
from src.utils.i18n import t as tr


def archive_has_containers(entries) -> bool:
    """按归档成员名识别容器标记，不将普通的嵌套压缩包视为 Mod 容器。"""
    return any(Path(name.replace("\\", "/")).name.casefold() == MOD_CONTAINER_MARKER for name, *_ in entries)


def _checked_path(path: Path, root: Path) -> Path:
    """将所有移动、覆盖和删除限制在明确指定的展开目录内。"""
    if not path.resolve().is_relative_to(root):
        raise OSError(tr("Mod 文件指向了展开目录之外，已停止操作，请检查压缩包内容。"))
    return path


def _directories(directory: Path, root: Path):
    """遍历普通子目录，拒绝链接和重解析目录，不进入管理器元数据。"""
    for child in fs.list_children(directory):
        if child.name.casefold() == META_DIR:
            continue
        info = os.lstat(fs.native_path(child))
        if stat.S_ISLNK(info.st_mode):
            if fs.is_dir(child):
                raise OSError(tr("Mod 中有无法安全展开的文件链接，请检查压缩包内容。"))
            continue
        if stat.S_ISDIR(info.st_mode):
            if getattr(info, "st_file_attributes", 0) & 0x400:
                raise OSError(tr("Mod 中有无法安全展开的文件链接，请检查压缩包内容。"))
            yield _checked_path(child, root)


def tree_has_containers(directory: Path) -> bool:
    """检查旧部署中是否仍有未展开的源 Mod 容器，部署身份标记不算源容器。"""
    root = directory.resolve()

    def visit(path):
        """在普通目录树中查找可识别的源容器。"""
        if fs.is_file(path / MOD_CONTAINER_MARKER) and read_container_marker(path) is not None:
            return True
        return any(visit(child) for child in _directories(path, root))

    return visit(directory)


def expand_container_tree(directory: Path, *, should_cancel=None) -> None:
    """在独立展开目录内展开嵌套本体、叠加各自配置并清理所有层级的管理器元数据。"""
    root = directory.resolve()
    service = ArchiveService()

    def visit(path, nesting=0):
        """先在临时目录准备完整本体，再迁入当前容器并递归处理其子目录。"""
        if should_cancel is not None and should_cancel():
            raise InterruptedError("nested deployment cancelled")
        marker = read_container_marker(path) if fs.is_file(path / MOD_CONTAINER_MARKER) else None
        if marker is not None:
            if nesting >= 32:
                raise OSError(tr("Mod 压缩包嵌套层数过多，已停止展开，请检查压缩包内容。"))
            body = _checked_path(path / marker["body"], root)
            if not fs.is_file(body) or not is_resident_archive(body):
                raise OSError(tr("压缩包内的 Mod 本体不完整，请使用完整的 Mod 压缩包重试。"))
            temporary = _checked_path(path / (".nested-" + uuid.uuid4().hex), root)
            try:
                extracted = service.extract(body, temporary, should_cancel=should_cancel)
                if extracted is None:
                    raise OSError(tr("压缩包内的 Mod 未能完整解压，请检查压缩包后重试。"))
                extracted = _checked_path(Path(extracted), root)
                overrides = path / META_DIR / INI_OVERRIDES_SUBDIR
                if fs.is_dir(overrides):
                    for relative, _token in _strict_ini_tree(overrides):
                        source = _checked_path(overrides / relative, root)
                        target = _checked_path(extracted / relative, root)
                        fs.ensure_dir(target.parent)
                        shutil.copy2(fs.native_path(source), fs.native_path(target))
                visit(extracted, nesting + 1)
                children = fs.list_children(extracted)
                for child in children:
                    target = _checked_path(path / child.name, root)
                    if fs.lexists(target):
                        raise FileExistsError(tr("Mod 展开后出现同名文件，为避免覆盖已有内容，操作已停止。"))
                for child in children:
                    target = _checked_path(path / child.name, root)
                    os.rename(fs.native_path(_checked_path(child, root)), fs.native_path(target))
                fs.unlink(body)
                fs.unlink(_checked_path(path / MOD_CONTAINER_MARKER, root))
            finally:
                if fs.exists(temporary):
                    fs.rmtree(_checked_path(temporary, root))
        for child in fs.list_children(path):
            if child.name.casefold() == META_DIR:
                fs.rmtree(_checked_path(child, root))
        for child in list(_directories(path, root)):
            visit(child, nesting)

    visit(directory)


@lru_cache(maxsize=32)
def read_nested_archive_inis(path: Path, version: tuple) -> tuple[tuple[str, str], ...]:
    """按原包版本缓存完整展开后的 INI，原包和其中的用户覆盖配置始终保持只读。"""
    temporary = Path(tempfile.mkdtemp(prefix="mod-nested-archive-"))
    temporary_root = Path(tempfile.gettempdir()).resolve()
    _checked_path(temporary, temporary_root)
    try:
        extracted = ArchiveService().extract(path, temporary)
        if extracted is None:
            raise OSError(tr("无法完整读取 Mod 压缩包，请检查压缩包后重试。"))
        extracted = _checked_path(Path(extracted), temporary.resolve())
        expand_container_tree(extracted)
        result = tuple(
            (relative, fs.read_bytes(extracted / relative).decode("utf-8", errors="surrogateescape"))
            for relative, _token in _strict_ini_tree(extracted, exclude_meta=True)
        )
        if _ini_stat_token(os.stat(fs.native_path(path))) != version:
            raise OSError(tr("Mod 文件在读取时发生变化，请等待文件复制或修改完成后重试。"))
        return result
    finally:
        fs.rmtree(_checked_path(temporary, temporary_root))
