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


def _sanitize_share_manifest(manifest: dict) -> dict:
    """应用外部分享数据前移除本地、私有和未知字段。"""
    info = manifest["info"]
    cleaned_info: dict[str, Any] = {}
    for key in ("author", "description", "version", "url"):
        value = info.get(key)
        if isinstance(value, str):
            cleaned_info[key] = value
    age_rating = info.get("age_rating")
    if age_rating in {"G", "P", "R", "X"}:
        cleaned_info["age_rating"] = age_rating
    tags = info.get("tags")
    if isinstance(tags, list):
        cleaned_info["tags"] = [tag for tag in tags if isinstance(tag, str)]
    custom_data = info.get("custom_data")
    if isinstance(custom_data, dict):
        gb = custom_data.get("gamebanana")
        if isinstance(gb, dict) and gb.get("mod_id"):
            shared_gb = {key: gb[key] for key in _SHARE_GB_KEYS if key in gb}
            shared_gb["last_checked_iso"] = ""
            cleaned_info["custom_data"] = {"gamebanana": shared_gb}
    cleaned = {
        "schema": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "name": manifest["name"],
        "info": cleaned_info,
        "previews": list(manifest["previews"]),
    }
    if "variants" in manifest:
        cleaned["variants"] = validate_variant_references(manifest["variants"])
    return cleaned


# 写入（导出侧）


