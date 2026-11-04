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
    read_envelope,
    read_sidecar,
    resolve_ini_overrides_dir,
    resolve_preview_paths,
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


@dataclass
class PreparedImportSource:
    """已清洗、可计算内容哈希的导入源；压缩物化在查重后单独执行。"""

    clean_path: Path
    source_format: str
    final_format: str
    passthrough_archive: bool = False
    work_root: Path | None = None
    share_manifest: dict | None = None
    share_previews: list[Path] = field(default_factory=list)
    share_variants: tuple[ModVariant, ...] = ()
    staged_body_previews: list[tuple[str, Path]] = field(default_factory=list)
    staged_previews_complete: bool = False
    excluded_preview_references: list[dict[str, str]] = field(default_factory=list)
    content_version: tuple | None = None
    staged_ini_overrides: Path | None = None
    metadata_staged: bool = False
    #: 非空表示描述类元数据被整份丢弃（见 DEGRADED_METADATA_NOTICE）；内容仍完好。
    metadata_notice: str = ""

    def cleanup(self) -> None:
        """清理本次导入的临时工作目录；未创建工作目录时不执行操作。"""
        if self.work_root is not None:
            fs.rmtree(self.work_root, ignore_errors=True)

    def ensure_work_root(self, temp_parent: str | Path) -> Path:
        """为压缩前元数据暂存按需创建工作目录。"""
        if self.work_root is None:
            self.work_root = Path(temp_parent) / f"import_{uuid.uuid4().hex}"
            fs.ensure_dir(self.work_root, exist_ok=False)
        return self.work_root


def _check_continue(checkpoint=None, should_cancel=None) -> None:
    """检查取消回调和工作检查点，任一要求停止时抛出中断异常。"""
    if should_cancel is not None and should_cancel():
        raise InterruptedError("import metadata staging cancelled")
    if checkpoint is not None and not checkpoint():
        raise InterruptedError("import metadata staging cancelled")


def _safe_relative_name(value: str | Path) -> str:
    """统一路径分隔符并移除空段、点号和父目录段，生成用于成员分组的相对名称。"""
    raw = str(value).replace("\\", "/").strip("/")
    parts = [part for part in raw.split("/") if part not in ("", ".", "..")]
    return "/".join(parts)


def _single_archive_top(names: list[str]) -> str:
    """返回所有归一化压缩成员共同的顶层名称，顶层不唯一时返回空字符串。"""
    tops = {normalized.split("/", 1)[0] for name in names if (normalized := _safe_relative_name(name))}
    return next(iter(tops)) if len(tops) == 1 else ""


