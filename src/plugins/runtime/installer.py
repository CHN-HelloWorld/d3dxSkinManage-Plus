"""插件安装器（从 ZIP、7z、RAR 安装到 plugins/<id>/）。

支持根目录或单一顶层文件夹中的 plugin.json。先在临时目录解压并校验，
再替换已安装版本；文件来自用户本地选择的压缩包。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from src.plugins.runtime.manifest import ManifestError
from src.plugins.runtime.validation import validate_package
from src.services import archive as archive_service
from src.utils.constants import ARCHIVE_EXTENSIONS
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.paths import get_plugins_dir


class InstallError(Exception):
    """压缩包结构非法 / manifest 缺失 / 解压越界等。"""


@dataclass
class InstallResult:
    """描述插件安装后的身份、目录及覆盖前版本，供界面更新安装状态。"""

    plugin_id: str
    name: str
    version: str
    was_update: bool  # 目标已存在（覆盖安装）
    previous_version: str  # 覆盖前的版本（was_update 时有意义）
    directory: Path


def _find_manifest_prefix(names: list[str]) -> str:
    """返回 plugin.json 的目录前缀（空字符串为根目录）。"""
    # 根布局
    if "plugin.json" in names:
        return ""
    # 单一顶层文件夹布局
    candidates = [n for n in names if n.count("/") == 1 and n.endswith("/plugin.json")]
    if len(candidates) == 1:
        return candidates[0][: -len("plugin.json")]
    if len(candidates) > 1:
        raise InstallError(tr("压缩包内含多个 plugin.json，无法判定插件根目录"))
    raise InstallError(tr("压缩包内未找到 plugin.json"))


def _checked_member_name(name: str) -> str:
    """解压前拒绝绝对路径、目录穿越及 Windows 路径别名。"""
    relative = PurePosixPath(name.replace("\\", "/"))
    windows = PureWindowsPath(name)
    if (
        relative.is_absolute()
        or windows.drive
        or windows.is_reserved()
        or ".." in relative.parts
        or any(":" in part or part.endswith((" ", ".")) for part in relative.parts)
    ):
        raise InstallError(tr("检测到越界路径，已拒绝: {name}", name=name))
    return relative.as_posix()


def _extract_stream_archive(reader, entries, target: Path) -> str:
    """ZIP 和两种 RAR 后端均读取成员流，不创建归档内的文件链接。"""
    members = [(name, _checked_member_name(name), is_dir) for name, is_dir in entries]
    prefix = _find_manifest_prefix([relative for _, relative, is_dir in members if not is_dir])
    for name, relative, is_dir in members:
        if not relative.startswith(prefix):
            continue
        destination = target / relative
        if is_dir:
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with reader.open(name) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
    return prefix


def _unpack_archive(archive: Path, target: Path) -> str:
    """返回插件根前缀；所有成员在解压前完成路径校验。"""
    suffix = archive.suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(archive) as reader:
            return _extract_stream_archive(
                reader, [(info.filename, info.is_dir()) for info in reader.infolist()], target
            )
    if suffix == ".7z":
        if not archive_service.PY7ZR_AVAILABLE:
            raise InstallError(tr("无法解压 7z 插件包：缺少 py7zr"))
        with archive_service.py7zr.SevenZipFile(archive, "r") as reader:
            names = []
            for info in reader.files:
                name = _checked_member_name(info.filename)
                if info.is_symlink or info.is_junction or info.is_socket:
                    raise InstallError(tr("插件压缩包不支持链接或特殊文件: {name}", name=info.filename))
                if not info.is_directory:
                    names.append(name)
            prefix = _find_manifest_prefix(names)
            reader.extractall(path=target)
            return prefix
    if suffix == ".rar":
        reader, kind = archive_service.ArchiveService._rar_open(archive)
        if reader is None:
            raise InstallError(tr("无法解压 RAR 插件包：缺少可用的 RAR 解压后端"))
        try:
            return _extract_stream_archive(
                reader,
                [(info.filename, archive_service.ArchiveService._rar_is_dir(info, kind)) for info in reader.infolist()],
                target,
            )
        finally:
            if kind == "rarfile":
                reader.close()
    raise InstallError(tr("不支持的插件压缩格式: {suffix}（支持 ZIP、7z、RAR）", suffix=suffix))


def install_from_archive(
    archive_path: str | Path, plugins_dir: Path | None = None, *, before_replace: Callable[[str], None] | None = None
) -> InstallResult:
    """从 ZIP、7z 或 RAR 安装插件，返回 InstallResult；失败抛 InstallError。"""
    archive_path = Path(archive_path)
    if archive_path.suffix.lower() not in ARCHIVE_EXTENSIONS:
        raise InstallError(tr("不支持的插件压缩格式: {suffix}（支持 ZIP、7z、RAR）", suffix=archive_path.suffix))
    base = (plugins_dir or get_plugins_dir()).resolve()
    base.mkdir(parents=True, exist_ok=True)
    # 在同级暂存目录完成校验后再替换已安装插件。
    with tempfile.TemporaryDirectory(prefix=".install-", dir=base) as scratch:
        unpacked = Path(scratch) / "unpacked"
        staging = Path(scratch) / "new"
        unpacked.mkdir()
        try:
            prefix = _unpack_archive(archive_path, unpacked)
            (unpacked / prefix).rename(staging)
        except InstallError:
            raise
        except Exception as exc:
            raise InstallError(tr("插件压缩包读取失败: {error}", error=exc)) from exc
        try:
            mf = validate_package(staging)
        except (ManifestError, OSError) as exc:
            raise InstallError(tr("插件校验失败: {error}", error=exc)) from exc
        pid = mf.id
        target = (base / pid).resolve()
        if target.parent != base:
            raise InstallError(tr("插件目标目录越界: {plugin_id}", plugin_id=pid))
        was_update = target.exists()
        previous_version = ""
        if was_update:
            old_mf = target / "plugin.json"
            if old_mf.exists():
                try:
                    previous_version = str(json.loads(old_mf.read_text("utf-8")).get("version", ""))
                except Exception:
                    pass
        # 回滚失败时保留恢复文件，避免被临时目录清理删除。
        backup = base / f".{pid}.backup-{uuid.uuid4().hex}"
        if before_replace is not None:
            before_replace(pid)
        try:
            if was_update:
                target.rename(backup)
            try:
                staging.rename(target)
            except OSError:
                if backup.exists():
                    try:
                        backup.rename(target)
                    except OSError as exc:
                        raise InstallError(
                            tr("替换和恢复失败；旧版本保留在 {backup}: {error}", backup=backup, error=exc)
                        ) from exc
                raise
        except OSError as exc:
            raise InstallError(tr("替换插件失败，已保留旧版本: {error}", error=exc)) from exc
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)

    logger.info("插件已安装: %s v%s -> %s", mf.name, mf.version, target)
    return InstallResult(
        plugin_id=mf.id,
        name=mf.name,
        version=mf.version,
        was_update=was_update,
        previous_version=previous_version,
        directory=target,
    )