def write_envelope_to_folder(
    folder: str | Path,
    meta: dict,
    preview_paths: list[str] | None = None,
    *,
    variants=None,
) -> bool:
    """把信封写入已导出的 Mod 文件夹（``folder`` 为该 Mod 的顶层目录）。"""
    folder = Path(folder)
    if not folder.is_dir():
        return False
    try:
        members = _variant_members(variants)
        meta_dir = folder / META_DIR
        meta_dir.mkdir(parents=True, exist_ok=True)
        names: list[str] = []
        previews = _collect_previews(preview_paths)
        if previews:
            prev_dir = meta_dir / PREVIEW_SUBDIR
            prev_dir.mkdir(parents=True, exist_ok=True)
            for src in previews:
                dst = _unique_in(prev_dir, src.name)
                shutil.copy2(str(src), str(dst))
                names.append(f"{PREVIEW_SUBDIR}/{dst.name}")
        for relative, data in members.items():
            fs.write_bytes(meta_dir / relative, data)
        manifest = build_manifest(meta, names, list(members) if variants is not None else None)
        _validate_manifest(manifest)
        (meta_dir / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except OSError as e:
        logger.warning("写入分享信封失败 (folder): %s", e)
        return False


def append_envelope_to_zip(
    zip_path: str | Path,
    meta: dict,
    preview_paths: list[str] | None = None,
    *,
    variants=None,
) -> bool:
    """把信封追加进已生成的 ZIP（写在压缩包根：``.d3dxmeta/...``）。"""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        return False
    try:
        members = _variant_members(variants)
        previews = _collect_previews(preview_paths)
        names: list[str] = []
        with zipfile.ZipFile(str(zip_path), "a", zipfile.ZIP_DEFLATED) as zf:
            existing = set(zf.namelist())
            # 将分享数据写入 ZIP 的唯一顶层目录，保持原有目录结构，
            # 避免解压服务因出现多个顶层成员而额外增加目录层级，影响哈希和部署。
            top = _detect_single_top(existing)
            base = f"{top}/{META_DIR}" if top else META_DIR
            for src in previews:
                arc = _unique_arc(existing, f"{base}/{PREVIEW_SUBDIR}/{src.name}")
                zf.write(str(src), arc)
                existing.add(arc)
                names.append(f"{PREVIEW_SUBDIR}/{Path(arc).name}")
            for relative, data in members.items():
                zf.writestr(f"{base}/{relative}", data)
            manifest = build_manifest(meta, names, list(members) if variants is not None else None)
            _validate_manifest(manifest)
            zf.writestr(
                f"{base}/{MANIFEST_NAME}",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
        return True
    except (OSError, zipfile.BadZipFile) as e:
        logger.warning("写入分享信封失败 (zip): %s", e)
        return False


def append_envelope_to_archive(
    archive_path: str | Path,
    meta: dict,
    preview_paths: list[str] | None = None,
    *,
    variants=None,
) -> bool:
    """将元数据及补充预览追加到 7z 或 RAR 的单一顶层目录中。

    目录布局与 ZIP 一致，读取方按最浅层的 .d3dxmeta/info.json 定位。
    7z 使用 py7zr 追加模式写入独立数据块，RAR 使用 WinRAR 的追加命令。
    预览图和清单分别写入；任一成员写入失败时返回 False，由调用方处理。
    RAR 追加依赖系统中的 WinRAR。
    """
    archive = Path(archive_path)
    if not archive.is_file():
        return False
    suffix = archive.suffix.lower()
    if suffix not in (".7z", ".rar"):
        return False
    try:
        members = _variant_members(variants)
        from src.services.archive import ArchiveService

        svc = ArchiveService()

        def _inject(arcname: str, data: bytes) -> bool:
            """按目标压缩格式调用成员注入接口，返回写入是否成功。"""
            if suffix == ".7z":
                return svc.inject_member_7z(archive, arcname, data)
            return svc.inject_member_rar(archive, arcname, data)

        top = _archive_single_top(archive) or ""
        base = f"{top}/{META_DIR}" if top else META_DIR
        existing = {n.replace("\\", "/") for n in svc.list_contents(archive)}
        names: list[str] = []
        for src in _collect_previews(preview_paths):
            arc = _unique_arc(existing, f"{base}/{PREVIEW_SUBDIR}/{src.name}")
            if not _inject(arc, src.read_bytes()):
                logger.warning("追加分享预览图失败 (archive): %s", src.name)
                return False
            existing.add(arc)
            names.append(f"{PREVIEW_SUBDIR}/{Path(arc).name}")
        for relative, data in members.items():
            if not _inject(f"{base}/{relative}", data):
                logger.warning("追加分享变体失败 (archive): %s", relative)
                return False
        manifest = build_manifest(meta, names, list(members) if variants is not None else None)
        _validate_manifest(manifest)
        text = json.dumps(manifest, ensure_ascii=False, indent=2)
        return _inject(f"{base}/{MANIFEST_NAME}", text.encode("utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("写入分享信封失败 (archive): %s", e)
        return False


# 读取 / 清理（导入侧）


def read_envelope(*candidate_dirs: str | Path) -> dict | None:
    """在候选目录里探测 ``.d3dxmeta/info.json`` 并读出。

    参数：
        candidate_dirs: 依序探测的目录，通常为 ``(解压根, 顶层 mod 文件夹)``。

    返回：
        ``{"manifest": dict, "meta_dir": Path}``；未找到返回 ``None``。文件存在但
        JSON 或字段结构无效时抛 :class:`InvalidManagedMetadataError`。
    """
    for d in candidate_dirs:
        if not d:
            continue
        meta_dir = Path(d) / META_DIR
        mpath = meta_dir / MANIFEST_NAME
        if fs.is_file(mpath):
            try:
                manifest = json.loads(fs.read_text(mpath, encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise InvalidManagedMetadataError(
                    tr("无法解析分享信封: {mpath}: {e}", mpath=mpath, e=e),
                ) from e
            validated = _validate_manifest(manifest)
            return {
                "manifest": _sanitize_share_manifest(validated),
                "meta_dir": meta_dir,
            }
    return None


def resolve_preview_paths(meta_dir: str | Path, manifest: dict) -> list[Path]:
    """解析分享数据中的补充预览图。

    分享包按清单读取；本地容器清单未记录预览文件时枚举 .d3dxmeta/previews，
    使直接复制容器后重新导入仍能恢复手动补充图片。
    """
    meta_dir = Path(meta_dir)
    out: list[Path] = []
    listed = manifest.get("previews") or []
    if listed:
        for rel in listed:
            if not isinstance(rel, str):
                continue
            p = meta_dir / rel
            if fs.is_file(p):
                out.append(p)
        return out
    preview_dir = meta_dir / PREVIEW_SUBDIR
    try:
        out.extend(
            p
            for p in sorted(preview_dir.iterdir())
            if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS and not p.name.lower().endswith(".dds")
        )
    except OSError:
        pass
    return out
