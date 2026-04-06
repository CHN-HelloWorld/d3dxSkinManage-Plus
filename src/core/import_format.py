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


def _folder_has_reserved_payload(path: Path) -> bool:
    """检查目录的直接子项是否占用了应用保留的元数据名称。"""
    return any(child.name.casefold() in (META_DIR, MOD_CONTAINER_MARKER) for child in fs.list_children(path))


#: 最近一次因元数据非法而拒绝导入的原因（线程本地）。
#: prepare_import_source 的失败契约是返回 None，无法带回原因；纳管服务在获取 None 后
#: 读取本值拼进 IntakeError，用户才能看到「哪个文件坏了、怎么处置」而不是通用失败。
#: 用 threading.local：并行纳管下多个 worker 各自 prepare，全局变量会互相串写。
_reject_reason = threading.local()


def _record_reject_reason(reason: str) -> None:
    """在线程局部状态中保存本次导入被拒绝的原因。"""
    _reject_reason.value = reason


def take_reject_reason() -> str:
    """取出并清空本线程最近一次拒绝原因（取过即清，避免污染下一次导入）。"""
    reason = getattr(_reject_reason, "value", "")
    _reject_reason.value = ""
    return reason


#: 描述类元数据损坏后的降级告知。落在 PreparedImportSource/PreparedIntake 上随结果走，
# 向任务明细报告元数据降级，区别于源文件未提供相关信息的情况。
DEGRADED_METADATA_NOTICE = N_(
    "随包的分享元数据（作者/来源/标签/补充预览图）已损坏或不是当前格式，已整份丢弃并按普通 Mod 导入；Mod 内容本身完好。"
)


def _add_notice(notices: list[str], text: str) -> None:
    """去重追加：同一条降级在多个候选目录上会被重复发现。"""
    text = tr(text)
    if text not in notices:
        notices.append(text)


def _probe_current_container(
    *candidate_dirs: Path,
    notices: list[str],
) -> Path | None:
    """解析导入容器并返回本体路径，非容器输入返回 None。

    结构标记决定本体位置，有效本体直接采用；缺失时局部重新识别，
    候选不唯一或权限错误时拒绝导入。

    新导入仅采用作者、来源、标签及预览等描述信息，身份由纳管服务生成。
    无效描述整体忽略；接管已有受管源使用独立流程，必须验证并沿用源身份。
    """
    body: Path | None = None
    for directory in candidate_dirs:
        if not fs.is_file(directory / MOD_CONTAINER_MARKER):
            continue
        try:
            marker = read_container_marker(directory)
        except InvalidManagedMetadataError as e:
            # 错误提示使用标记文件名，避免引用导入结束后会清理的暂存路径。
            raise InvalidManagedMetadataError(
                tr(
                    (
                        "这份内容自称是受管 Mod 容器，但其中的 {MOD_CONTAINER_MARKER} "
                        "已损坏、无法解析，为避免造出不完整的容器已拒绝导入。\n"
                        "可从源文件中删除 {MOD_CONTAINER_MARKER_2} 后作为普通 Mod "
                        "重新导入，或向分享者索取完整的容器。"
                    ),
                    MOD_CONTAINER_MARKER=MOD_CONTAINER_MARKER,
                    MOD_CONTAINER_MARKER_2=MOD_CONTAINER_MARKER,
                ),
            ) from e
        if marker is None:
            continue
        candidate = directory / marker["body"]
        try:
            sidecar = read_sidecar(candidate)
        except InvalidManagedMetadataError as e:
            logger.warning(
                "容器级 %s/info.json 损坏，按无元数据导入 (%s): %s",
                META_DIR,
                directory,
                e,
            )
            sidecar = None
        if sidecar is None:
            _add_notice(notices, DEGRADED_METADATA_NOTICE)
        if body is None:
            body = candidate
    return body


def _unwrap_container_body(
    body: Path,
    work_root: Path,
    target_format: str,
    strict_format: bool,
    archive_service,
    *,
    variants: tuple[ModVariant, ...] = (),
    checkpoint=None,
    should_cancel=None,
) -> tuple[Path, str, str, bool] | None:
    """从输入容器中解析本体，并按本体格式重新决定导入方式。

    容器元数据及外层目录不属于资源内容，不能重复包装到本体中，
    否则会改变目录层级和内容哈希。返回本体路径、源格式、目标格式及是否直接保存归档；
    格式不受支持时返回 None。
    """
    body_fmt = source_format(body)
    if not body_fmt:
        logger.error("容器 marker 指定的本体格式不受支持: %s", body)
        return None
    final_fmt = final_import_format(body_fmt, target_format, strict_format)
    if body_fmt == "folder" or final_fmt == body_fmt:
        # 文件夹本体后续按 final_fmt 压缩；归档本体格式已等于目标 → 零解压直存。
        return body, body_fmt, final_fmt, body_fmt != "folder"
    # 转码前校验原始清单，避免重复 INI 成员被覆盖后丢失变体绑定证据。
    validate_package_variants(body, variants)
    # 归档本体需要换格式：解压一次，向下游交回统一的文件夹路径。
    extracted = archive_service.extract(
        str(body),
        str(work_root / "body_extracted"),
        checkpoint=checkpoint,
        should_cancel=should_cancel,
    )
    if extracted is None:
        logger.error("解压容器内本体失败: %s", body)
        return None
    return Path(extracted), body_fmt, final_fmt, False
