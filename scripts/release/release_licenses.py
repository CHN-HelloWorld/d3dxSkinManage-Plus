"""根据已安装的运行依赖元数据收集发行许可原文。"""

from __future__ import annotations

import json
import re
import shutil
import sys
from collections import deque
from importlib import metadata
from pathlib import Path, PurePosixPath

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_NOTICE_NAME = re.compile(r"(?:^|[._-])(?:licen[cs]e|copying|notice|copyright)(?:[._-]|$)", re.IGNORECASE)


def _relative_path(value: str) -> Path:
    """规范化许可相对路径并拒绝绝对路径、父目录跳转及盘符，确保文件位于所属目录内。"""
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part == ".." or ":" in part for part in path.parts):
        raise ValueError(f"许可文件路径必须位于所属目录内：{value}")
    return Path(*path.parts)


def _runtime_distributions(project_name: str) -> list[metadata.Distribution]:
    """按当前平台和请求的 extras 遍历依赖，不启用项目的开发或构建 extras。"""
    root = canonicalize_name(project_name)
    pending = deque([(root, frozenset())])
    visited = set()
    distributions = {}
    while pending:
        name, extras = pending.popleft()
        key = (name, extras)
        if key in visited:
            continue
        visited.add(key)
        if name not in distributions:
            try:
                distributions[name] = metadata.distribution(name)
            except metadata.PackageNotFoundError as error:
                raise ValueError(f"运行依赖 {name} 未安装，无法收集许可文件") from error
        distribution = distributions[name]
        for value in distribution.requires or ():
            requirement = Requirement(value)
            if requirement.marker is not None and not any(
                requirement.marker.evaluate({"extra": extra}) for extra in ("", *extras)
            ):
                continue
            pending.append((canonicalize_name(requirement.name), frozenset(requirement.extras)))
    return [distribution for name, distribution in sorted(distributions.items()) if name != root]


def _distribution_notices(distribution: metadata.Distribution) -> list[tuple[Path, Path, str]]:
    """读取声明的许可及 wheel 内嵌声明，保留来源路径和文件内容。"""
    name = distribution.metadata["Name"]
    records = {str(path).replace("\\", "/"): path for path in distribution.files or ()}
    metadata_directory = next(
        (
            PurePosixPath(path).parent
            for path in records
            if len(PurePosixPath(path).parts) == 2
            and PurePosixPath(path).name == "METADATA"
            and PurePosixPath(path).parent.name.endswith(".dist-info")
        ),
        None,
    )
    if metadata_directory is None:
        raise ValueError(f"运行依赖 {name} 缺少已安装文件清单或 METADATA")
    selected = {
        path
        for path in records
        if _NOTICE_NAME.search(PurePosixPath(path).name)
        or any(part.casefold() in ("licenses", "licences") for part in PurePosixPath(path).parts)
    }
    for declared in distribution.metadata.get_all("License-File", ()):
        relative = _relative_path(declared).as_posix()
        candidates = (
            f"{metadata_directory}/licenses/{relative}",
            f"{metadata_directory}/{relative}",
            relative,
        )
        recorded = next((path for path in candidates if path in records), None)
        if recorded is None:
            raise FileNotFoundError(f"运行依赖 {name} 声明的许可文件缺失：{declared}")
        selected.add(recorded)
    if not selected:
        raise ValueError(f"运行依赖 {name} 未提供许可原文，不能仅凭许可名称生成发行声明")
    result = {}
    for recorded in sorted(selected):
        relative = _relative_path(recorded)
        if relative.parts[0] == str(metadata_directory):
            relative = Path(*relative.parts[1:])
            if relative.parts[0].casefold() == "licenses":
                relative = Path(*relative.parts[1:])
        source = Path(distribution.locate_file(records[recorded]))
        if not source.is_file():
            raise FileNotFoundError(f"运行依赖 {name} 的许可文件缺失：{source}")
        target = Path(canonicalize_name(name)) / relative
        key = target.as_posix().casefold()
        if key in result:
            # 部分 wheel 同时在元数据和包目录保存同一许可，仅合并内容相同的副本。
            if result[key][0].read_bytes() != source.read_bytes():
                raise ValueError(f"运行依赖 {name} 的许可文件内容冲突：{target}")
            continue
        result[key] = (source, target, recorded)
    return list(result.values())


def stage_runtime_licenses(product_dir: Path, project_name: str, supplement_root: Path) -> Path:
    """收集运行依赖、Python 运行时及所需的上游补充许可，并生成来源索引。"""
    distributions = _runtime_distributions(project_name)
    copies = []
    packages = []
    for distribution in distributions:
        notices = _distribution_notices(distribution)
        copies.extend((source, target) for source, target, _recorded in notices)
        packages.append(
            {
                "name": distribution.metadata["Name"],
                "version": distribution.version,
                "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License"),
                "files": [{"path": target.as_posix(), "source": recorded} for _source, target, recorded in notices],
            }
        )

    python_license = next(
        (path for name in ("LICENSE.txt", "LICENSE") if (path := Path(sys.base_prefix) / name).is_file()), None
    )
    if python_license is None:
        raise FileNotFoundError(f"Python 运行时许可缺失：{sys.base_prefix}")
    python_target = Path("python") / python_license.name
    copies.append((python_license, python_target))

    supplements = []
    names = {canonicalize_name(package["name"]) for package in packages}
    sources = json.loads((supplement_root / "sources.json").read_text(encoding="utf-8"))
    for component, source_info in sources.items():
        applies_to = sorted(names.intersection(canonicalize_name(name) for name in source_info["distributions"]))
        if not applies_to:
            continue
        if not source_info["files"]:
            raise ValueError(f"组件 {component} 缺少补充许可文件清单")
        files = []
        for filename in source_info["files"]:
            relative = _relative_path(filename)
            source = supplement_root / relative
            if not source.is_file():
                raise FileNotFoundError(f"组件 {component} 的补充许可文件缺失：{source}")
            target = Path("supplements") / relative
            copies.append((source, target))
            files.append(target.as_posix())
        supplements.append({**source_info, "component": component, "distributions": applies_to, "files": files})

    # 先验证全部来源和目标，再清理本次构建的旧许可目录。
    targets = [target.as_posix().casefold() for _source, target in copies]
    if len(targets) != len(set(targets)):
        raise ValueError("不同许可文件使用了相同的发行路径")
    directory = product_dir / "licenses"
    if directory.resolve() != product_dir.resolve() / "licenses":
        raise ValueError(f"许可输出目录不能指向其他位置：{directory}")
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    for source, relative in copies:
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    index = {
        "python": {"version": sys.version.split()[0], "files": [python_target.as_posix()]},
        "packages": packages,
        "supplements": supplements,
    }
    (directory / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return directory