def stage_import_metadata(
    prepared: PreparedImportSource,
    archive_service,
    temp_parent: str | Path,
    *,
    include_previews: bool = True,
    checkpoint=None,
    should_cancel=None,
) -> bool:
    """压缩前暂存本体预览图和归档 INI 覆盖文件。

    仅从 prepared.clean_path 读取，后续压缩完成后复用这些文件，避免再次打开归档。
    """
    if prepared.metadata_staged:
        return True

    source = Path(fs.normal_path(prepared.clean_path))
    # 仅为受支持的归档输出格式准备 INI 覆盖数据。
    want_ini_stage = prepared.final_format in ARCHIVE_IMPORT_FORMATS
    try:
        source_version = capture_source_version(source)
        if prepared.content_version is not None and source_version != prepared.content_version:
            raise OSError("Import source changed after fingerprinting")
        work_root = prepared.ensure_work_root(temp_parent)
        preview_root = work_root / "body_previews"
        ini_root = work_root / "ini_overrides"
        staged_previews: list[tuple[str, Path]] = []
        previews_complete = include_previews
        ini_complete = True

        if fs.is_dir(source):
            for path in sorted(fs.iter_files(source), key=lambda p: str(p).casefold()):
                _check_continue(checkpoint, should_cancel)
                try:
                    rel = _safe_relative_name(path.relative_to(source).as_posix())
                except ValueError:
                    continue
                if not rel or META_DIR in rel.split("/"):
                    continue
                suffix = path.suffix.lower()
                if include_previews and len(staged_previews) < 5 and suffix in IMAGE_EXTENSIONS and suffix != ".dds":
                    staged = preview_root / Path(rel)
                    fs.copy_path(
                        path,
                        staged,
                        checkpoint=checkpoint,
                        should_cancel=should_cancel,
                    )
                    staged_previews.append((rel, staged))
                if want_ini_stage and suffix == ".ini":
                    fs.copy_path(
                        path,
                        ini_root / Path(rel),
                        checkpoint=checkpoint,
                        should_cancel=should_cancel,
                    )

        elif fs.is_file(source) and is_resident_archive(source):
            _check_continue(checkpoint, should_cancel)
            names = archive_service.list_contents(source)
            normalized_names = [(name, _safe_relative_name(name)) for name in names]
            if include_previews:
                preview_names = [
                    original
                    for original, normalized in normalized_names
                    if normalized
                    and META_DIR not in normalized.split("/")
                    and Path(normalized).suffix.lower() in IMAGE_EXTENSIONS
                    and Path(normalized).suffix.lower() != ".dds"
                ][:5]
                if preview_names:
                    extracted = {
                        Path(fs.normal_path(path))
                        for path in archive_service.extract_files(source, preview_names, preview_root)
                    }
                    for original in preview_names:
                        rel = _safe_relative_name(original)
                        staged = preview_root / Path(rel)
                        # 解压失败可能留下不完整文件，仅发布已确认完成的成员。
                        if staged in extracted and fs.is_file(staged):
                            staged_previews.append((rel, staged))
                    previews_complete = len(staged_previews) == len(preview_names)

            if want_ini_stage:
                expected = {
                    normalized
                    for _original, normalized in normalized_names
                    if normalized.lower().endswith(".ini") and META_DIR not in normalized.split("/")
                }
                raw_inis = archive_service.read_all_inis(source)
                top = _single_archive_top(names)
                staged_members: set[str] = set()
                for member, data in raw_inis.items():
                    _check_continue(checkpoint, should_cancel)
                    normalized = _safe_relative_name(member)
                    rel = normalized
                    if top and normalized.startswith(top + "/"):
                        rel = normalized[len(top) + 1 :]
                    rel = _safe_relative_name(rel)
                    if not rel or META_DIR in rel.split("/"):
                        continue
                    fs.write_bytes(ini_root / Path(rel), data)
                    staged_members.add(normalized)
                ini_complete = expected.issubset(staged_members)

        else:
            logger.error("无法暂存导入元数据，不支持的准备源: %s", source)
            return False

        if want_ini_stage:
            if ini_complete:
                fs.ensure_dir(ini_root)
                fs.write_text(
                    ini_root / INI_OVERRIDES_SEEDED_MARKER,
                    "1",
                    encoding="utf-8",
                )
                prepared.staged_ini_overrides = ini_root
            else:
                logger.warning("压缩前 ini 提取不完整，未写 seeded 标记: %s", source)

        if capture_source_version(source) != source_version:
            raise OSError("Import source changed while staging previews")
        prepared.staged_body_previews = staged_previews
        prepared.staged_previews_complete = previews_complete
        prepared.metadata_staged = True
        return True
    except InterruptedError:
        return False
    except Exception as e:
        logger.error("压缩前暂存导入元数据失败 (%s): %s", source, e)
        return False


def materialize_staged_previews(
    prepared: PreparedImportSource,
    body_path: str | Path,
    *,
    cache_dir: str | Path | None = None,
    content_hash: str = "",
    checkpoint=None,
    should_cancel=None,
) -> list[Path]:
    """完整预览复制完成后一次性发布，失败或取消不留下可复用的部分缓存。"""
    body = Path(fs.normal_path(body_path))
    if not prepared.staged_previews_complete or not prepared.staged_body_previews:
        return []
    results: list[Path] = []
    if fs.is_dir(body):
        for rel, _staged in prepared.staged_body_previews:
            candidate = body / Path(rel)
            if fs.is_file(candidate):
                results.append(candidate)
        return results
    if cache_dir is None:
        return []
    cache = Path(fs.normal_path(cache_dir))
    with preview_cache_staging(cache) as stage:
        for rel, staged in prepared.staged_body_previews:
            _check_continue(checkpoint, should_cancel)
            target = stage / Path(rel)
            fs.copy_path(
                staged,
                target,
                checkpoint=checkpoint,
                should_cancel=should_cancel,
            )
            results.append(target)
        _check_continue(checkpoint, should_cancel)
        return publish_preview_cache(stage, cache, results, content_hash=content_hash)


