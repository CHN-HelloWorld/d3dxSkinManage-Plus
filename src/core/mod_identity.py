"""独立于路径和可重建索引记录的持久 Mod 身份。"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from src.utils import fs
from src.utils.constants import MIGOTO_DISABLED_PREFIX, MOD_CONTAINER_MARKER
from src.utils.i18n import t as tr

DEPLOYMENT_IDENTITY_FILE = MOD_CONTAINER_MARKER
DEPLOYMENT_IDENTITY_KIND = "d3dxSkinManage-Plus.deployment"


class IdentityConflict(ValueError):
    """持久身份缺失、无效、重复或已归其他对象所有。"""


def validate_mod_id(value: str) -> str:
    """验证身份为小写 UUID4 十六进制文本，不符合要求时抛出身份冲突异常。"""
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as error:
        raise IdentityConflict(tr("无效的 mod_id 身份：{value!r}", value=value)) from error
    if parsed.version != 4 or parsed.hex != value:
        raise IdentityConflict(tr("mod_id 必须是小写 UUID4 hex：{value!r}", value=value))
    return value


def new_mod_id(used=()) -> str:
    """生成不在已有身份集合中的随机 UUID4 编号。"""
    occupied = set(used)
    while True:
        candidate = uuid.uuid4().hex
        if candidate not in occupied:
            return candidate


def read_source_mod_id(body: str | Path) -> str:
    """从正文或容器路径读取源标记并核对正文名称，缺失或损坏时拒绝身份认领。"""
    from src.core.mod_package import InvalidManagedMetadataError, read_container_marker

    path = Path(fs.normal_path(body))
    container = path if fs.is_file(path / MOD_CONTAINER_MARKER) else path.parent
    try:
        marker = read_container_marker(container)
    except InvalidManagedMetadataError as error:
        raise IdentityConflict(tr("源 Mod 身份标记损坏：{container}", container=container)) from error
    if marker is None or (path != container and marker["body"] != path.name):
        raise IdentityConflict(tr("源 Mod 缺少 .d3dxmod 身份标记：{path}", path=path))
    return validate_mod_id(marker.get("mod_id"))


def read_deployment_identity(directory: str | Path) -> dict | None:
    """解析并验证部署身份标记，缺失或属于源容器标记时返回 None。"""
    from src.core.mod_package import CONTAINER_KIND

    marker = Path(directory) / DEPLOYMENT_IDENTITY_FILE
    try:
        if os.name == "nt":
            from src.core.watcher_windows import read_marker_text

            text = read_marker_text(marker)
        else:
            text = fs.read_text(marker, encoding="utf-8")
        data = json.loads(text)
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError) as error:
        raise IdentityConflict(tr("部署身份元数据损坏：{marker}", marker=marker)) from error
    if isinstance(data, dict) and data.get("kind") == CONTAINER_KIND:
        return None
    if not isinstance(data, dict) or data.get("kind") != DEPLOYMENT_IDENTITY_KIND:
        raise IdentityConflict(tr("部署身份元数据格式错误：{marker}", marker=marker))
    validate_mod_id(data.get("mod_id"))
    if not isinstance(data.get("content_hash"), str):
        raise IdentityConflict(tr("部署身份缺少 content_hash：{marker}", marker=marker))
    return data


def write_deployment_identity(directory: str | Path, mod_id: str, content_hash: str = "") -> None:
    """校验身份后将部署标记写入同目录临时文件，刷新磁盘并原子替换目标。"""
    validate_mod_id(mod_id)
    data = {"kind": DEPLOYMENT_IDENTITY_KIND, "mod_id": mod_id, "content_hash": content_hash}
    root = Path(directory)
    fs.ensure_dir(root)
    fd, temporary = tempfile.mkstemp(prefix=".identity-", suffix=".tmp", dir=fs.native_path(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, fs.native_path(root / DEPLOYMENT_IDENTITY_FILE))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class DeploymentLocation:
    """保存部署对象的持久身份、实际目录及内容摘要。"""

    mod_id: str
    path: Path
    content_hash: str

    @property
    def active(self) -> bool:
        """根据目录是否带禁用前缀判断该部署当前是否处于活动状态。"""
        return not self.path.name.upper().startswith(MIGOTO_DISABLED_PREFIX.upper())


def scan_deployment_identities(roots, *, should_cancel=None) -> dict[str, DeploymentLocation]:
    """单次扫描部署目录的直接子项，按身份判断归属。

    根目录缺失或离线时抛出异常，不返回伪造的空清单；暂存和更新目录不作为已发布部署。
    """
    result: dict[str, DeploymentLocation] = {}
    visited: set[str] = set()
    for root in roots:
        if should_cancel is not None and should_cancel():
            raise InterruptedError("Deployment probe cancelled")
        root = Path(root)
        key = os.path.normcase(os.path.abspath(root))
        if key in visited:
            continue
        visited.add(key)
        for child in root.iterdir():
            if should_cancel is not None and should_cancel():
                raise InterruptedError("Deployment probe cancelled")
            name = child.name.casefold()
            if name.startswith(".") or name.endswith(".partial") or name.startswith("disabled_.update_"):
                continue
            if not stat.S_ISDIR(os.stat(fs.native_path(child)).st_mode):
                continue
            marker = read_deployment_identity(child)
            if marker is None:
                continue
            identity = marker["mod_id"]
            if identity in result:
                raise IdentityConflict(
                    tr("mod_id 身份重复：{path} 和 {child}", path=result[identity].path, child=child)
                )
            result[identity] = DeploymentLocation(identity, child, marker["content_hash"])
    return result
