"""在 Mod 导入和导出事务边界处理可分享的命名变体。"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import replace
from pathlib import Path

from src.core.mod_package import (
    MANIFEST_KIND,
    MANIFEST_NAME,
    META_DIR,
    VARIANT_SUBDIR,
    InvalidVariantPackageError,
    _detect_single_top,
    _resolve_sidecar_dir,
    read_container_marker,
    validate_variant_references,
)
from src.core.mod_variant_store import VARIANT_STORE_LOCK, ModVariantStore, _unique_json_object
from src.core.persist_vars import ModIniSource, build_attribution_index, resolve_variant_values
from src.models.mod_variant import ModVariant, normalize_ini_path
from src.utils import fs
from src.utils.constants import is_resident_archive
from src.utils.i18n import t as tr


def saved_variants(mod_path: str | Path) -> tuple[ModVariant, ...]:
    """没有容器时视为没有保存变体，已有容器损坏时仍报告失败。"""
    if _resolve_sidecar_dir(mod_path) is None:
        return ()
    return tuple(ModVariantStore(mod_path).list())


def _plain_directory(directory: Path) -> bool:
    """确认变体目录是独立普通目录，缺失时返回 False，链接或重解析对象会被拒绝。"""
    try:
        info = os.lstat(fs.native_path(directory))
    except FileNotFoundError:
        return False
    reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(info.st_mode) or reparse or not stat.S_ISDIR(info.st_mode):
        raise InvalidVariantPackageError(tr("变体元数据目录不是独立目录：{name}", name=directory.name))
    return True


def _variant_manifest(raw: str | bytes, *, has_directory: bool):
    """描述损坏可降级处理，但不能丢弃变体存在的证据。"""
    try:
        text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw.lstrip("\ufeff")
        return json.loads(text, object_pairs_hook=_unique_json_object)
    except (UnicodeError, ValueError) as error:
        # 即使引用文件全都缺失，不完整的声明仍须保留。
        # 解码 JSON 键以识别转义写法，普通作者文本错误仍按描述降级处理。
        text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw
        declared = False
        for match in re.finditer(r'("(?:[^"\\]|\\.)*")\s*:', text):
            try:
                declared |= json.loads(match.group(1)) == "variants"
            except ValueError:
                continue
        if not has_directory and not declared:
            return None
        raise InvalidVariantPackageError(tr("无法校验变体分享清单：{error}", error=error)) from error


def validate_archive_variant_members(archive: Path, service) -> bool:
    """解压前拒绝重复成员，防止覆盖后丢失校验证据。"""
    inventory = [(raw, raw.replace("\\", "/")) for raw in service.list_contents(archive)]
    names = [name for _raw, name in inventory]
    members, manifests = [], []
    seen = set()
    directory_prefixes = set()
    for raw, name in inventory:
        parts = name.split("/")
        lowered = [part.casefold() for part in parts]
        if META_DIR not in lowered:
            continue
        index = lowered.index(META_DIR)
        if parts[index] != META_DIR:
            raise InvalidVariantPackageError(tr("归档变体元数据目录存在大小写别名"))
        is_variant = len(parts) > index + 2 and lowered[index + 1] == VARIANT_SUBDIR
        is_manifest = lowered[index + 1 :] == [MANIFEST_NAME]
        if is_variant or is_manifest:
            if (
                index > 1
                or any(part in ("", ".", "..") or ":" in part for part in parts)
                or (is_manifest and parts[-1] != MANIFEST_NAME)
            ):
                raise InvalidVariantPackageError(tr("归档变体成员路径越界或无法确认资源根目录"))
            key = name.casefold()
            if key in seen:
                raise InvalidVariantPackageError(tr("归档变体包含重复成员或大小写身份冲突"))
            seen.add(key)
            prefix = "/".join(parts[:index])
            if is_manifest:
                manifests.append((raw, prefix))
            else:
                directory_prefixes.add(prefix)
                if name.casefold().endswith(".json"):
                    validate_variant_references(["/".join(parts[index + 1 :])])
                    members.append((name, prefix))
    prefixes = {prefix for _name, prefix in members}
    requires_files = bool(members)
    for raw, prefix in manifests:
        payload = service.read_file(archive, raw)
        if payload is None:
            raise InvalidVariantPackageError(tr("无法完整读取归档变体分享清单"))
        manifest = _variant_manifest(payload, has_directory=prefix in directory_prefixes)
        if isinstance(manifest, dict) and "variants" in manifest:
            references = validate_variant_references(manifest["variants"])
            prefixes.add(prefix)
            requires_files |= bool(references)
    if not prefixes:
        return False
    if len(prefixes) != 1:
        raise InvalidVariantPackageError(tr("归档包含多个不同资源根目录的变体"))
    prefix = next(iter(prefixes))
    if prefix and any(name.split("/", 1)[0] != prefix for name in names):
        raise InvalidVariantPackageError(tr("归档变体所在目录不是唯一 Mod 资源根目录"))
    try:
        ini_paths = [
            normalize_ini_path(name).casefold()
            for name in names
            if name.casefold().endswith(".ini") and META_DIR not in name.casefold().split("/")
        ]
        if len(ini_paths) != len(set(ini_paths)):
            raise ValueError(tr("同包资源存在重复或大小写冲突的 INI 路径"))
    except ValueError as error:
        raise InvalidVariantPackageError(tr("变体与同包 Mod 资源不兼容：{error}", error=error)) from error
    return requires_files


def read_package_variants(*candidate_dirs: Path) -> tuple[ModVariant, ...]:
    """移除分享数据前冻结全部已声明文件。"""
    found = None
    seen_dirs = set()
    for folder in candidate_dirs:
        folder = Path(folder)
        key = str(folder.absolute()).casefold()
        if key in seen_dirs:
            continue
        seen_dirs.add(key)
        meta = folder / META_DIR
        directory = meta / VARIANT_SUBDIR
        try:
            for child in fs.list_children(folder):
                if child.name.casefold() == META_DIR and child.name != META_DIR:
                    raise InvalidVariantPackageError(tr("变体元数据目录存在大小写别名"))
            if _plain_directory(meta):
                for child in fs.list_children(meta):
                    if child.name.casefold() in (VARIANT_SUBDIR, MANIFEST_NAME) and child.name not in (
                        VARIANT_SUBDIR,
                        MANIFEST_NAME,
                    ):
                        raise InvalidVariantPackageError(tr("变体元数据成员存在大小写别名"))
            has_directory = _plain_directory(directory)
            try:
                raw = fs.read_bytes(meta / MANIFEST_NAME)
            except FileNotFoundError:
                manifest = None
            else:
                manifest = _variant_manifest(raw, has_directory=has_directory)
        except (OSError, ValueError) as error:
            raise InvalidVariantPackageError(tr("无法校验变体分享清单：{error}", error=error)) from error
        declared = isinstance(manifest, dict) and "variants" in manifest
        if not declared and not has_directory:
            continue
        if found is not None:
            raise InvalidVariantPackageError(tr("同一 Mod 包含多个变体信封，无法确认所属资源"))
        try:
            if not declared:
                # 本地容器不需要分享清单，完整外部副本仍携带所有已保存文件。
                if read_container_marker(folder) is None:
                    raise InvalidVariantPackageError(tr("变体文件缺少完整分享清单"))
                found = tuple(ModVariantStore(folder).list())
                continue
            if manifest.get("kind") != MANIFEST_KIND:
                raise InvalidVariantPackageError(tr("变体分享信封标识无效"))
            references = validate_variant_references(manifest["variants"])
            actual = (
                [path.name for path in fs.list_children(directory) if path.suffix.casefold() == ".json"]
                if has_directory
                else []
            )
            if len(actual) != len({name.casefold() for name in actual}):
                raise InvalidVariantPackageError(tr("变体文件存在重复或大小写身份冲突"))
            if set(actual) != {Path(name).name for name in references}:
                raise InvalidVariantPackageError(tr("变体清单与文件不完整或存在未声明的变体"))
            variants = []
            for relative in references:
                path = meta / relative
                if path.is_symlink() or path.resolve().parent != directory.resolve():
                    raise InvalidVariantPackageError(tr("变体文件越出所属目录"))
                variant = ModVariant.from_dict(
                    json.loads(fs.read_text(path, encoding="utf-8"), object_pairs_hook=_unique_json_object)
                )
                if variant.variant_id != path.stem:
                    raise InvalidVariantPackageError(tr("变体文件名与 variant_id 不一致"))
                variants.append(variant)
            found = tuple(variants)
        except (OSError, ValueError) as error:
            if isinstance(error, InvalidVariantPackageError):
                raise
            raise InvalidVariantPackageError(tr("变体文件无法完整读取：{error}", error=error)) from error
    return found or ()
