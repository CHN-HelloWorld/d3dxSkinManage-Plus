"""共享标签原子编辑的独立文件步骤。

边界只传递值和文件句柄，数据库连接、路径解析器和活动模型由所有者保留。
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, fields
from pathlib import Path

from src.core.mod_package import (
    MANIFEST_NAME,
    META_DIR,
    _validate_manifest,
    build_local_manifest,
    unknown_manifest_keys,
    write_sidecar,
)
from src.core.operation_preflight import (
    OperationPreflightError,
    SourceReceipt,
    access_file,
    confirm_metadata,
    confirm_source,
    failure,
    file_version,
)
from src.utils import fs
from src.utils.i18n import t as tr
from src.utils.logger import logger


@dataclass(frozen=True)
class TagFileTarget:
    """冻结标签批量写回所需的 Mod 路径、身份和序列化描述信息。"""

    mod_id: str
    path: str
    content_hash: str
    display_name: str
    info_json: str

    @classmethod
    def from_mod(cls, mod):
        """在所有者线程采集元数据校验所需的冻结视图。"""
        info = {field.name: getattr(mod.info, field.name) for field in fields(mod.info)}
        info["age_rating"] = mod.info.age_rating.value
        return cls(mod.mod_id, mod.path, mod.content_hash, mod.display_name, json.dumps(info, ensure_ascii=False))

    def to_dict(self):
        """解析冻结的描述 JSON，生成旁置文件写入所需的元数据结构。"""
        return {"info": json.loads(self.info_json)}


@dataclass(frozen=True)
class TagFileSnapshot:
    """保存标签批量文件写入前的目标、原始内容和版本凭据。"""

    receipt: SourceReceipt
    path: str
    data: bytes
    version: tuple


def check_tag_files(targets, *, full, previous, pins):
    """核对全部写入目标，并在数据库发布期间持有源句柄。"""
    snapshots = {}
    for target in targets:
        path = Path(target.path).parent / META_DIR / MANIFEST_NAME
        try:
            receipt = confirm_source(target.path, target.mod_id, target.content_hash, full=full)
            version = confirm_metadata(receipt, expected=target)
            opened = previous.get(target.mod_id)
            if opened is not None and (receipt != opened[0] or version != opened[1]):
                raise OperationPreflightError(target.mod_id, receipt.path, tr("打开编辑窗口后文件已变化"))
            pins.enter_context(access_file(receipt.path, pin=True))
            receipt.recheck()
            data = fs.read_bytes(path)
            if file_version(path) != version:
                raise OperationPreflightError(target.mod_id, path, tr("确认期间文件已变化"))
            snapshots[target.mod_id] = TagFileSnapshot(receipt, str(path), data, version)
        except (OSError, ValueError) as error:
            raise failure(target.mod_id, path, error) from error
    return snapshots


def tag_sidecar_payload(snapshot, metadata, preview_references):
    """使用已检查的内容构造完整清单，保留扩展信息。"""
    existing = _validate_manifest(json.loads(snapshot.data))
    local = dict(existing.get("local") or {})
    for key in ("preview_order", "classification", "status_hint"):
        local.pop(key, None)
    local["preview_references"] = preview_references
    metadata = dict(metadata, info={**existing.get("info", {}), **metadata["info"]})
    manifest = build_local_manifest(metadata, local, extra=unknown_manifest_keys(existing))
    return json.dumps(manifest, ensure_ascii=False)


def write_tag_files(writes, written):
    """写入有限批次，成功后所有者才发布数据库变更。"""
    for snapshot, target, preview_json in writes:
        receipt = snapshot.receipt
        try:
            metadata = dict(target.to_dict(), name=target.display_name)
            payload = tag_sidecar_payload(snapshot, metadata, json.loads(preview_json))
            receipt.recheck()
            if file_version(snapshot.path) != snapshot.version:
                raise OperationPreflightError(receipt.mod_id, snapshot.path, tr("保存前 Mod 信息已变化"))
            if not write_sidecar(receipt.path, json.loads(payload)):
                raise OperationPreflightError(receipt.mod_id, snapshot.path, tr("Mod 信息写入失败"))
            written.append(snapshot)
        except (OSError, ValueError) as error:
            raise failure(receipt.mod_id, snapshot.path, error) from error


def restore_tag_files(written):
    """恢复原内容，仅返回实际恢复成功的文件凭据。"""
    restored = {}
    for snapshot in reversed(written):
        temporary = None
        try:
            snapshot.receipt.recheck()
            path = Path(snapshot.path)
            temporary = path.with_name(".info-restore-" + uuid.uuid4().hex + ".tmp")
            fs.write_bytes(temporary, snapshot.data)
            os.replace(fs.native_path(temporary), fs.native_path(path))
            version = file_version(path)
            if fs.read_bytes(path) != snapshot.data or file_version(path) != version:
                raise OSError(f"回滚后的元数据已被修改: {path}")
            restored[snapshot.receipt.mod_id] = (snapshot.receipt, version)
        except OSError as error:
            logger.error("恢复编辑元数据失败: %s", error)
        finally:
            if temporary is not None:
                try:
                    fs.unlink(temporary, missing_ok=True)
                except OSError as error:
                    logger.error("清理编辑元数据回滚临时文件失败: %s", error)
    return restored
