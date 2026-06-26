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


def strip_envelope(*candidate_dirs: str | Path) -> None:
    """移除输入副本中的 .d3dxmeta 和 .d3dxmod，使资源内容不包含容器元数据。

    导入服务随后创建新的容器标记，避免结构信息重复进入归档本体。
    """
    for d in candidate_dirs:
        if not d:
            continue
        meta_dir = Path(d) / META_DIR
        if meta_dir.is_dir():
            shutil.rmtree(str(meta_dir), ignore_errors=True)
        marker = Path(d) / MOD_CONTAINER_MARKER
        if marker.is_file():
            try:
                marker.unlink()
            except OSError:
                pass


def stage_envelope(candidate_dirs, stage_dir: str | Path) -> tuple[dict | None, list[Path]]:
    """读取目录或已解压的分享数据并建立暂存副本，不修改源内容。"""
    env = read_envelope(*candidate_dirs)
    if not env:
        return None, []
    manifest = env["manifest"]
    meta_dir = env["meta_dir"]
    staged: list[Path] = []
    stage = Path(stage_dir)
    try:
        stage.mkdir(parents=True, exist_ok=True)
        for sp in resolve_preview_paths(meta_dir, manifest):
            dst = _unique_in(stage, sp.name)
            shutil.copy2(str(sp), str(dst))
            staged.append(dst)
    except OSError as e:
        logger.warning("暂存分享预览图失败: %s", e)
    return manifest, staged


def take_envelope(candidate_dirs, stage_dir: str | Path) -> tuple[dict | None, list[Path]]:
    """读取分享数据并暂存补充预览，然后移除源中的分享目录。

    参数：
        candidate_dirs: 按顺序检查的目录，通常为解压根与顶层 Mod 目录。
        stage_dir: 当前 Mod 独占的预览暂存目录，避免并行导入争用同一路径。

    返回清单或 None，以及暂存图片的绝对路径列表。移除源分享目录后暂存图仍可用，
    调用方获得 Mod 编号后将其保存到永久预览目录。
    """
    manifest, staged = stage_envelope(candidate_dirs, stage_dir)
    if manifest is None:
        return None, []
    strip_envelope(*candidate_dirs)
    logger.info(
        "检测到分享信封: name=%s, 预览图 %d 张",
        manifest.get("name", ""),
        len(staged),
    )
    return manifest, staged


# 容器内本体与元数据同级存储，元数据不进入本体归档。


def is_mod_container(dir_path: str | Path) -> bool:
    """根据 .d3dxmod 标记判断目录是否为 Mod 容器。

    显式标记界定发现流程的扫描边界，不根据目录名称推断容器身份。
    """
    try:
        marker = _read_container_marker_data(Path(fs.normal_path(dir_path)))
        from src.core.mod_identity import DEPLOYMENT_IDENTITY_KIND

        return marker is not None and (not isinstance(marker, dict) or marker.get("kind") != DEPLOYMENT_IDENTITY_KIND)
    except InvalidManagedMetadataError:
        # 损坏的资源标记仍界定容器边界，由发现流程报告错误。
        return True
    except OSError:
        return False