def install_staged_ini_overrides(
    prepared: PreparedImportSource,
    body_path: str | Path,
    *,
    checkpoint=None,
    should_cancel=None,
) -> bool:
    """把已暂存的 ini 镜像装入容器；只复制小文件，不读取最终归档。"""
    stage = prepared.staged_ini_overrides
    if stage is None:
        return False
    target = resolve_ini_overrides_dir(fs.normal_path(body_path))
    if target is None:
        return False
    marker = stage / INI_OVERRIDES_SEEDED_MARKER
    try:
        # seeded 最后写入磁盘：取消/失败时不会把半份 override 误标成完整。
        for source in sorted(fs.iter_files(stage), key=lambda p: str(p).casefold()):
            if source.name == INI_OVERRIDES_SEEDED_MARKER:
                continue
            _check_continue(checkpoint, should_cancel)
            rel = source.relative_to(stage)
            fs.copy_path(
                source,
                target / rel,
                checkpoint=checkpoint,
                should_cancel=should_cancel,
            )
        _check_continue(checkpoint, should_cancel)
        fs.ensure_dir(target)
        if fs.is_file(marker):
            fs.copy_path(marker, target / marker.name)
        else:
            fs.write_text(
                target / INI_OVERRIDES_SEEDED_MARKER,
                "1",
                encoding="utf-8",
            )
        return True
    except InterruptedError:
        return False
    except Exception as e:
        logger.warning("安装压缩前暂存的 ini override 失败: %s", e)
        return False


def _take_envelope_or_degrade(
    candidate_dirs,
    stage: Path,
    notices: list[str],
    preview_sources: dict[Path, Path] | None = None,
) -> tuple[dict | None, list[Path]]:
    """读取分享元数据，无效时记录说明并忽略该元数据。

    分享数据不决定资源内容有效性；调用方随后仍须移除其保留目录。
    """
    try:
        originals = []
        if preview_sources is not None:
            envelope = read_envelope(*candidate_dirs)
            if envelope:
                originals = resolve_preview_paths(envelope["meta_dir"], envelope["manifest"])
        manifest, staged = take_envelope(candidate_dirs, stage)
        if preview_sources is not None:
            preview_sources.update(zip(originals, staged))
        return manifest, staged
    except InvalidVariantPackageError:
        raise
    except InvalidManagedMetadataError as e:
        logger.warning("分享信封损坏，按无元数据导入: %s", e)
        _add_notice(notices, DEGRADED_METADATA_NOTICE)
        return None, []


