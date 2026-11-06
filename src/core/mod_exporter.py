"""将资源库中的 Mod 导出为文件夹或归档。

文件夹源按目标格式复制或压缩；归档源在格式一致或不要求转换时直接复制，
转换格式时先解压再压缩。归档保留单一顶层目录，便于解压和重新导入。
内容哈希由 Mod.compute_sha1 统一处理顶层目录，保持不同存储形式的身份一致。

本模块不依赖界面或数据库连接，可在工作线程调用。
"""

from __future__ import annotations

import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

from src.core.mod_package import (
    append_envelope_to_archive,
    append_envelope_to_zip,
    resolve_body_path,
    strip_sidecar,
    supplemental_preview_paths,
    write_envelope_to_folder,
)
from src.core.mod_variant_package import saved_variants
from src.services.archive import ArchiveService
from src.utils.i18n import t as tr
from src.utils.logger import logger

# Windows 文件名非法字符（与 mod_manager._sanitize_folder_name 保持一致）
_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_name(name: str, fallback: str = "Mod") -> str:
    """将显示名称转换为合法的 Windows 文件或目录名称。"""
    if not name:
        return fallback
    cleaned = _INVALID_NAME_CHARS.sub("_", name).strip().rstrip(".")
    return cleaned[:200] if cleaned else fallback


@dataclass
class ExportItem:
    """可跨线程传递的导出项。

    属性：
        name: 显示名称，规范化后用于输出命名。
        src_path: 资源库中的源文件夹或归档路径。
        rel_dir: 相对于目标目录的分类路径；空字符串表示平铺。为 None 时，
            根据 preserve_tree 决定是否保留相对于 library_root 的目录结构。
        meta: 可分享元数据，仅在 include_meta 启用且内容非空时写入 .d3dxmeta。
        preview_paths: 预览候选路径，仅采用当前容器 .d3dxmeta/previews 中的补充图片。
    """

    name: str
    src_path: str
    rel_dir: str | None = None
    meta: dict | None = None
    preview_paths: list[str] = field(default_factory=list)


@dataclass
class ExportResult:
    """单项导出结果。"""

    ok: bool
    name: str
    out_path: str = ""
    error: str = ""
    skipped: bool = False


