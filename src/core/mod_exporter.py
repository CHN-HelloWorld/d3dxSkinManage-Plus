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
