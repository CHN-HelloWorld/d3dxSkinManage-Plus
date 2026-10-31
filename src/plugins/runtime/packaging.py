"""根据插件显式发布清单生成内容和元数据稳定的 ZIP 文件。"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path, PureWindowsPath

from src.plugins.runtime.manifest import Manifest, ManifestError
from src.plugins.runtime.validation import validate_package
from src.utils.i18n import t as tr

_EXCLUDED_DIRECTORIES = {"__pycache__", ".git", ".venv", ".pytest_cache", ".ruff_cache"}


def package_files(manifest: Manifest) -> list[Path]:
    """展开显式文件或目录，仅排除工具缓存，并拒绝链接与文件名冲突。"""
    if not manifest.files:
        raise ManifestError(tr("打包前必须在 plugin.json 的 files 中声明发布文件"))
    root = manifest.directory.resolve()
    selected: dict[str, Path] = {"plugin.json": root / "plugin.json"}
    for relative in manifest.files:
        candidate = root / relative
        paths = candidate.rglob("*") if candidate.is_dir() else [candidate]
        if candidate.is_symlink():
            raise ManifestError(tr("发布文件不能是链接：{path}", path=relative))
        for path in paths:
            name = path.relative_to(root)
            if any(part in _EXCLUDED_DIRECTORIES for part in name.parts) or path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ManifestError(tr("发布文件不能是链接或越界路径：{path}", path=name))
            if not path.is_file():
                continue
            if any(
                PureWindowsPath(part).is_reserved() or part.endswith((" ", ".")) or ":" in part for part in name.parts
            ):
                raise ManifestError(tr("发布文件名不能用于 Windows：{path}", path=name))
            key = name.as_posix().casefold()
            if key in selected and selected[key] != path:
                raise ManifestError(tr("发布文件名冲突：{path}", path=name))
            selected[key] = path
    if manifest.entry.casefold() not in selected:
        raise ManifestError(tr("files 必须包含插件入口：{entry}", entry=manifest.entry))
    return [selected[key] for key in sorted(selected)]


def pack_plugin(directory: Path, output: Path) -> Path:
    """在独立目录验证实际发布内容，成功后原子替换目标 ZIP。"""
    directory, output = directory.resolve(), output.resolve()
    if output.is_relative_to(directory):
        raise ManifestError(tr("压缩包输出不能位于插件源码目录内"))
    manifest = validate_package(directory)
    files = package_files(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".plugin-package-", dir=output.parent) as scratch:
        staging = Path(scratch) / "source"
        staging.mkdir()
        contents = {}
        for path in files:
            name = path.relative_to(directory).as_posix()
            content = path.read_bytes()
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            contents[name] = content
        validate_package(staging)
        archive_path = Path(scratch) / "package.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, content in sorted(contents.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content, compresslevel=9)
        archive_path.replace(output)
    return output
