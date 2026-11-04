"""事务化移除预览引用与缓存，保留 Mod 资源供重建索引恢复。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from src.core.library_snapshot import library_mutation, path_key
from src.core.mod_package import read_sidecar
from src.core.operation_preflight import access_file, confirm_metadata, failure, file_version
from src.utils import fs
from src.utils.constants import IMAGE_EXTENSIONS
from src.utils.i18n import t as tr
from src.utils.logger import logger


def _remember_directories(path, root, directories):
    """拒绝受管目录内部各层级的链接。"""
    relative = Path(path).relative_to(root)
    current = Path(root)
    for part in ("", *relative.parts):
        if part:
            current /= part
        version = file_version(current)
        if not stat.S_ISDIR(os.stat(fs.native_path(current)).st_mode):
            raise NotADirectoryError(str(current))
        directories[current] = version[:2]


def _check_directories(directories):
    """逐一核对预览相关目录的设备与文件身份，任一目录被替换时中止。"""
    for path, identity in directories.items():
        if file_version(path)[:2] != identity:
            raise OSError(tr("预览所在目录已变化：{path}", path=path))


def _check_writable(path):
    """确认目标为可写普通文件且父目录可写，访问检查前后版本必须一致。"""
    version = file_version(path)
    if not stat.S_ISREG(os.stat(fs.native_path(path), follow_symlinks=False).st_mode):
        raise OSError(tr("预览操作目标不是普通文件：{path}", path=path))
    with access_file(path, write=True), access_file(path.parent, write=True, directory=True):
        pass
    if file_version(path) != version:
        raise OSError(tr("预览操作目标已变化：{path}", path=path))
    return version


def _stage(target, temporaries, *, data):
    """在元数据目录暂存完整 JSON 字节并刷新磁盘，登记文件供事务清理。"""
    descriptor, name = tempfile.mkstemp(prefix=".preview-edit-", suffix=".tmp", dir=fs.native_path(target.parent))
    path = Path(fs.normal_path(name))
    temporaries.append(path)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _json_bytes(data):
    """将数据序列化为保留中文的缩进 UTF-8 JSON 字节。"""
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf8")


@dataclass
class _Change:
    """记录预览文件替换或删除的原始版本、暂存文件和回滚备份。"""

    path: Path
    version: tuple
    staged: Path | None = None
    backup: Path | None = None
    installed: tuple | None = None

    def apply(self, directories):
        """核对目录与原文件版本，保留回滚备份后安装元数据或移走缓存。"""
        _check_directories(directories)
        if file_version(self.path) != self.version:
            raise OSError(tr("删除前文件已变化：{path}", path=self.path))
        backup = self.path.with_name(".preview-backup-" + uuid.uuid4().hex + ".bak")
        os.replace(fs.native_path(self.path), fs.native_path(backup))
        self.backup = backup
        if file_version(backup)[:4] != self.version[:4]:
            raise OSError(tr("删除期间文件已变化：{path}", path=self.path))
        if self.staged is not None:
            os.replace(fs.native_path(self.staged), fs.native_path(self.path))
            self.installed = file_version(self.path)

    def rollback(self, directories):
        """在目录与安装版本仍一致时恢复备份，原位置被外部占用则保留原件并报错。"""
        if self.backup is None:
            return
        _check_directories(directories)
        if os.path.lexists(fs.native_path(self.path)):
            if self.installed is None or file_version(self.path) != self.installed:
                raise OSError(tr("新文件已占用原位置，原件保留于：{backup}", backup=self.backup))
            os.unlink(fs.native_path(self.path))
        os.replace(fs.native_path(self.backup), fs.native_path(self.path))
        self.backup = None


@library_mutation
def delete_preview_files(manager, mod_id, paths) -> bool:
    """同步移除预览引用与实例缓存，任何格式的资源本体和补充原图均保留。"""
    changes, temporaries, directories = [], [], {}
    token = "preview-remove:" + uuid.uuid4().hex
    target = ""
    committed = False
    try:
        original = manager.get_mod(mod_id)
        if original is None or original.game_id != manager.current_game_id:
            raise ValueError(tr("Mod 记录已不存在或游戏已切换"))
        target = original.path
        receipt = manager.require_mod_operation(mod_id, metadata=True, full=True)
        source = Path(receipt.path)
        container = source.parent
        cache = Path(manager._index.paths.preview_roots[original.game_id]) / mod_id
        marker, metadata = container / ".d3dxmod", container / ".d3dxmeta/info.json"
        known = {path_key(path): Path(path) for path in original.preview_images}
        selected = []
        for value in dict.fromkeys(path_key(str(path)) for path in paths):
            if value not in known:
                raise ValueError(tr("所选图片已不在此 Mod 的预览列表中"))
            path = known[value]
            root = container if path.is_relative_to(container) else cache
            if not path.is_relative_to(root) or path.suffix.lower() not in IMAGE_EXTENSIONS:
                raise ValueError(tr("预览不属于此 Mod 容器或专属缓存：{path}", path=path))
            _remember_directories(path.parent, root, directories)
            selected.append(path)
            if path.is_relative_to(cache):
                changes.append(_Change(path, _check_writable(path)))
        if not selected:
            return True
        _remember_directories(metadata.parent, container, directories)
        metadata_version = confirm_metadata(receipt, expected=original)
        metadata_bytes = metadata.read_bytes()
        if file_version(metadata) != metadata_version:
            raise OSError(tr("预览信息文件已变化"))
        receipt.recheck()

        # 开始暂存前已检查全部目标文件和目录。
        manager.protect_library_operation(token, paths=(source, *selected), mod_ids=(mod_id,))
        manager.invalidate_operation_trust()
        updated = deepcopy(original)
        selected_keys = {path_key(str(path)) for path in selected}
        updated.preview_images = [path for path in original.preview_images if path_key(path) not in selected_keys]
        info = json.loads(metadata_bytes)
        local = info.setdefault("local", {})
        if not isinstance(local, dict):
            raise ValueError(tr("预览元数据格式已变化"))
        local["preview_references"] = [
            manager._index._image_reference(path, container, updated) for path in updated.preview_images
        ]
        # 显式选择优先于自动扫描，重建索引时才恢复自动识别图片。
        local["preview_selection_locked"] = True
        metadata_change = _Change(metadata, metadata_version, _stage(metadata, temporaries, data=_json_bytes(info)))
        changes.append(metadata_change)
        receipt.recheck()
        current = manager.get_mod(mod_id)
        if manager.current_game_id != original.game_id or current is None or current.to_dict() != original.to_dict():
            raise OSError(tr("准备删除预览期间 Mod 记录已变化"))
        # 数据库提交前保持最终文件句柄有效。
        with ExitStack() as pins:
            with manager._index.transaction():
                for change in changes:
                    change.apply(directories)
                confirm_metadata(receipt, expected=updated)
                for path in (source, marker, metadata):
                    pins.enter_context(access_file(path, pin=True))
                _check_directories(directories)
                receipt.recheck()
                if file_version(metadata) != metadata_change.installed:
                    raise OSError(tr("提交前预览信息已变化"))
                for change in changes:
                    if change.staged is None and os.path.lexists(fs.native_path(change.path)):
                        raise OSError(tr("删除位置已被新文件占用：{path}", path=change.path))
                if not manager._index.update_mod(updated, commit=False):
                    raise OSError(tr("预览记录保存失败"))
            committed = True
        manager._emit("mod_metadata_changed", mod_id=mod_id, game_id=original.game_id)
        return True
    except Exception as error:
        if committed:
            logger.exception("预览移除已提交，后续通知失败")
            return True
        for change in reversed(changes):
            try:
                change.rollback(directories)
            except OSError as rollback_error:
                logger.error("预览移除回滚未完成，保留原件备份：%s", rollback_error)
        manager.report_operation_failure(failure(mod_id, target, error))
        return False
    finally:
        manager.release_library_operation(token)
        cleanup = temporaries + ([change.backup for change in changes if change.backup] if committed else [])
        for path in cleanup:
            try:
                fs.unlink(path, missing_ok=True)
            except OSError as error:
                logger.warning("预览事务临时文件待清理 %s: %s", path, error)


@contextmanager
def reset_preview_selection(manager, expected_sidecars):
    """随重建索引事务清除移除记录；数据库提交失败时恢复原元数据。"""
    changes, temporaries, directories = [], [], {}
    committed = False
    try:
        for mod_id, expected in expected_sidecars.items():
            local = (expected or {}).get("local") or {}
            if not (local.get("preview_selection_locked") or local.get("excluded_archive_previews")):
                continue
            mod = manager.get_mod(mod_id)
            container = Path(mod.path).parent
            metadata = container / ".d3dxmeta/info.json"
            _remember_directories(metadata.parent, container, directories)
            version = _check_writable(metadata)
            if read_sidecar(mod.path) != expected or file_version(metadata) != version:
                raise OSError(tr("预览信息文件已变化"))
            updated = deepcopy(expected)
            local = updated["local"]
            local.pop("preview_selection_locked", None)
            local.pop("excluded_archive_previews", None)
            local["preview_references"] = [
                manager._index._image_reference(path, container, mod) for path in mod.preview_images
            ]
            change = _Change(metadata, version, _stage(metadata, temporaries, data=_json_bytes(updated)))
            changes.append(change)
            change.apply(directories)
        yield
        committed = True
    finally:
        if not committed:
            for change in reversed(changes):
                try:
                    change.rollback(directories)
                except OSError as error:
                    logger.error("预览恢复回滚未完成，保留原件备份：%s", error)
        for path in temporaries + ([change.backup for change in changes if change.backup] if committed else []):
            try:
                fs.unlink(path, missing_ok=True)
            except OSError as error:
                logger.warning("预览事务临时文件待清理 %s: %s", path, error)
