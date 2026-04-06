"""统一处理 Mod 导入格式和源文件准备。

归档在不要求转换时保留原格式，转换时先解压再压缩；文件夹按指定目标复制或压缩。
导入中的分享数据与容器标记不属于本体内容，须在生成资源本体前读取并移除。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from src.core.mod_package import (
    INI_OVERRIDES_SEEDED_MARKER,
    META_DIR,
    InvalidManagedMetadataError,
    InvalidVariantPackageError,
    read_container_marker,
    read_sidecar,
    resolve_ini_overrides_dir,
    strip_envelope,
    take_envelope,
)
from src.core.mod_variant_package import (
    read_package_variants,
    validate_archive_variant_members,
    validate_folder_variant_roots,
    validate_package_variants,
)
from src.core.preview_cache_index import (
    capture_source_version,
    preview_cache_staging,
    publish_preview_cache,
)
from src.models.mod_variant import ModVariant
from src.utils import fs
from src.utils.constants import (
    ARCHIVE_EXTENSIONS,
    IMAGE_EXTENSIONS,
    MOD_CONTAINER_MARKER,
    is_resident_archive,
)
from src.utils.i18n import N_
from src.utils.i18n import t as tr
from src.utils.logger import logger

ARCHIVE_IMPORT_FORMATS = {"zip", "7z", "rar"}
# 库内本体使用归档格式；source_format 的 folder 表示外部目录输入。
VALID_IMPORT_FORMATS = set(ARCHIVE_IMPORT_FORMATS)
DEFAULT_IMPORT_FORMAT = "7z"


def normalize_import_format(value: object, default: str = DEFAULT_IMPORT_FORMAT) -> str:
    """规范化导入格式名称，无效选项使用调用方指定的默认格式。"""
    fmt = str(value or default).strip().lower()
    return fmt if fmt in VALID_IMPORT_FORMATS else default


def get_import_settings(config) -> tuple[str, bool]:
    """从 Config-like 对象读取统一导入格式设置。"""
    return (
        normalize_import_format(config.get("import.format", DEFAULT_IMPORT_FORMAT)),
        bool(config.get("import.strict_format", False)),
    )


def source_format(path: str | Path) -> str:
    """按实际文件类型识别目录或受支持压缩包，无法识别时返回空字符串。"""
    p = Path(path)
    if fs.is_dir(p):
        return "folder"
    suffix = p.suffix.lower()
    if fs.is_file(p) and suffix in ARCHIVE_EXTENSIONS:
        return suffix.lstrip(".")
    return ""


def final_import_format(src_format: str, target_format: str, strict_format: bool) -> str:
    """计算最终库内格式；返回 zip/7z/rar（文件夹源一律压制成 target）。"""
    target = normalize_import_format(target_format)
    if src_format in ARCHIVE_IMPORT_FORMATS and not strict_format:
        return src_format
    return target


def _member_is_reserved(name: str) -> bool:
    """检查压缩成员路径是否包含元数据目录或容器标记文件。"""
    parts = [p.casefold() for p in name.replace("\\", "/").split("/") if p]
    return META_DIR in parts or (parts and parts[-1] == MOD_CONTAINER_MARKER)


def archive_has_reserved_payload(path: str | Path, archive_service) -> bool:
    """归档内是否含分享信封/容器 marker；仅查看目录，不解压。"""
    try:
        return any(_member_is_reserved(name) for name in archive_service.list_contents(path))
    except Exception as e:
        logger.debug("探测导入归档保留成员失败 (%s): %s", path, e)
        return False