class ModExporter:
    """Mod 导出引擎（纯逻辑）。"""

    def __init__(
        self,
        archive_service: ArchiveService | None = None,
        library_root: str | Path | None = None,
    ) -> None:
        """绑定压缩服务和可选资料库根，初始化并行导出的输出及分类路径预留。"""
        self._archive = archive_service or ArchiveService()
        self._library_root = Path(library_root) if library_root else None
        self._output_lock = threading.Lock()
        self._reserved_outputs: set[Path] = set()
        self._category_dirs: dict[Path, Path] = {}
        self._reserved_categories: set[Path] = set()

    # 目录计算

    def target_dir(self, dest_dir: Path, src_path: Path, preserve_tree: bool) -> Path:
        """计算 Mod 的输出目录。

        preserve_tree 启用时，保留相对于 library_root 的分类路径，
        使不同分类下的同名 Mod 使用独立输出位置。
        """
        if not preserve_tree or not self._library_root:
            return dest_dir
        # 源路径位于容器内时，以容器的父目录作为分类锚点；容器名称不属于分类路径。
        from src.utils.constants import MOD_CONTAINER_MARKER

        anchor_parent = src_path.parent
        if (anchor_parent / MOD_CONTAINER_MARKER).is_file():
            anchor_parent = anchor_parent.parent
        try:
            rel_parent = anchor_parent.relative_to(self._library_root)
            return dest_dir / rel_parent
        except (ValueError, OSError):
            # 源路径不属于资源库时平铺导出。
            return dest_dir

    # 单项导出

    def export_one(
        self,
        item: ExportItem,
        dest_dir: str | Path,
        fmt: str = "zip",
        preserve_tree: bool = True,
        include_meta: bool = False,
        strict_format: bool = False,
        stage_cb=None,
    ) -> ExportResult:
        """以 zip、7z、rar 或 folder 格式导出单个 Mod。

        include_meta 为 True 时附带 .d3dxmeta 元数据和补充预览，不重复复制本体原图。
        文件夹源先写入元数据再压缩；归档源向归档追加元数据成员。

        strict_format 为 False 时直接复制归档并保持原扩展名；为 True 时将不同格式的
        归档解压并重新压缩为目标格式。同格式直接复制，folder 目标始终解压。

        stage_cb 接收单项进度和说明，压缩或解压字节进度映射到对应处理区间。
        """

        def _stage(frac: float, text: str) -> None:
            """将阶段进度限制在零到一之间并通知调用方，忽略回调异常。"""
            if stage_cb is None:
                return
            try:
                stage_cb(max(0.0, min(1.0, frac)), text)
            except Exception:
                pass

        src = Path(item.src_path)
        if not src.exists():
            logger.warning("导出跳过（源不存在）: %s", src)
            return ExportResult(False, item.name, skipped=True, error=tr("源文件不存在"))
        try:
            if src.is_dir():
                src = resolve_body_path(src) or src
            share_previews = supplemental_preview_paths(src, item.preview_paths)
        except (OSError, ValueError) as error:
            return ExportResult(False, item.name, error=str(error))

        # 统一识别 ZIP、7z 和 RAR 本体，避免将归档交给文件夹压缩流程。
        from src.utils.constants import is_resident_archive

        is_archive_src = src.is_file() and is_resident_archive(src)
        src_fmt = src.suffix.lower().lstrip(".") if is_archive_src else ""
        if item.rel_dir is not None:
            # 按显式相对目录保留选中节点以下的分类结构。
            # 逐段规范化名称，移除 Windows 文件名不允许的字符。
            out_dir = Path(dest_dir)
            for part in Path(item.rel_dir).parts:
                out_dir = out_dir / _sanitize_name(part)
        else:
            out_dir = self.target_dir(Path(dest_dir), src, preserve_tree)
        try:
            out_dir = self._prepare_output_dir(Path(dest_dir), out_dir)
        except OSError as e:
            return ExportResult(False, item.name, error=tr("创建输出目录失败: {e}", e=e))

        base = _sanitize_name(item.name or (src.stem if is_archive_src else src.name))
        out_path: Path | None = None
        # 归档解压产生的临时目录由本次导出负责清理。
        tmp_extract: Path | None = None

        # 归档输出格式与文件夹输出分别处理。
        archive_fmts = {"zip", "7z", "rar"}
        try:
            share_variants = tuple(variant.to_dict() for variant in saved_variants(src)) if include_meta else ()
            if fmt in archive_fmts:
                # 同格式归档或不强制转换时复制原文件并保留扩展名；强制转换时先解压再压缩。
                # 文件夹源始终按目标格式压缩。
                if is_archive_src and (not strict_format or src_fmt == fmt):
                    _stage(0.05, tr("复制: {name}", name=item.name))
                    out_path = self._unique(out_dir / f"{base}{src.suffix.lower()}")
                    shutil.copy2(str(src), str(out_path))
                    _stage(0.90, tr("复制: {name}", name=item.name))
                elif is_archive_src:
                    # 格式不同时先解压再按目标格式压缩。RAR 解压使用 unrar，RAR 压缩需要 WinRAR。
                    from src.utils.temp_dirs import mkdtemp

                    tmp_extract = Path(mkdtemp(prefix="d3dx_export_"))
                    # 解压和重压分别占用进度区间 0.05～0.45、0.45～0.90。
                    extracted = self._archive.extract(
                        str(src),
                        str(tmp_extract),
                        progress_cb=lambda f: _stage(0.05 + 0.40 * f, tr("解压: {name}", name=item.name)),
                    )
                    if not extracted:
                        return ExportResult(False, item.name, error=tr("解压失败"))
                    out_path = self._compress_to_unique(
                        extracted,
                        out_dir,
                        base,
                        fmt,
                        progress_cb=lambda f: _stage(
                            0.45 + 0.45 * f, tr("转为 {value}: {name}", value=fmt.upper(), name=item.name)
                        ),
                    )
                    if out_path is None:
                        return ExportResult(False, item.name, error=tr("转换格式失败"))
                else:
                    # 文件夹压缩结果保留顶层目录。RAR 工具不可用时返回 ZIP，实际后缀以结果为准。
                    # 压缩过程占用进度区间 0.05～0.90。
                    out_path = self._compress_to_unique(
                        src,
                        out_dir,
                        base,
                        fmt,
                        progress_cb=lambda f: _stage(
                            0.05 + 0.85 * f, tr("压缩为 {value}: {name}", value=fmt.upper(), name=item.name)
                        ),
                    )
                    if out_path is None:
                        return ExportResult(False, item.name, error=tr("压缩失败"))
            else:
                # 输出为文件夹
                if is_archive_src:
                    # 先解压归档，再规范化顶层目录名称并移至目标位置。
                    from src.utils.temp_dirs import mkdtemp

                    tmp_extract = Path(mkdtemp(prefix="d3dx_export_"))
                    extracted = self._archive.extract(
                        str(src),
                        str(tmp_extract),
                        progress_cb=lambda f: _stage(0.05 + 0.85 * f, tr("解压: {name}", name=item.name)),
                    )
                    if not extracted:
                        return ExportResult(False, item.name, error=tr("解压失败"))
                    out_path = self._unique(out_dir / base)
                    shutil.move(str(extracted), str(out_path))
                else:
                    # 文件夹源 → 文件夹：直接拷贝
                    _stage(0.05, tr("复制: {name}", name=item.name))
                    out_path = self._unique(out_dir / base)
                    shutil.copytree(str(src), str(out_path))
                    _stage(0.90, tr("复制: {name}", name=item.name))

            # 导出前移除本地元数据中的分类路径、启用提示等私有状态。
            # 同时避免追加分享元数据时产生重复归档成员；随后仅写入可分享字段。
            if out_path is not None:
                if not strip_sidecar(out_path):
                    raise OSError(tr("无法移除本地 Mod 身份元数据，已取消分享导出"))

            # 将可分享元数据和补充预览写入 .d3dxmeta；归档源使用成员追加方式。
            # 元数据属于导出结果，写入失败时须清理产物并报告错误。
            if include_meta and (item.meta or share_variants) and out_path is not None:
                op = Path(out_path)
                meta = item.meta or {"name": item.name, "info": {}}
                variant_args = {"variants": share_variants} if share_variants else {}
                if op.is_dir():
                    written = write_envelope_to_folder(op, meta, share_previews, **variant_args)
                elif op.suffix.lower() == ".zip":
                    written = append_envelope_to_zip(op, meta, share_previews, **variant_args)
                elif op.suffix.lower() in (".7z", ".rar"):
                    written = append_envelope_to_archive(op, meta, share_previews, **variant_args)
                else:
                    written = False
                if not written:
                    raise OSError(tr("导出失败：无法保存随 Mod 分享的信息"))

            logger.info("Mod 已导出: %s -> %s", item.name, out_path)
            return ExportResult(True, item.name, out_path=str(out_path))
        except Exception as e:
            logger.error("导出失败 (%s): %s", item.name, e)
            # copytree 的 FileExistsError 表示目标不是本次创建的；不能把
            # 名称检查后才出现的外来目录当成半成品删除。
            if out_path is not None and not isinstance(e, FileExistsError):
                try:
                    if out_path.is_dir():
                        shutil.rmtree(str(out_path), ignore_errors=True)
                    elif out_path.is_file():
                        out_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return ExportResult(False, item.name, error=str(e))
        finally:
            # 清理解压暂存目录；已导出的内容位于最终目标。
            if tmp_extract is not None and tmp_extract.exists():
                shutil.rmtree(str(tmp_extract), ignore_errors=True)

    # 工具

    def _prepare_output_dir(self, root: Path, requested: Path) -> Path:
        """分类目录和导出产物共用命名锁，避免分类路径进入其他产物。

        先创建的目录或产物保留名称，冲突项追加后缀。分类目录的重命名映射
        在后续项目中复用，确保同一分类仍写入同一目录。
        """
        parts = requested.relative_to(root).parts
        with self._output_lock:
            logical = root.resolve()
            if any(path in self._reserved_outputs for path in (logical, *logical.parents)):
                raise OSError(tr("输出根目录已被本批其他导出产物占用"))
            root.mkdir(parents=True, exist_ok=True)
            actual = root
            for part in parts:
                logical = logical / part
                mapped = self._category_dirs.get(logical)
                if mapped is None:
                    mapped = actual / part
                    resolved = mapped.resolve()
                    if (
                        resolved in self._reserved_outputs
                        or resolved in self._reserved_categories
                        or (mapped.exists() and not mapped.is_dir())
                    ):
                        mapped = self._available_path(mapped)
                    mapped.mkdir(exist_ok=True)
                    self._category_dirs[logical] = mapped
                    self._reserved_categories.add(mapped.resolve())
                actual = mapped
            return actual

    def _available_path(self, path: Path) -> Path:
        """调用方持有 _output_lock；目录和产物使用相同的冲突后缀规则。"""
        suffix = path.suffix
        stem = path.stem if suffix else path.name
        candidate = path
        counter = 0
        while (
            candidate.exists()
            or candidate.resolve() in self._reserved_outputs
            or candidate.resolve() in self._reserved_categories
        ):
            counter += 1
            candidate = path.parent / f"{stem}_{counter}{suffix}"
        return candidate

    def _unique(self, path: Path) -> Path:
        """原子预留本实例的输出名；锁只覆盖命名，不串行化复制/压缩。

        并行项可能尚未创建产物，故仅检查 exists 不足以防止同名覆盖。
        预留在导出器存活期间有效，撤回或清理中的路径也不能由其他任务复用。
        """
        with self._output_lock:
            candidate = self._available_path(path)
            self._reserved_outputs.add(candidate.resolve())
            return candidate

    def _compress_to_unique(
        self, source_dir: Path, out_dir: Path, base: str, fmt: str, progress_cb=None
    ) -> Path | None:
        """按指定格式压缩源目录，并为输出名称处理冲突。

        进度回调接收 0～1 的字节进度。先在独立暂存目录压缩，再移动到预留的最终路径，
        避免压缩服务使用固定名称时覆盖已有导出。RAR 不可用时可能返回 ZIP，
        实际扩展名以返回路径为准；返回 None 表示失败。
        """
        from src.utils.temp_dirs import mkdtemp

        stage = Path(mkdtemp(prefix="d3dx_comp_"))
        try:
            produced = self._archive.compress(str(source_dir), str(stage / base), fmt, progress_cb=progress_cb)
            if produced is None:
                return None
            produced = Path(produced)
            dst = self._unique(out_dir / produced.name)
            shutil.move(str(produced), str(dst))
            return dst
        except Exception as e:
            logger.error("压缩到唯一目标失败: %s", e)
            return None
        finally:
            shutil.rmtree(str(stage), ignore_errors=True)
