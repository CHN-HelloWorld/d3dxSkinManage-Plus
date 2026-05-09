"""删除明确选择的预览，保留资源身份和回滚材料。"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import uuid
import zipfile
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from src.core.library_snapshot import library_mutation, path_key
from src.core.operation_preflight import access_file, confirm_metadata, confirm_source, failure, file_version
from src.core.preview_cache_index import CACHE_FINGERPRINT
from src.models.mod import Mod
from src.services.archive import ArchiveService
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


def _stage(target, temporaries, *, data=None, suffix=".tmp"):
    """在目标所在目录创建并登记临时文件，可写入指定字节并刷新磁盘。"""
    descriptor, name = tempfile.mkstemp(prefix=".preview-edit-", suffix=suffix, dir=fs.native_path(target.parent))
    path = Path(fs.normal_path(name))
    temporaries.append(path)
    with os.fdopen(descriptor, "wb") as stream:
        if data is not None:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    return path


def _json_bytes(data):
    """将数据序列化为保留中文的缩进 UTF-8 JSON 字节。"""
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf8")


def _matching_zip_members(source, cache, selected):
    """仅以唯一的缓存相对名称和相同内容确认来源。"""
    matched = []
    with zipfile.ZipFile(fs.native_path(source)) as archive:
        infos = archive.infolist()
        # ZIP 按名称写入可能将重复成员覆盖为最后一项的内容，因此保留这类归档。
        if len({info.filename for info in infos}) != len(infos):
            return []
        candidates = {}
        for info in infos:
            name = info.filename.replace("\\", "/")
            parts = name.split("/")
            if (
                info.is_dir()
                or not name
                or any(part in ("", ".", "..") for part in parts)
                or Path(name).is_absolute()
                or Path(name).drive
                or ".d3dxmeta" in parts
            ):
                continue
            candidates.setdefault(path_key(str(cache / name)), []).append(info)
        for path in selected:
            if not path.is_relative_to(cache):
                continue
            infos = candidates.get(path_key(str(path)), ())
            if len(infos) != 1:
                continue
            info = infos[0]
            if path.stat().st_size != info.file_size:
                continue
            with archive.open(info) as member, open(fs.native_path(path), "rb") as local:
                while True:
                    data = member.read(1024 * 1024)
                    if data != local.read(1024 * 1024):
                        break
                    if not data:
                        matched.append(info.filename)
                        break
    return matched


def _prepare_zip(source, receipt, staged_source, members):
    """仅执行文件工作，可由界面后台检查器运行。"""
    with access_file(source, pin=True):
        receipt.recheck()
        shutil.copy2(fs.native_path(source), fs.native_path(staged_source))
    if not ArchiveService().remove_files_from_zip(staged_source, members):
        raise OSError(tr("无法从临时 ZIP 删除所选预览，原文件未改变"))
    version = file_version(staged_source)
    content_hash = Mod.compute_sha1(staged_source)
    if file_version(staged_source) != version:
        raise OSError(tr("准备后的 ZIP 已变化"))
    return content_hash, version


@dataclass
class _Change:
    """记录预览文件替换或删除的原始版本、暂存文件和回滚备份。"""

    path: Path
    version: tuple
    staged: Path | None = None
    backup: Path | None = None
    installed: tuple | None = None
    staged_version: tuple | None = None

    def apply(self, directories):
        """核对目录与源暂存版本，将原件移至备份后安装替换文件，并记录安装版本。"""
        _check_directories(directories)
        if file_version(self.path) != self.version:
            raise OSError(tr("删除前文件已变化：{path}", path=self.path))
        if self.staged_version is not None and file_version(self.staged) != self.staged_version:
            raise OSError(tr("准备后的替换文件已变化：{path}", path=self.path))
        backup = self.path.with_name(".preview-backup-" + uuid.uuid4().hex + ".bak")
        os.replace(fs.native_path(self.path), fs.native_path(backup))
        self.backup = backup
        if file_version(backup)[:4] != self.version[:4]:
            raise OSError(tr("删除期间文件已变化：{path}", path=self.path))
        if self.staged is not None:
            os.replace(fs.native_path(self.staged), fs.native_path(self.path))
            self.installed = file_version(self.path)
            if self.staged_version is not None and self.installed[:4] != self.staged_version[:4]:
                raise OSError(tr("替换期间文件已变化：{path}", path=self.path))

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
    """校验完整选择并暂存变更，再一起发布文件和数据库。

    修改 ZIP 需要实际成员证据，其他格式及无来源关联的图片不改写源归档。
    """
    changes, temporaries, directories = [], [], {}
    token = "preview-delete:" + uuid.uuid4().hex
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
            changes.append(_Change(path, _check_writable(path)))
        if not selected:
            return True
        _remember_directories(metadata.parent, container, directories)
        metadata_version = confirm_metadata(receipt, expected=original)
        metadata_bytes = metadata.read_bytes()
        marker_bytes = marker.read_bytes()
        if file_version(metadata) != metadata_version:
            raise OSError(tr("预览信息文件已变化"))
        receipt.recheck()
        runner = getattr(manager, "operation_check_runner", None)

        def match_members():
            """按当前源归档和缓存索引匹配所选预览对应的压缩成员。"""
            return _matching_zip_members(source, cache, selected)

        members = (
            (runner(match_members) if runner is not None else match_members())
            if source.suffix.lower() == ".zip"
            else []
        )
        extra_changes = []
        fingerprint = cache / CACHE_FINGERPRINT
        if members:
            _check_writable(source)
            _check_writable(marker)
            if os.path.lexists(fs.native_path(fingerprint)):
                _remember_directories(fingerprint.parent, cache, directories)
                extra_changes.append(_Change(fingerprint, _check_writable(fingerprint)))
        # 开始暂存前已检查全部目标文件和目录。
        manager.protect_library_operation(token, paths=(source, *selected), mod_ids=(mod_id,))
        manager.invalidate_operation_trust()
        updated = deepcopy(original)
        selected_keys = {path_key(str(path)) for path in selected}
        updated.preview_images = [path for path in original.preview_images if path_key(path) not in selected_keys]
        if members:
            staged_source = _stage(source, temporaries, suffix=".zip")

            def prepare_source():
                """依据已确认源凭据生成删除所选成员后的临时 ZIP 文件。"""
                return _prepare_zip(source, receipt, staged_source, members)

            updated.content_hash, staged_version = runner(prepare_source) if runner is not None else prepare_source()
            new_marker = json.loads(marker_bytes)
            new_marker["content_hash"] = updated.content_hash
            changes[:0] = [
                _Change(source, receipt.version, staged_source, staged_version=staged_version),
                _Change(marker, receipt.marker_version, _stage(marker, temporaries, data=_json_bytes(new_marker))),
            ]
        info = json.loads(metadata_bytes)
        local = info.setdefault("local", {})
        if not isinstance(local, dict):
            raise ValueError(tr("预览元数据格式已变化"))
        local["preview_references"] = [
            manager._index._image_reference(path, container, updated) for path in updated.preview_images
        ]
        # 不能改写源归档时记录缓存相对名称，防止清空或重建缓存后重新出现已删除图片。
        deleted_members = [path.relative_to(cache).as_posix() for path in selected if path.is_relative_to(cache)]
        if deleted_members:
            local["excluded_archive_previews"] = list(
                dict.fromkeys([*local.get("excluded_archive_previews", []), *deleted_members])
            )
        metadata_change = _Change(metadata, metadata_version, _stage(metadata, temporaries, data=_json_bytes(info)))
        changes.append(metadata_change)
        changes.extend(extra_changes)
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
                if members:
                    if file_version(source) != changes[0].installed or file_version(marker) != changes[1].installed:
                        raise OSError(tr("提交前源文件或身份说明已变化"))
                    # 工作线程已验证内容，安装后文件身份未变；短事务中只重新读取标记。
                    confirm_source(source, mod_id, updated.content_hash, full=False)
                else:
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
            logger.exception("预览删除已提交，后续通知失败")
            return True
        for change in reversed(changes):
            try:
                change.rollback(directories)
            except OSError as rollback_error:
                logger.error("预览删除回滚未完成，保留原件备份：%s", rollback_error)
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
