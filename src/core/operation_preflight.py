"""只读核对用户操作即将修改的精确对象。

不执行发现、修复、写入或删除；校验凭据可跨线程传递，不持有数据库或可变 Mod。
"""

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

from src.utils import fs
from src.utils.i18n import t as tr


class OperationPreflightError(OSError):
    """携带 Mod 身份、失败路径和可展示原因的操作前置检查异常。"""

    def __init__(self, mod_id, path, reason):
        """保存结构化失败信息并生成包含目标路径的错误描述。"""
        self.mod_id, self.path, self.reason = mod_id, str(path), str(reason)
        super().__init__(tr("本次操作失败：{reason}\n{path}", reason=reason, path=path))


def file_version(path):
    """读取非链接对象的设备、身份、大小和时间版本，链接及重解析目标会被拒绝。"""
    info = os.stat(fs.native_path(path), follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise OSError(tr("目标已变成链接，无法确认原文件"))
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def access_file(path, *, write=False, directory=False, pin=False):
    """打开现有目标验证实际访问权限，不创建或截断文件。

    短提交期间持有读取句柄以阻止源替换；移动和替换由各自事务负责回滚。
    """
    if os.name == "nt":
        from src.core.watcher_windows import _metadata_api, _NativeAPI

        api = _metadata_api(_NativeAPI)
        rights = (0x2 if write else 0x1) if directory else (0x80000000 | (0x40010000 if write else 0))
        handle = api.k.CreateFileW(fs.native_path(path), rights, 1 if pin else 7, None, 3, 0x02000000, None)
        if handle == api.c.c_void_p(-1).value:
            raise api.c.WinError(api.c.get_last_error())
        try:
            yield
        finally:
            api.k.CloseHandle(handle)
    elif directory:
        if not os.path.isdir(path):
            raise FileNotFoundError(str(path))
        if not os.access(path, os.W_OK if write else os.R_OK):
            raise PermissionError(str(path))
        yield
    else:
        with open(fs.native_path(path), "r+b" if write else "rb"):
            yield


def failure(mod_id, path, error):
    """将文件系统异常归类为缺失、离线、占用或权限原因，并包装为操作检查错误。"""
    if isinstance(error, OperationPreflightError):
        return error
    if isinstance(error, (FileNotFoundError, NotADirectoryError)):
        reason = tr("目标文件或目录不存在")
    elif getattr(error, "winerror", None) in (21, 53, 64, 67):
        reason = tr("所在磁盘或网络路径当前不可用")
    elif getattr(error, "winerror", None) in (32, 33):
        reason = tr("文件正被其他程序占用")
    elif isinstance(error, PermissionError):
        reason = tr("没有读取或修改所需文件的权限")
    else:
        reason = str(error)
    return OperationPreflightError(mod_id, path, reason)


@dataclass(frozen=True)
class SourceReceipt:
    """保存源文件的身份、摘要和版本凭据，供文件操作前后复核。"""

    mod_id: str
    path: str
    content_hash: str
    version: tuple
    marker_version: tuple

    def recheck(self):
        """按已保存凭据重新确认源身份与版本，跳过完整内容扫描。"""
        return confirm_source(self.path, self.mod_id, self.content_hash, full=False, expected=self)


def confirm_source(path, mod_id, content_hash="", *, full=True, expected=None):
    """核对精确的资源与标记，不恢复缺失的本体名称。"""
    path = Path(path)
    marker_path = path.parent / ".d3dxmod"
    try:
        initial = file_version(path)
        marker_version = file_version(marker_path)
        with access_file(path):
            with open(fs.native_path(marker_path), encoding="utf-8") as stream:
                marker = json.load(stream)
            from src.core.mod_package import CONTAINER_KIND

            if (
                not isinstance(marker, dict)
                or marker.get("kind") != CONTAINER_KIND
                or marker.get("mod_id") != mod_id
                or marker.get("body") != path.name
            ):
                raise ValueError(tr("源文件身份或位置已变化"))
            recorded_hash = marker.get("content_hash")
            if not recorded_hash or (content_hash and recorded_hash != content_hash):
                raise ValueError(tr("源文件版本与记录不一致"))
            if full:
                from src.models.mod import Mod

                if Mod.compute_sha1(path) != recorded_hash:
                    raise ValueError(tr("压缩包内容已变化"))
            receipt = SourceReceipt(mod_id, str(path), recorded_hash, initial, marker_version)
            if expected is not None and receipt != expected:
                raise ValueError(tr("操作期间源文件或身份说明已变化"))
            if file_version(path) != initial or file_version(marker_path) != marker_version:
                raise ValueError(tr("确认期间文件已变化"))
            return receipt
    except (OSError, ValueError, TypeError) as error:
        raise failure(mod_id, path, error) from error


def confirm_metadata(receipt, *, expected=None):
    """已有编辑目标必须存在，并满足读写权限要求。"""
    path = Path(receipt.path).parent / ".d3dxmeta" / "info.json"
    try:
        with access_file(path, write=True), access_file(path.parent, write=True, directory=True):
            pass
        with open(fs.native_path(path), encoding="utf-8") as stream:
            data = json.load(stream)
            from src.core.mod_package import MANIFEST_KIND

            if not isinstance(data, dict) or data.get("kind") != MANIFEST_KIND:
                raise ValueError(tr("Mod 信息格式已变化"))
            if expected is not None:
                if data.get("name") != expected.display_name:
                    raise ValueError(tr("文件中的 Mod 信息与当前记录不一致"))
                info = data.get("info", {})
                if not isinstance(info, dict):
                    raise ValueError(tr("Mod 信息格式已变化"))
                recorded = expected.to_dict()["info"]
                recorded.pop("name", None)
                if any(key not in info or value != info[key] for key, value in recorded.items()):
                    raise ValueError(tr("文件中的 Mod 信息与当前记录不一致"))
        return file_version(path)
    except (OSError, ValueError) as error:
        raise failure(receipt.mod_id, path, error) from error


def confirm_deployment(mod_id, path):
    """确认非空部署路径可写且标记属于目标 Mod，失败时转换为操作检查异常。"""
    if not path:
        return
    try:
        from src.core.mod_identity import read_deployment_identity

        with access_file(path, write=True, directory=True):
            marker = read_deployment_identity(path)
            if not marker or marker.get("mod_id") != mod_id:
                raise ValueError(tr("部署文件身份与记录不一致"))
    except (OSError, ValueError) as error:
        raise failure(mod_id, path, error) from error


def confirm_delete_target(mod):
    """源已缺失时可清理记录，被替换的其他身份不属于当前操作。"""
    body = Path(mod.path)
    container = body.parent
    try:
        try:
            file_version(container)
        except (FileNotFoundError, NotADirectoryError):
            return
        with open(fs.native_path(container / ".d3dxmod"), encoding="utf-8") as stream:
            marker = json.load(stream)
        if not isinstance(marker, dict) or marker.get("mod_id") != mod.mod_id:
            raise ValueError(tr("该位置已无法确认为此 Mod，未删除其中的文件"))
        if marker.get("body") != body.name:
            raise ValueError(tr("源文件位置已变化，未删除其中的文件"))
    except (OSError, ValueError) as error:
        raise failure(mod.mod_id, container, error) from error


def guarded_edit(method):
    """元数据写入和数据库发布期间保持源文件有效。"""

    @wraps(method)
    def wrapped(manager, target, *args, **kwargs):
        """校验元数据并固定源对象后执行编辑，文件访问或前置检查失败时报告并返回 False。"""
        mod_id = target.mod_id if hasattr(target, "mod_id") else target
        try:
            receipt = manager.require_mod_operation(mod_id, metadata=True)
            with access_file(receipt.path, pin=True):
                receipt.recheck()
                return method(manager, target, *args, **kwargs)
        except OperationPreflightError as error:
            manager.report_operation_failure(error)
            return False
        except OSError as error:
            manager.report_operation_failure(failure(mod_id, receipt.path, error))
            return False

    return wrapped
