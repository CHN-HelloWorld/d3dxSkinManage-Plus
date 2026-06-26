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