def prepare_import_source(
    source_path: str | Path,
    archive_service,
    target_format: str,
    strict_format: bool,
    temp_parent: str | Path,
    *,
    excluded_previews: tuple[str, ...] = (),
    extract_progress_cb=None,
    checkpoint=None,
    should_cancel=None,
) -> PreparedImportSource | None:
    """准备用于内容校验的导入源；可直接保存的归档不解压。

    调用方先根据 clean_path 计算哈希并检查重复，再生成最终本体。
    """
    src = Path(source_path)
    from src.core.library_schema import valid_relative

    for name in excluded_previews:
        if not valid_relative(name) or Path(name).suffix.lower() not in IMAGE_EXTENSIONS:
            raise ValueError(f"Invalid excluded preview: {name!r}")
    src_fmt = source_format(src)
    if not src_fmt:
        logger.error("不支持的导入源: %s", src)
        return None

    target = normalize_import_format(target_format)
    final_fmt = final_import_format(src_fmt, target, strict_format)
    is_archive = src_fmt in ARCHIVE_IMPORT_FORMATS
    reserved = archive_has_reserved_payload(src, archive_service) if is_archive else _folder_has_reserved_payload(src)

    # 最常见的 GameBanana/普通压缩包快路径：保留原档，零解压零重压。
    if is_archive and final_fmt == src_fmt and not reserved:
        return PreparedImportSource(
            clean_path=src,
            source_format=src_fmt,
            final_format=final_fmt,
            passthrough_archive=True,
            excluded_preview_references=[
                {"scope": "preview_cache", "path": name.replace("\\", "/")} for name in excluded_previews
            ],
        )

    needs_work_root = is_archive or reserved or final_fmt in ARCHIVE_IMPORT_FORMATS
    work_root = None
    if needs_work_root:
        work_root = Path(temp_parent) / f"import_{uuid.uuid4().hex}"
        fs.ensure_dir(work_root, exist_ok=False)

    manifest = None
    variants: tuple[ModVariant, ...] = ()
    staged_previews: list[Path] = []
    notices: list[str] = []
    passthrough = False
    preview_sources = {} if excluded_previews else None
    try:
        if is_archive:
            archive_variants = validate_archive_variant_members(src, archive_service) if reserved else False
            extract_root = work_root / "extracted"
            preview_source_root = extract_root
            extracted = archive_service.extract(
                str(src),
                str(extract_root),
                progress_cb=extract_progress_cb,
                checkpoint=checkpoint,
                should_cancel=should_cancel,
            )
            if extracted is None:
                logger.error("解压失败: %s", src)
                if work_root is not None:
                    fs.rmtree(work_root, ignore_errors=True)
                return None
            clean_path = Path(extracted)
            body = _probe_current_container(extract_root, clean_path, notices=notices)
            stage = work_root / "share_previews"
            variants = read_package_variants(extract_root, clean_path)
            if archive_variants and not variants:
                raise InvalidVariantPackageError(tr("归档内的变体文件无法与同包 Mod 资源对应"))
            manifest, staged_previews = _take_envelope_or_degrade(
                (extract_root, clean_path), stage, notices, preview_sources
            )
            # take_envelope 在没有 info.json 时不会清 marker；这里无条件清一次保留结构。
            strip_envelope(extract_root, clean_path)
        else:
            clean_path = src
            preview_source_root = src
            body = None
            validate_folder_variant_roots(src)
            if reserved:
                _probe_current_container(src, notices=notices)
                source_variants = read_package_variants(src)
                staged = work_root / "folder" / src.name
                fs.ensure_dir(staged.parent)
                fs.copy_path(
                    src,
                    staged,
                    checkpoint=checkpoint,
                    should_cancel=should_cancel,
                )
                clean_path = staged
                preview_source_root = staged
                body = _probe_current_container(clean_path, notices=notices)
                stage = work_root / "share_previews"
                variants = read_package_variants(clean_path)
                if variants != source_variants:
                    raise InvalidManagedMetadataError(tr("复制过程中变体文件发生变化，已取消导入"))
                manifest, staged_previews = _take_envelope_or_degrade((clean_path,), stage, notices, preview_sources)
                strip_envelope(clean_path)

        # 完整容器输入须先读取分享元数据，再移除管理结构并解析内部本体。
        if body is not None:
            unwrapped = _unwrap_container_body(
                body,
                work_root,
                target,
                strict_format,
                archive_service,
                variants=variants,
                checkpoint=checkpoint,
                should_cancel=should_cancel,
            )
            if unwrapped is None:
                if work_root is not None:
                    fs.rmtree(work_root, ignore_errors=True)
                return None
            clean_path, src_fmt, final_fmt, passthrough = unwrapped

        validate_package_variants(clean_path, variants)
        excluded_references = []
        for name in excluded_previews:
            original = preview_source_root / name
            if original in (preview_sources or {}):
                excluded_references.append(
                    {"scope": "container", "path": f"{META_DIR}/previews/{preview_sources[original].name}"}
                )
            elif fs.is_dir(clean_path) and original.is_relative_to(clean_path):
                excluded_references.append(
                    {"scope": "preview_cache", "path": original.relative_to(clean_path).as_posix()}
                )
        return PreparedImportSource(
            clean_path=clean_path,
            source_format=src_fmt,
            final_format=final_fmt,
            passthrough_archive=passthrough,
            work_root=work_root,
            share_manifest=manifest,
            share_previews=staged_previews,
            share_variants=variants,
            metadata_notice="\n".join(notices),
            excluded_preview_references=excluded_references,
        )
    except InvalidManagedMetadataError as e:
        # 元数据校验失败时按统一契约返回 None，同时保存具体拒绝原因供上层显示。
        logger.error("拒绝导入受管容器 (%s): %s", src, e)
        _record_reject_reason(str(e))
        if work_root is not None:
            fs.rmtree(work_root, ignore_errors=True)
        return None
    except Exception as e:
        logger.error("准备导入源失败 (%s): %s", src, e)
        if work_root is not None:
            fs.rmtree(work_root, ignore_errors=True)
        return None


def materialize_import_body(
    prepared: PreparedImportSource,
    archive_service,
    *,
    progress_cb=None,
    should_cancel=None,
    checkpoint=None,
) -> Path | None:
    """在查重后把准备源物化成最终库内形态（恒为压缩档）。"""
    if prepared.passthrough_archive:
        return prepared.clean_path
    if not fs.is_dir(prepared.clean_path):
        logger.error("待压缩导入源不是文件夹: %s", prepared.clean_path)
        return None

    work_root = prepared.work_root
    if work_root is None:
        logger.error("压缩导入缺少临时工作目录: %s", prepared.clean_path)
        return None
    out_dir = work_root / "materialized"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ArchiveService 使用 Path.with_suffix；以无点占位名压缩，再改为真实 Mod 基名，
    # 避免 ``Skin.v1`` 被误截成 ``Skin.zip``。
    raw = archive_service.compress(
        str(prepared.clean_path),
        str(out_dir / "payload"),
        prepared.final_format,
        progress_cb=progress_cb,
        should_cancel=should_cancel,
        checkpoint=checkpoint,
    )
    if raw is None:
        return None
    raw = Path(raw)
    desired = out_dir / f"{prepared.clean_path.name}{raw.suffix.lower()}"
    if raw != desired:
        desired.unlink(missing_ok=True)
        raw.replace(desired)
    return desired
