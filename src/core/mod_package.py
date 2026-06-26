"""管理 Mod 的可分享元数据与容器本地元数据。

分享数据包含显示名称、作者、描述、标签、年龄分级和手动补充预览图，
随导出产物写入 .d3dxmeta/info.json 与 .d3dxmeta/previews/。本体原始预览不重复携带。
导入时先读取并暂存补充图片，再移除分享数据，使资源内容哈希和部署内容保持一致。

模块通过对象属性及序列化接口读取 Mod 信息，不依赖界面库或 Mod 模型类型。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
import zlib
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.utils import fs
from src.utils.constants import IMAGE_EXTENSIONS, MOD_CONTAINER_MARKER, is_resident_archive
from src.utils.i18n import t as tr
from src.utils.logger import logger

# 分享与本地元数据使用独立目录，资源扫描时排除该目录。
META_DIR = ".d3dxmeta"
MANIFEST_NAME = "info.json"
PREVIEW_SUBDIR = "previews"
VARIANT_SUBDIR = "variants"
# INI 覆盖目录按本体相对路径保存编辑后的完整文件，保持归档和内容哈希不变。
# 覆盖文件与容器一同复制，供解析和部署使用。
INI_OVERRIDES_SUBDIR = "ini_overrides"
# 标记覆盖目录已包含本体的全部 INI，后续读取可直接使用普通文件，
# 避免反复打开归档阻塞界面线程。
INI_OVERRIDES_SEEDED_MARKER = ".seeded"
# 非 zip 分享产物格式；仅外部信封写入/清理会访问归档内部。
_NONZIP_RESIDENT_SUFFIXES = {".7z", ".rar"}
# 元数据版本标记用于诊断；读取时按字段校验，缺失字段使用默认值。
SCHEMA_VERSION = 2
MANIFEST_KIND = "d3dxSkinManage-Plus.mod-share"

# Mod 容器目录的身份标记常量。
CONTAINER_SCHEMA_VERSION = 1
CONTAINER_KIND = "d3dxSkinManage-Plus.mod-container"
_CONTENT_HASH_RE = re.compile(r"[0-9a-f]{40}\Z")


class InvalidManagedMetadataError(ValueError):
    """受管元数据文件存在，但不符合当前结构。"""


class InvalidVariantPackageError(InvalidManagedMetadataError):
    """声明的命名配置无效时，不能将导入报告为成功。"""


# 分享清单仅携带接收方需要的字段；本地身份、路径、哈希、状态和分类归属由导入重建。
SHARE_INFO_KEYS = ("author", "description", "version", "url", "age_rating", "tags")

# 分享 custom_data 时保留 GameBanana 更新来源，供接收方继续检查更新。
# 不携带本地检查时间及忽略偏好。
_SHARE_GB_KEYS = (
    "mod_id",
    "file_id",
    "file_md5",
    "file_ts",
    "page_url",
    "version",
    "submitter_id",
    "submitter_name",
)


# 构造


def _shareable_custom_data(custom_data: dict) -> dict:
    """提取可分享的 custom_data 字段，包括 GameBanana 更新来源。"""
    gb = (custom_data or {}).get("gamebanana") or {}
    if not gb.get("mod_id"):
        return {}
    shared_gb = {k: gb[k] for k in _SHARE_GB_KEYS if k in gb}
    shared_gb["last_checked_iso"] = ""  # 不继承本地检测历史，接收方重新检测
    return {"gamebanana": shared_gb}


def share_meta_from_mod(mod: Any) -> dict:
    """通过对象属性与序列化接口提取可分享元数据。

    返回包含 name 和 info 的字典，其中 info 仅保留分享字段及更新来源。
    """
    info = (mod.to_dict() or {}).get("info", {}) or {}
    shared_info = {k: info[k] for k in SHARE_INFO_KEYS if k in info}
    cd = _shareable_custom_data(info.get("custom_data") or {})
    if cd:
        shared_info["custom_data"] = cd
    return {"name": getattr(mod, "display_name", "") or "", "info": shared_info}


def validate_preview_references(references):
    """校验逻辑图片引用，不访问文件系统。"""
    from src.core.library_schema import valid_relative

    if not isinstance(references, list):
        raise InvalidManagedMetadataError("local.preview_references must be an array")
    for reference in references:
        if (
            not isinstance(reference, dict)
            or reference.get("scope") not in ("container", "node", "preview_cache")
            or not valid_relative(reference.get("path"))
        ):
            raise InvalidManagedMetadataError(f"Invalid preview reference: {reference!r}")
    return references


def restore_preview_references(sidecar, discovered, *, container, preview_cache, node=None):
    """按引用顺序恢复已保存图片，再追加新发现的图片。"""
    roots = {"container": Path(container), "preview_cache": Path(preview_cache)}
    if node is not None:
        roots["node"] = Path(node)
    restored = []
    references = validate_preview_references(((sidecar or {}).get("local") or {}).get("preview_references", []))
    for reference in references:
        if reference["scope"] not in roots:
            raise InvalidManagedMetadataError(f"Preview reference has no owner: {reference!r}")
        candidate = roots[reference["scope"]] / reference["path"]
        if fs.is_file(candidate):
            restored.append(str(candidate))
    return list(dict.fromkeys((*restored, *(str(path) for path in discovered))))


def local_meta_from_mod(mod: Any) -> dict:
    """完整保存本地用户信息，不受分享字段白名单限制。"""
    info = deepcopy((mod.to_dict() or {}).get("info", {}) or {})
    info.pop("name", None)
    return {"name": getattr(mod, "display_name", "") or "", "info": info}


def valid_imported_at(value: Any) -> bool:
    """判断导入时间是否为非空且可解析的 ISO 日期时间字符串。"""
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def build_manifest(meta: dict, preview_names: list[str], variant_names: list[str] | None = None) -> dict:
    """构造写入 ``info.json`` 的清单内容。"""
    manifest = {
        "name": (meta or {}).get("name", ""),
        "info": (meta or {}).get("info", {}) or {},
        "previews": preview_names,
    }
    if variant_names is not None:
        manifest["variants"] = validate_variant_references(variant_names)
    return _sanitize_share_manifest(manifest)


def validate_variant_references(references: Any) -> list[str]:
    """变体成员使用固定目录和安全的身份文件名。"""
    from src.models.mod_variant import validate_variant_id

    if not isinstance(references, list):
        raise InvalidVariantPackageError(tr("变体清单必须是数组"))
    seen = set()
    for relative in references:
        if not isinstance(relative, str):
            raise InvalidVariantPackageError(tr("变体清单必须包含相对文件名"))
        parts = relative.split("/")
        if len(parts) != 2 or parts[0] != VARIANT_SUBDIR or not parts[1].casefold().endswith(".json"):
            raise InvalidVariantPackageError(tr("变体成员必须位于 variants/<variant_id>.json"))
        try:
            validate_variant_id(parts[1][:-5])
        except ValueError as error:
            raise InvalidVariantPackageError(tr("变体成员路径非法：{relative}", relative=relative)) from error
        key = relative.casefold()
        if key in seen:
            raise InvalidVariantPackageError(tr("变体清单包含重复文件或身份"))
        seen.add(key)
    return list(references)


def _variant_members(variants) -> dict[str, bytes]:
    """只编码可分享模型的白名单字段，不依赖本地存储。"""
    from src.models.mod_variant import ModVariant

    members, seen = {}, set()
    for payload in variants or ():
        variant = ModVariant.from_dict(payload)
        relative = f"{VARIANT_SUBDIR}/{variant.variant_id}.json"
        if relative.casefold() in seen:
            raise InvalidVariantPackageError(tr("变体分享包含重复身份"))
        seen.add(relative.casefold())
        members[relative] = (json.dumps(variant.to_dict(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return members


def _validate_relative_member(value: Any, field: str, *, preview: bool = False) -> str:
    """校验成员为无越界段的正斜杠相对路径，预览引用进一步限制在预览子目录。"""
    if not isinstance(value, str) or not value or "\\" in value:
        raise InvalidManagedMetadataError(tr("{field} 必须是非空 posix 相对路径", field=field))
    path = Path(value)
    parts = value.split("/")
    if path.is_absolute() or bool(path.drive) or any(part in {"", ".", ".."} for part in parts):
        raise InvalidManagedMetadataError(tr("{field} 包含非法路径", field=field))
    if preview and (len(parts) != 2 or parts[0] != PREVIEW_SUBDIR):
        raise InvalidManagedMetadataError(
            tr("{field} 必须是 {PREVIEW_SUBDIR}/<文件名>", field=field, PREVIEW_SUBDIR=PREVIEW_SUBDIR),
        )
    return value


def _validate_manifest(data: Any) -> dict:
    """校验身份和图片位置，保留未知描述字段。

    版本字段用于说明；缺失的可选内容取默认值，显式图片引用必须使用安全的范围与路径。
    """
    if not isinstance(data, dict):
        raise InvalidManagedMetadataError(tr("sidecar/share 根节点必须是 JSON 对象"))
    if data.get("kind") != MANIFEST_KIND:
        raise InvalidManagedMetadataError(
            tr("sidecar/share kind 必须为 {MANIFEST_KIND}", MANIFEST_KIND=MANIFEST_KIND),
        )
    schema = data.get("schema")
    if schema != SCHEMA_VERSION:
        logger.info(
            "sidecar/share schema=%r 与本程序的 %d 不同，按「缺失即默认」照常读取",
            schema,
            SCHEMA_VERSION,
        )
    if not isinstance(data.get("name"), str):
        data["name"] = ""
    if not isinstance(data.get("info"), dict):
        data["info"] = {}
    if not isinstance(data.get("previews"), list):
        data["previews"] = []
    for index, rel in enumerate(data["previews"]):
        _validate_relative_member(rel, f"previews[{index}]", preview=True)
    if "variants" in data:
        validate_variant_references(data["variants"])

    if "local" in data:
        if not isinstance(data["local"], dict):
            logger.info("sidecar local 不是 JSON 对象（%r），按无本地元数据处理", type(data["local"]).__name__)
            data["local"] = {}
        local = data["local"]
        if "preview_references" in local:
            validate_preview_references(local["preview_references"])
        excluded = local.get("excluded_archive_previews", [])
        if not isinstance(excluded, list):
            raise InvalidManagedMetadataError("local.excluded_archive_previews must be an array")
        for index, relative in enumerate(excluded):
            _validate_relative_member(relative, f"local.excluded_archive_previews[{index}]")
    return data