def write_container_marker(
    container: str | Path,
    body_name: str,
    content_hash: str,
    *,
    mod_id: str | None = None,
    imported_at: str,
) -> bool:
    """写容器正向 marker ``.d3dxmod``（JSON：本体文件名/类型/content_hash/schema）。

    ``body_name`` 是本体在容器内的文件/夹名（``<ModName>.7z`` 或
    ``<ModName>``）。无效输入抛 :class:`InvalidManagedMetadataError`；磁盘写入失败
    抛出异常，调用方必须回滚本轮产物。
    """
    c = Path(fs.normal_path(container))
    from src.core.mod_identity import IdentityConflict, new_mod_id, validate_mod_id

    if not valid_imported_at(imported_at):
        raise InvalidManagedMetadataError("imported_at must be a valid ISO timestamp")
    existing = _read_container_marker_data(c)
    if existing is not None:
        if not isinstance(existing, dict) or existing.get("kind") != CONTAINER_KIND:
            raise InvalidManagedMetadataError(tr("不是源 Mod 容器标记：{c}", c=c))
        # 原本体可能已改名，此处仅核对身份。
        try:
            identity = validate_mod_id(existing.get("mod_id"))
        except IdentityConflict:
            identity = new_mod_id() if mod_id is None else validate_mod_id(mod_id)
        if mod_id is not None and mod_id != identity:
            raise IdentityConflict(tr("不能改写源 Mod 的 mod_id 身份：{c}", c=c))
    else:
        identity = new_mod_id() if mod_id is None else validate_mod_id(mod_id)
    marker = {
        **(existing or {}),
        "schema": CONTAINER_SCHEMA_VERSION,
        "kind": CONTAINER_KIND,
        "body": body_name,
        "body_type": "archive" if is_resident_archive(body_name) else "folder",
        "content_hash": content_hash,
        "mod_id": identity,
        "imported_at": (
            existing["imported_at"] if existing and valid_imported_at(existing.get("imported_at")) else imported_at
        ),
    }
    _validate_container_marker(c, marker, recover_body=False)
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix=".marker-", suffix=".tmp", dir=fs.native_path(c))
        os.close(fd)
        temporary = Path(name)
        fs.write_text(
            temporary,
            json.dumps(marker, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with open(fs.native_path(temporary), "r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(fs.native_path(temporary), fs.native_path(c / MOD_CONTAINER_MARKER))
        return True
    finally:
        if temporary is not None:
            fs.unlink(temporary, missing_ok=True)


def read_container_marker(container: str | Path) -> dict | None:
    """读取并校验当前容器标记。

    标记缺失或属于部署目录时返回 None；资源结构无效时抛出 InvalidManagedMetadataError。
    """
    c = Path(fs.normal_path(container))
    data = _read_container_marker_data(c)
    from src.core.mod_identity import DEPLOYMENT_IDENTITY_KIND

    if data is None or (isinstance(data, dict) and data.get("kind") == DEPLOYMENT_IDENTITY_KIND):
        return None
    return _validate_container_marker(c, data)


def read_missing_container_marker(container: str | Path) -> dict | None:
    """为已识别但缺少本体的容器提供后台检查证据。

    存在的内容、改名、链接、无效身份和访问错误不能授权删除，调用方仍须完成移动认领检查。
    """
    from src.core.mod_identity import validate_mod_id

    c = Path(fs.normal_path(container))
    info = os.lstat(fs.native_path(c))
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise InvalidManagedMetadataError(tr("不是普通源容器目录：{c}", c=c))
    data = _validate_container_marker(c, _read_container_marker_data(c), recover_body=False, allow_missing_body=True)
    validate_mod_id(data.get("mod_id"))
    try:
        os.lstat(fs.native_path(c / data["body"]))
    except (FileNotFoundError, NotADirectoryError):
        pass
    else:
        return None
    if any(child.name not in (MOD_CONTAINER_MARKER, META_DIR) for child in fs.list_children(c)):
        raise InvalidManagedMetadataError(tr("缺失源容器仍含其他内容：{c}", c=c))
    return data


def restore_container_marker(container: str | Path, snapshot: bytes) -> None:
    """重命名回滚时原子恢复资源标记的原始内容。"""
    from src.core.mod_identity import IdentityConflict, validate_mod_id

    c = Path(fs.normal_path(container))
    data = _validate_container_marker(c, json.loads(snapshot), recover_body=False)
    identity = validate_mod_id(data.get("mod_id"))
    existing = _read_container_marker_data(c)
    if existing is not None and existing.get("mod_id") != identity:
        raise IdentityConflict(tr("回滚时源 Mod 身份发生变化：{c}", c=c))
    fd, temporary = tempfile.mkstemp(prefix=".marker-", suffix=".tmp", dir=fs.native_path(c))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(snapshot)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, fs.native_path(c / MOD_CONTAINER_MARKER))
    finally:
        fs.unlink(temporary, missing_ok=True)


def _read_container_marker_data(container: Path):
    """读取容器标记 JSON 对象，文件缺失时返回 None，损坏或访问失败转为元数据错误。"""
    p = container / MOD_CONTAINER_MARKER
    try:
        if os.name == "nt":
            from src.core.watcher_windows import read_marker_text

            text = read_marker_text(p)
        else:
            text = fs.read_text(p, encoding="utf-8")
        data = json.loads(text)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, ValueError) as e:
        raise InvalidManagedMetadataError(tr("无法解析容器 marker: {p}: {e}", p=p, e=e)) from e
    if not isinstance(data, dict):
        raise InvalidManagedMetadataError(tr("容器 marker 根节点必须是 JSON 对象"))
    return data


def _identify_container_body(container: Path) -> str:
    """仅在本地存在唯一候选时恢复缺失引用。"""
    candidates = []
    for child in container.iterdir():
        if child.name.startswith("."):
            continue
        mode = os.stat(fs.native_path(child), follow_symlinks=False).st_mode
        if stat.S_ISDIR(mode) or (stat.S_ISREG(mode) and is_resident_archive(child)):
            candidates.append(child.name)
    if len(candidates) != 1:
        raise InvalidManagedMetadataError(f"Cannot identify a unique container body: {container} ({candidates})")
    return candidates[0]


def _validate_container_marker(
    container: Path, data: Any, *, recover_body: bool = True, allow_missing_body: bool = False
) -> dict:
    """校验结构，仅从唯一的本地候选恢复缺失本体。

    权限错误向上传播，身份和时间戳修复由拥有库上下文的调用方负责。
    """
    if not isinstance(data, dict):
        raise InvalidManagedMetadataError(tr("容器 marker 根节点必须是 JSON 对象"))
    if data.get("kind") != CONTAINER_KIND:
        raise InvalidManagedMetadataError(tr("容器 marker kind 必须为 {CONTAINER_KIND}", CONTAINER_KIND=CONTAINER_KIND))
    schema = data.get("schema")
    if schema != CONTAINER_SCHEMA_VERSION:
        logger.info(
            "容器 marker schema=%r 与本程序的 %d 不同，按「缺失即默认」照常读取",
            schema,
            CONTAINER_SCHEMA_VERSION,
        )

    body_name = data.get("body")
    if body_name in (None, "") and recover_body:
        body_name = _identify_container_body(container)
        data = dict(data, body=body_name, body_type=None)
    if (
        not isinstance(body_name, str)
        or not body_name
        or body_name in {".", ".."}
        or "\x00" in body_name
        or "/" in body_name
        or "\\" in body_name
        or Path(body_name).is_absolute()
        or bool(Path(body_name).drive)
    ):
        raise InvalidManagedMetadataError(tr("容器 marker body 必须是单层 basename"))

    content_hash = data.get("content_hash")
    if not isinstance(content_hash, str) or _CONTENT_HASH_RE.fullmatch(content_hash) is None:
        raise InvalidManagedMetadataError(tr("容器 marker content_hash 必须是 40 位小写十六进制"))

    body = container / body_name
    try:
        body_stat = os.stat(fs.native_path(body), follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        if allow_missing_body:
            body_type = data.get("body_type")
            if body_type not in {"folder", "archive"} or (body_type == "archive" and not is_resident_archive(body)):
                raise InvalidManagedMetadataError(tr("缺失本体类型无效：{body}", body=body))
            return data
        if not recover_body:
            raise InvalidManagedMetadataError(f"Container body does not exist: {body}")
        recovered = _identify_container_body(container)
        return _validate_container_marker(container, dict(data, body=recovered, body_type=None), recover_body=False)
    ordinary = (
        body_stat is not None
        and not stat.S_ISLNK(body_stat.st_mode)
        and not (getattr(body_stat, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    )
    if ordinary:
        # 已确认的普通直接子项属于当前父目录，无需重复解析祖先链接。
        is_directory = stat.S_ISDIR(body_stat.st_mode)
        is_file = stat.S_ISREG(body_stat.st_mode)
    else:
        # 链接、重解析点和不确定的读取仍使用完整路径解析。
        if not fs.exists(body):
            raise InvalidManagedMetadataError(tr("容器 marker 指定的本体不存在: {body}", body=body))
        resolved_container = os.path.normcase(
            os.path.normpath(os.path.realpath(fs.native_path(container))),
        )
        resolved_body = os.path.normcase(
            os.path.normpath(os.path.realpath(fs.native_path(body))),
        )
        if os.path.dirname(resolved_body) != resolved_container:
            raise InvalidManagedMetadataError(tr("容器 marker 本体解析到容器之外: {body}", body=body))
        is_directory, is_file = fs.is_dir(body), fs.is_file(body)

    body_type = data.get("body_type")
    if body_type not in {"folder", "archive"}:
        inferred = ""
        if is_directory:
            inferred = "folder"
        elif is_file and is_resident_archive(body):
            inferred = "archive"
        if not inferred:
            raise InvalidManagedMetadataError(
                tr(
                    "容器 marker body_type={body_type!r} 陌生，且本体既不是目录也不是受支持归档: {body}",
                    body_type=body_type,
                    body=body,
                ),
            )
        logger.info("容器 marker body_type=%r 陌生，按本体实际形态取 %s: %s", body_type, inferred, container)
        data["body_type"] = body_type = inferred
    if body_type == "folder" and not is_directory:
        raise InvalidManagedMetadataError(tr("容器 marker 声明 folder，但本体不是目录: {body}", body=body))
    if body_type == "archive" and (not is_file or not is_resident_archive(body)):
        raise InvalidManagedMetadataError(tr("容器 marker 声明 archive，但本体不是受支持归档: {body}", body=body))
    return data


def resolve_body_path(container: str | Path) -> Path | None:
    """解析容器内的本体路径（``容器/<ModName>.7z`` 或 ``容器/<ModName>/``）。

    采用有效引用，缺失时只接受容器内唯一候选。marker 缺失返回 ``None``；
    无法唯一确定或结构非法时传播 :class:`InvalidManagedMetadataError`。
    """
    c = Path(fs.normal_path(container))
    if not fs.is_dir(c):
        return None
    marker = read_container_marker(c)
    return c / marker["body"] if marker is not None else None


def _resolve_sidecar_dir(mod_path: str | Path) -> Path | None:
    """返回当前容器的元数据目录，无匹配时返回 None。

    带标记路径必须符合完整容器契约；本体路径必须与父目录标记声明的名称一致。
    """
    p = Path(fs.normal_path(mod_path))
    if fs.is_dir(p) and fs.is_file(p / MOD_CONTAINER_MARKER):
        marker = read_container_marker(p)
        return p / META_DIR if marker is not None else None
    if fs.is_file(p.parent / MOD_CONTAINER_MARKER):
        marker = read_container_marker(p.parent)
        if marker is None:
            return None
        declared = p.parent / marker["body"]
        declared_key = os.path.normcase(os.path.abspath(str(declared)))
        path_key = os.path.normcase(os.path.abspath(str(p)))
        if declared_key == path_key:
            return p.parent / META_DIR
    return None


def resolve_previews_dir(mod_path: str | Path) -> Path | None:
    """返回当前格式容器的补充预览图目录。

    ``.d3dxmeta/previews`` 只保存用户手动添加或分享信封带来的图片；本体原图仍留在
    文件夹本体内，或由归档本体按需生成 ``preview_cache``。非容器路径不受支持。
    """
    sidecar_dir = _resolve_sidecar_dir(mod_path)
    return sidecar_dir / PREVIEW_SUBDIR if sidecar_dir is not None else None


def supplemental_preview_paths(
    mod_path: str | Path,
    preview_paths: list[str] | None,
) -> list[Path]:
    """筛出可写入分享信封的补充图，严格排除本体原始预览图。"""
    preview_dir = resolve_previews_dir(mod_path)
    if preview_dir is None:
        return []
    root_key = os.path.normcase(os.path.abspath(str(preview_dir)))
    seen: set[str] = set()
    result: list[Path] = []
    for raw in preview_paths or []:
        try:
            path = Path(raw)
            key = os.path.normcase(os.path.abspath(str(path)))
            parent_key = os.path.normcase(os.path.abspath(str(path.parent)))
        except (OSError, TypeError, ValueError):
            continue
        if parent_key != root_key or key in seen or not fs.is_file(path):
            continue
        seen.add(key)
        result.append(path)
    return result


# 容器的 INI 覆盖文件独立于本体，随容器迁移。


def resolve_ini_overrides_dir(mod_path: str | Path) -> Path | None:
    """解析容器级 .d3dxmeta/ini_overrides 目录。

    mod_path 不位于容器内时返回 None，由调用方决定如何处理独立源文件。
    容器判定与 resolve_previews_dir 一致。
    """
    sidecar_dir = _resolve_sidecar_dir(mod_path)
    if sidecar_dir is None:
        return None
    p = Path(fs.normal_path(mod_path))
    in_container = (fs.is_dir(p) and fs.is_file(p / MOD_CONTAINER_MARKER)) or fs.is_file(
        p.parent / MOD_CONTAINER_MARKER
    )
    if not in_container:
        return None
    return sidecar_dir / INI_OVERRIDES_SUBDIR


def _norm_rel(rel: str | Path) -> str:
    """规范化 ini 的「本体内相对路径」为 posix 风格、去首尾分隔，防目录穿越。"""
    s = str(rel).replace("\\", "/").strip("/")
    parts = [seg for seg in s.split("/") if seg not in ("", ".", "..")]
    return "/".join(parts)


def override_path_for(mod_path: str | Path, rel: str | Path) -> Path | None:
    """某 ini（本体内相对路径 rel）对应的 override 文件绝对路径；非容器返回 None。"""
    base = resolve_ini_overrides_dir(mod_path)
    if base is None:
        return None
    norm = _norm_rel(rel)
    if not norm:
        return None
    return base / norm


def read_override(mod_path: str | Path, rel: str | Path) -> str | None:
    """读某 ini 的 override 全文（存在则返回文本），无 override / 非容器返回 None。"""
    op = override_path_for(mod_path, rel)
    if op is None or not fs.is_file(op):
        return None
    try:
        # surrogateescape 和空 newline 参数保留原始字节及换行格式。
        with open(fs.native_path(op), encoding="utf-8", errors="surrogateescape", newline="") as fh:
            return fh.read()
    except OSError as e:
        logger.warning("读取 ini override 失败: %s", e)
        return None


def write_override(mod_path: str | Path, rel: str | Path, text: str) -> bool:
    """将完整 INI 文本写入覆盖文件；非容器或写入失败时返回 False。

    覆盖文件可能与部署文件共享硬链接，或被符号链接引用。此时就地截断写入，
    保留文件身份，使部署侧读取到相同内容；普通文件使用暂存文件与原子替换。
    对硬链接使用原子替换会创建新的文件身份并断开原有共享关系。
    """
    op = override_path_for(mod_path, rel)
    if op is None:
        logger.warning("write_override: 非容器本体，无 override 能力: %s", mod_path)
        return False
    try:
        fs.ensure_dir(op.parent)
        is_linked = False
        try:
            if op.is_symlink():
                is_linked = True
            elif fs.exists(op) and os.stat(fs.native_path(op)).st_nlink > 1:
                is_linked = True
        except OSError:
            is_linked = False
        if is_linked:
            # 就地写入以保留硬链接身份，符号链接则写入其目标文件。
            with open(fs.native_path(op), "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
                fh.write(text)
            return True
        tmp = op.with_name(op.name + ".tmp")
        with open(fs.native_path(tmp), "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
            fh.write(text)
        os.replace(fs.native_path(tmp), fs.native_path(op))
        return True
    except OSError as e:
        logger.warning("写入 ini override 失败: %s", e)
        try:
            if "tmp" in dir() and fs.exists(tmp):
                fs.unlink(tmp)
        except OSError:
            pass
        return False


def list_overrides(mod_path: str | Path) -> list[str]:
    """列出该容器内已有 override 的 ini 相对路径（posix 风格）。非容器返回 []。"""
    base = resolve_ini_overrides_dir(fs.normal_path(mod_path))
    if base is None or not fs.is_dir(base):
        return []
    base = Path(fs.normal_path(base))
    out: list[str] = []
    for p in sorted(fs.iter_files(base)):
        if p.suffix.casefold() == ".ini" and fs.is_file(p):
            if p.name == INI_OVERRIDES_SEEDED_MARKER:
                continue  # 初始化标记不属于 INI 文件，不能作为部署内容。
            try:
                rel = Path(fs.normal_path(p)).relative_to(base).as_posix()
                rel = _norm_rel(rel)
                if rel:
                    out.append(rel)
            except ValueError:
                continue
    return out


def overrides_seeded(mod_path: str | Path) -> bool:
    """判断覆盖目录是否已完整包含本体 INI。

    完整时可直接读取覆盖文件；未完成时由调用方读取归档并补充覆盖目录。
    """
    base = resolve_ini_overrides_dir(mod_path)
    if base is None:
        return False
    return fs.is_file(base / INI_OVERRIDES_SEEDED_MARKER)


def seed_ini_overrides(mod_path: str | Path) -> bool:
    """将归档中的全部 INI 初始化到覆盖目录，并写入 .seeded 标记。

    仅处理容器中的归档本体；非归档或非容器路径返回 False。
    已有覆盖文件包含用户编辑结果，不能被本体内容覆盖。
    归档只打开一次以批量读取全部 INI；任一步失败时返回 False，由调用方读取本体。
    """
    p = Path(fs.normal_path(mod_path))
    if not (fs.is_file(p) and is_resident_archive(p)):
        return False
    base = resolve_ini_overrides_dir(mod_path)
    if base is None:
        return False
    try:
        existing = set(list_overrides(mod_path))  # 已编辑的 override，不覆盖
        pairs: list[tuple[str, str]] = []  # (rel, text)

        try:
            from src.services.archive import ArchiveService

            raw = ArchiveService().read_all_inis(p)
        except Exception:
            raw = {}
        top = _archive_top_prefix(p)
        for member, data in raw.items():
            nn = member.replace("\\", "/")
            rel = nn
            if top and (nn == top or nn.startswith(top + "/")):
                rel = nn[len(top) + 1 :] if nn.startswith(top + "/") else ""
            rel = _norm_rel(rel)
            if not rel or rel in existing:
                continue
            try:
                pairs.append((rel, data.decode("utf-8", errors="surrogateescape")))
            except Exception:
                pass

        for rel, text in pairs:
            write_override(mod_path, rel, text)
        # 即使本体不含 INI 也写入完成标记，避免反复打开归档确认。
        fs.ensure_dir(base)
        fs.write_text(base / INI_OVERRIDES_SEEDED_MARKER, "1", encoding="utf-8")
        return True
    except Exception as e:
        logger.warning("播种 ini override 失败: %s", e)
        return False


def _archive_top_prefix(archive_path: Path) -> str:
    """归档单顶层目录前缀（如 ``MyMod``），无单顶层返回 ""。用于本体内 rel ↔ 归档成员名换算。"""
    top = _archive_single_top(archive_path)
    return top or ""


def body_ini_rels(mod_path: str | Path) -> list[str]:
    """列出本体内所有 INI 的相对路径，排除 .d3dxmeta 元数据目录。

    文件夹移除本体目录前缀，归档移除唯一顶层目录前缀，使同一 Mod
    在两种存储形式下使用一致的覆盖文件路径。
    """
    p = Path(fs.normal_path(mod_path))
    rels: list[str] = []
    if fs.is_file(p) and is_resident_archive(p):
        try:
            from src.services.archive import ArchiveService

            names = ArchiveService().list_contents(p)
        except Exception:
            return []
        top = _archive_top_prefix(p)
        for n in names:
            nn = n.replace("\\", "/")
            if nn.endswith("/") or not nn.lower().endswith(".ini"):
                continue
            if META_DIR in nn.split("/"):
                continue
            rel = nn
            if top and (nn == top or nn.startswith(top + "/")):
                rel = nn[len(top) + 1 :] if nn.startswith(top + "/") else ""
            rel = _norm_rel(rel)
            if rel:
                rels.append(rel)
    elif fs.is_dir(p):
        try:
            for f in sorted(fs.iter_files(p), key=lambda item: str(item).casefold()):
                if f.suffix.lower() != ".ini" or not fs.is_file(f):
                    continue
                rel_path = Path(fs.normal_path(f)).relative_to(p)
                rel_parts = rel_path.parts
                if META_DIR in rel_parts:
                    continue
                rels.append(rel_path.as_posix())
        except OSError:
            pass
    return rels


def read_body_ini_text(mod_path: str | Path, rel: str | Path) -> str | None:
    """读取本体中的指定 INI；文件夹直接读取，归档读取对应成员，不存在时返回 None。"""
    p = Path(fs.normal_path(mod_path))
    norm = _norm_rel(rel)
    if not norm:
        return None
    if fs.is_file(p) and is_resident_archive(p):
        try:
            from src.services.archive import ArchiveService

            svc = ArchiveService()
            top = _archive_top_prefix(p)
            member = f"{top}/{norm}" if top else norm
            raw = svc.read_file(p, member)
            if raw is None:
                return None
            return raw.decode("utf-8", errors="surrogateescape")
        except Exception as e:
            logger.warning("从归档读 ini 失败: %s", e)
            return None
    if fs.is_dir(p):
        f = p / norm
        if fs.is_file(f):
            try:
                with open(fs.native_path(f), encoding="utf-8", errors="surrogateescape", newline="") as fh:
                    return fh.read()
            except OSError:
                return None
    return None


def resolve_effective_ini_text(mod_path: str | Path, rel: str | Path) -> str | None:
    """读取有效 INI 全文，优先采用覆盖文件，其次读取本体；均不存在时返回 None。"""
    ov = read_override(mod_path, rel)
    if ov is not None:
        return ov
    return read_body_ini_text(mod_path, rel)


def iter_effective_inis(mod_path: str | Path) -> list[tuple[str, str]]:
    """列出本体与覆盖目录中的全部有效 INI，以相对路径和完整文本返回。

    覆盖文件优先于同名本体文件，按键解析和部署共用此结果。
    归档批量读取全部 INI，避免逐个成员打开归档所需的重复初始化开销。
    """
    p = Path(fs.normal_path(mod_path))
    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    # 先读取覆盖文件，保留用户编辑后的内容。
    ov_texts: dict[str, str] = {}
    for rel in list_overrides(mod_path):
        if rel.lower().endswith(".ini"):
            t = read_override(mod_path, rel)
            if t is not None:
                ov_texts[rel] = t

    # 完整初始化的覆盖目录已包含全部 INI，可直接返回而无需打开归档。
    if fs.is_file(p) and is_resident_archive(p) and overrides_seeded(p):
        return list(ov_texts.items())

    if fs.is_file(p) and is_resident_archive(p):
        # 一次读取归档内全部 INI，同名覆盖文件保持优先。
        try:
            from src.services.archive import ArchiveService

            raw = ArchiveService().read_all_inis(p)
        except Exception:
            raw = {}
        top = _archive_top_prefix(p)
        for member, data in raw.items():
            nn = member.replace("\\", "/")
            rel = nn
            if top and (nn == top or nn.startswith(top + "/")):
                rel = nn[len(top) + 1 :] if nn.startswith(top + "/") else ""
            rel = _norm_rel(rel)
            if not rel or rel in seen:
                continue
            seen.add(rel)
            if rel in ov_texts:
                out.append((rel, ov_texts[rel]))
            else:
                try:
                    out.append((rel, data.decode("utf-8", errors="surrogateescape")))
                except Exception:
                    pass
    else:
        # 直接读取文件夹本体，同名覆盖文件保持优先。
        for rel in body_ini_rels(mod_path):
            if rel in seen:
                continue
            seen.add(rel)
            if rel in ov_texts:
                out.append((rel, ov_texts[rel]))
            else:
                t = read_body_ini_text(mod_path, rel)
                if t is not None:
                    out.append((rel, t))

    # 补入仅存在于覆盖目录的 INI。
    for rel, t in ov_texts.items():
        if rel not in seen:
            seen.add(rel)
            out.append((rel, t))
    return out


def _ini_stat_token(info):
    """提取 INI 文件类型、大小、时间和文件编号，生成读取一致性标记。"""
    return (info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)


def _optional_ini_stat(path):
    """仅缺失条目可视为可选，访问或类型错误不能当作缺失。"""
    try:
        return os.stat(fs.native_path(path))
    except FileNotFoundError:
        return None


def _strict_ini_tree(root, *, exclude_meta=False):
    """遍历完整目录树，并保留访问和类型错误。"""
    files = []

    def visit(directory):
        """递归枚举普通 INI 文件及版本，拒绝链接目录和非普通 INI 对象。"""
        with os.scandir(fs.native_path(directory)) as entries:
            children = sorted(entries, key=lambda entry: entry.name)
        for entry in children:
            if exclude_meta and entry.name == META_DIR:
                continue
            path = directory / entry.name
            info = os.stat(fs.native_path(path))
            if stat.S_ISDIR(info.st_mode):
                if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise OSError(tr("无法完整枚举 INI 链接目录：{path}", path=path))
                visit(path)
            elif path.suffix.casefold() == ".ini":
                if not stat.S_ISREG(info.st_mode):
                    raise OSError(tr("INI 不是普通文件：{path}", path=path))
                files.append((path.relative_to(root).as_posix(), _ini_stat_token(info)))

    visit(root)
    return tuple(files)


@dataclass(frozen=True)
class EffectiveIniInventory:
    """可缓存的严格文件清单，仅完整读取成功才能证明内容有效。

    已准备覆盖文件的归档直接使用覆盖目录；其他归档必须验证全部成员与内容后再生成声明。
    """

    path: Path
    body_version: tuple
    override_root: Path | None
    seed_version: tuple | None
    overrides: tuple
    originals: tuple

    def read(self) -> tuple[tuple[str, str], ...]:
        """按覆盖层优先级读取全部有效 INI，结束时重新检查清单以拒绝读取期间变化。"""

        def read_text(path):
            """保留原始换行读取 UTF-8 INI，并以代理转义保留不可解码字节。"""
            with open(fs.native_path(path), encoding="utf-8", errors="surrogateescape", newline="") as stream:
                return stream.read()

        # 已有覆盖文件必须可读，不能因原始文件存在而忽略错误。
        texts = {rel: read_text(self.override_root / rel) for rel, _ in self.overrides}
        if stat.S_ISDIR(self.body_version[0]):
            for rel, _ in self.originals:
                if rel not in texts:
                    texts[rel] = read_text(self.path / rel)
        elif self.seed_version is None:
            for rel, text in _read_complete_archive_inis(self.path):
                texts.setdefault(rel, text)
        if inspect_effective_inis(self.path) != self:
            raise OSError(tr("有效 INI 在读取期间发生变化，请重试：{path}", path=self.path))
        return tuple(texts.items())


def inspect_effective_inis(mod_path: str | Path) -> EffectiveIniInventory:
    """构造有效 INI 的严格签名，保留全部文件错误。

    业务操作使用完整清单与读取结果，不能仅凭可读取的部分内容继续执行。
    """
    path = Path(fs.normal_path(mod_path))
    body = os.stat(fs.native_path(path))
    folder = stat.S_ISDIR(body.st_mode)
    if not folder and not (stat.S_ISREG(body.st_mode) and is_resident_archive(path)):
        raise OSError(tr("无法读取 Mod INI 本体：{path}", path=path))

    # 直接检查元数据路径，避免 is_file 或 is_dir 隐藏访问错误。
    container = path if folder and _optional_ini_stat(path / MOD_CONTAINER_MARKER) else path.parent
    marker_path = container / MOD_CONTAINER_MARKER
    marker_stat = _optional_ini_stat(marker_path)
    marker = read_container_marker(container) if marker_stat is not None else None
    base = None
    if marker is not None:
        if container == path:
            return inspect_effective_inis(container / marker["body"])
        if os.path.normcase(str(container / marker["body"])) == os.path.normcase(str(path)):
            base = container / META_DIR / INI_OVERRIDES_SUBDIR
    overrides, seed = (), None
    if base is not None:
        base_stat = _optional_ini_stat(base)
        if base_stat is not None:
            if not stat.S_ISDIR(base_stat.st_mode):
                raise OSError(tr("INI override 目录无效：{base}", base=base))
            overrides = _strict_ini_tree(base)
            seed_stat = _optional_ini_stat(base / INI_OVERRIDES_SEEDED_MARKER)
            if seed_stat is not None:
                if not stat.S_ISREG(seed_stat.st_mode):
                    raise OSError(tr("INI 播种标记无效：{base}", base=base))
                seed = _ini_stat_token(seed_stat)
    originals = _strict_ini_tree(path, exclude_meta=True) if folder else ()
    return EffectiveIniInventory(path, _ini_stat_token(body), base, seed, overrides, originals)


def _read_complete_archive_inis(path):
    """校验压缩包成员路径、重复项及 INI 大小和校验码，完整读取后返回相对路径与文本。"""
    from src.services.archive import ArchiveService

    service = ArchiveService()
    entries = service.list_entries(path)
    if entries is None:
        raise OSError(tr("无法确认归档 INI 成员完整性，请检查归档或重试：{path}", path=path))
    expected = {}
    names = set()
    for name, size, crc in entries:
        name = _validate_relative_member(name.replace("\\", "/"), tr("归档成员"))
        if name.casefold() in names:
            raise ValueError(tr("归档成员重复或大小写冲突：{name}", name=name))
        names.add(name.casefold())
        if name.casefold().endswith(".ini") and META_DIR not in name.split("/"):
            expected[name] = (size, crc)
    prefix = _detect_single_top({name for name, _, _ in entries if not _is_under_meta(name)})
    raw = service.read_all_inis(path)
    normalized = {name.replace("\\", "/"): data for name, data in raw.items()}
    if len(normalized) != len(raw) or set(normalized) != set(expected):
        raise OSError(tr("归档 INI 未完整读取，请重试：{path}", path=path))
    texts = []
    for name, (size, crc) in expected.items():
        data = normalized[name]
        if len(data) != size or zlib.crc32(data) != crc:
            raise OSError(tr("归档 INI 内容不完整：{path} / {name}", path=path, name=name))
        rel = name[len(prefix) + 1 :] if prefix else name
        texts.append((rel, data.decode("utf-8", errors="surrogateescape")))
    return tuple(texts)


# 随容器保存的本地元数据。


def _is_under_meta(name: str) -> bool:
    """ZIP/相对成员名是否落在 .d3dxmeta 目录内（任一路径段命中）。"""
    parts = name.replace("\\", "/").casefold().split("/")
    return META_DIR in parts or MOD_CONTAINER_MARKER in parts


_KNOWN_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "kind",
        "name",
        "info",
        "previews",
        "local",
    }
)
_KNOWN_LOCAL_KEYS = frozenset({"classification", "preview_order", "status_hint", "mod_id"})


def unknown_manifest_keys(manifest: dict | None) -> dict:
    """提取清单中的未知字段，供读取后重新写入时原样保留。

    顶层字段直接返回，local 内的字段保存在 local 子字典，
    build_local_manifest 的 extra 参数按相同结构合并。
    """
    if not isinstance(manifest, dict):
        return {}
    extra = {key: value for key, value in manifest.items() if key not in _KNOWN_MANIFEST_KEYS}
    local = manifest.get("local")
    if isinstance(local, dict):
        local_extra = {key: value for key, value in local.items() if key not in _KNOWN_LOCAL_KEYS}
        if local_extra:
            extra["local"] = local_extra
    return extra


def build_local_manifest(meta: dict, local: dict | None = None, *, extra: dict | None = None) -> dict:
    """构造本地元数据清单。

    完整保存用户信息，不使用分享字段白名单。预览文件单独存储并在重建时扫描，
    因此清单中的 previews 为空，避免重复复制图片。local 保存分类路径、预览排序
    和启用提示，分享导出时移除。

    extra 包含读取时发现的未知字段，合并时保留这些字段，但不覆盖本模块管理的键。
    """
    manifest = {
        "schema": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "name": (meta or {}).get("name", ""),
        "info": deepcopy((meta or {}).get("info", {}) or {}),
        "previews": [],
    }
    manifest["info"].pop("name", None)
    if local:
        manifest["local"] = dict(local)
    for key, value in (extra or {}).items():
        if key == "local" and isinstance(value, dict):
            merged = manifest.setdefault("local", {})
            if isinstance(merged, dict):
                for local_key, local_value in value.items():
                    merged.setdefault(local_key, local_value)
            continue
        manifest.setdefault(key, value)
    if isinstance(manifest.get("local"), dict):
        manifest["local"].pop("mod_id", None)
        manifest["local"].pop("imported_at", None)
    return manifest


def write_sidecar(mod_path: str | Path, manifest: dict) -> bool:
    """原子更新描述，持久身份由 .d3dxmod 保存。"""

    p = Path(mod_path)
    validated = _validate_manifest(deepcopy(manifest))
    validated["info"].pop("name", None)
    sidecar_dir = _resolve_sidecar_dir(p)
    if sidecar_dir is None:
        raise InvalidManagedMetadataError(f"write_sidecar requires a managed container: {p}")
    local = dict(validated.get("local", {}))
    local.pop("mod_id", None)
    local.pop("imported_at", None)
    if "local" in validated:
        validated = dict(validated, local=local)
    text = json.dumps(validated, ensure_ascii=False, indent=2)
    temporary: Path | None = None
    try:
        fs.ensure_dir(sidecar_dir)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".info-",
            suffix=".tmp",
            dir=fs.native_path(sidecar_dir),
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        fs.write_text(temporary, text, encoding="utf-8")
        os.replace(fs.native_path(temporary), fs.native_path(sidecar_dir / MANIFEST_NAME))
        return True
    finally:
        if temporary is not None:
            fs.unlink(temporary, missing_ok=True)


def strip_sidecar(mod_path: str | Path) -> bool:
    """移除导出内容中的本地元数据。

    清除分类路径、启用提示等私有字段，同时避免后续追加分享数据时产生重复成员。
    """
    p = Path(mod_path)
    try:
        # 优先移除容器或文件夹的 .d3dxmeta 目录。
        # 容器中的本体归档保持不变；普通文件夹移除自身元数据目录。
        sidecar_dir = None if fs.is_dir(p) else _resolve_sidecar_dir(p)
        if sidecar_dir is not None:
            if fs.is_dir(sidecar_dir):
                fs.rmtree(sidecar_dir)
            # 容器内本体无需再访问归档成员，避免无关重写。
            if p.is_dir() or (p.is_file() and (p.parent / MOD_CONTAINER_MARKER).is_file()):
                return True
        if fs.is_dir(p):
            # 分享文件夹没有容器 marker；与归档一致，清除任意层级的本地元数据。
            def walk_error(error: OSError) -> None:
                """将目录遍历错误继续抛出，阻止把不完整扫描误判为清理成功。"""
                raise error

            for directory, dirs, files in os.walk(fs.native_path(p), onerror=walk_error):
                for name in files:
                    if name.casefold() == MOD_CONTAINER_MARKER:
                        fs.unlink(Path(directory) / name)
                for name in tuple(dirs):
                    if name.casefold() == META_DIR:
                        fs.rmtree(Path(directory) / name)
                        dirs.remove(name)
            return True
        # 独立导出归档需移除内部元数据成员。
        if p.is_file() and p.suffix.lower() == ".zip":
            # 没有元数据成员时直接返回，避免重建整个 ZIP。
            try:
                with zipfile.ZipFile(str(p), "r") as zf:
                    if not any(_is_under_meta(n) for n in zf.namelist()):
                        return True
            except (OSError, zipfile.BadZipFile):
                return False
            return _rewrite_zip_meta(p, None)
        # 7z/rar：剔除归档内部的 .d3dxmeta 成员，确保分享导出的包不带任何本地专属元数据。
        if p.is_file() and p.suffix.lower() in _NONZIP_RESIDENT_SUFFIXES:
            from src.services.archive import ArchiveService

            svc = ArchiveService()
            pred = lambda n: _is_under_meta(n)  # noqa: E731
            if p.suffix.lower() == ".7z":
                return svc.remove_members_7z(p, pred)
            return svc.remove_members_rar(p, pred)
        return sidecar_dir is not None
    except OSError as e:
        logger.warning("剥离 sidecar 失败: %s", e)
        return False


def read_sidecar(mod_path: str | Path) -> dict | None:
    """读取经过校验的容器级元数据。

    非容器或元数据缺失时返回 None；已有受管元数据无效时抛出异常。
    """
    p = Path(mod_path)
    sidecar_dir = _resolve_sidecar_dir(p)
    if sidecar_dir is None:
        return None
    mpath = sidecar_dir / MANIFEST_NAME
    if not fs.is_file(mpath):
        return None
    try:
        manifest = json.loads(fs.read_text(mpath, encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise InvalidManagedMetadataError(
            tr("无法解析容器级 sidecar: {mpath}: {e}", mpath=mpath, e=e),
        ) from e
    return _validate_manifest(manifest)
