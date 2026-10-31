"""plugin.json 解析与校验（与代码分离的声明式 manifest）。"""

from __future__ import annotations

import json
import keyword
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from src.plugins.runtime.manifest_validation import activation_events, validate_contributions
from src.plugins.runtime.versions import Version, constraint_bounds
from src.utils.i18n import t as tr

# 清单只接受宿主明确支持的权限。
KNOWN_PERMISSIONS = frozenset(
    {
        "mods:read",
        "mods:write",
        "mods:manage",
        "storage",
        "ui:tools-page",
        "ui:menus",
        "ui:settings",
        "ui:views",
        "ui:shortcuts",
        "network",
        "fs:read",
        "fs:write",
        "background",
        "process",
        "native",
    }
)

_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{1,63}$")


class ManifestError(Exception):
    """plugin.json 缺失/格式非法/缺必填字段。"""


@dataclass(frozen=True)
class Manifest:
    """经解析的插件清单，包含身份、入口、权限及界面扩展声明。"""

    id: str
    name: str
    version: str
    api_version: str
    entry: str
    directory: Path
    min_app_version: str = "0.0.0"
    author: str = "Unknown"
    description: str = ""
    homepage: str = ""
    permissions: tuple[str, ...] = field(default_factory=tuple)
    contributes: dict[str, Any] = field(default_factory=dict)
    activation_events: tuple[str, ...] = ()
    dependencies: dict[str, str] = field(default_factory=dict)
    files: tuple[str, ...] = ()

    @property
    def major_api(self) -> int:
        """提取插件 API 主版本，无法解析时返回 -1。"""
        try:
            return int(self.api_version.split(".")[0])
        except (ValueError, IndexError):
            return -1


def _require(data: dict, key: str, path: Path) -> Any:
    """读取清单必填字段，缺失或为空时抛出包含路径的 ManifestError。"""
    if key not in data or data[key] in (None, ""):
        raise ManifestError(tr("{path}: 缺少必填字段 '{key}'", path=path, key=key))
    return data[key]


def validate_plugin_id(value: str) -> str:
    """校验插件 ID 字符集合及长度，非法值抛出 ManifestError。"""
    if (
        not isinstance(value, str)
        or not _ID_RE.fullmatch(value)
        or value.endswith(".")
        or value.split(".")[0].upper()
        in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    ):
        raise ManifestError(tr("非法 id '{value}'（仅限字母数字 . _ -，2~64 位）", value=value))
    return value


def _validate_shortcuts(contributes, permissions, path):
    """校验快捷键权限、命令归属和上下文，并原地规范化默认键位。"""
    shortcuts = contributes.get("shortcuts", [])
    if not isinstance(shortcuts, list):
        raise ManifestError(tr("{path}: contributes.shortcuts 必须是数组", path=path))
    if not shortcuts:
        return
    if "ui:shortcuts" not in permissions:
        raise ManifestError(tr("{path}: 快捷键贡献需要 ui:shortcuts 权限", path=path))
    declarations = contributes.get("commands", [])
    if not isinstance(declarations, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in declarations
    ):
        raise ManifestError(tr("{path}: shortcuts 必须引用 commands 中声明的命令", path=path))
    command_ids = {item["id"] for item in declarations}
    from src.core.shortcuts import ShortcutCommand, normalize_binding, validate_binding

    seen = set()
    for item in shortcuts:
        if not isinstance(item, dict) or set(item) - {"command", "default", "context"}:
            raise ManifestError(tr("{path}: 非法快捷键声明", path=path))
        command = item.get("command")
        context = item.get("context", "window")
        if not isinstance(command, str) or command not in command_ids or command in seen:
            raise ManifestError(tr("{path}: 快捷键必须唯一引用本插件声明的命令", path=path))
        if context not in ("window", "overview", "preset"):
            raise ManifestError(tr("{path}: 快捷键 context 仅支持 window/overview/preset", path=path))
        definition = ShortcutCommand(command, "", "", "", item.get("default"), frozenset({context}))
        error = validate_binding(item.get("default"), definition)
        if error:
            raise ManifestError(f"{path}: {error}")
        item["default"] = normalize_binding(item["default"])
        seen.add(command)


def parse_manifest(plugin_dir: Path) -> Manifest:
    """读取并校验 `<plugin_dir>/plugin.json`，返回 Manifest。失败抛 ManifestError。"""
    mf_path = plugin_dir / "plugin.json"
    if not mf_path.is_file():
        raise ManifestError(tr("{path}: 缺少 plugin.json", path=plugin_dir))

    try:
        data = json.loads(mf_path.read_text("utf-8"))
    except Exception as e:
        raise ManifestError(tr("{path}: JSON 解析失败 - {error}", path=mf_path, error=e)) from e
    if not isinstance(data, dict):
        raise ManifestError(tr("{path}: 顶层必须是 JSON 对象", path=mf_path))

    fields = {
        "$schema",
        "id",
        "name",
        "version",
        "apiVersion",
        "entry",
        "minAppVersion",
        "author",
        "description",
        "homepage",
        "permissions",
        "contributes",
        "activationEvents",
        "dependencies",
        "files",
    }
    if set(data) - fields:
        raise ManifestError(
            tr("{path}: 未知清单字段 {fields}", path=mf_path, fields=", ".join(sorted(set(data) - fields)))
        )
    for key in ("id", "name", "version", "apiVersion", "entry"):
        value = _require(data, key, mf_path)
        if not isinstance(value, str) or not value.strip():
            raise ManifestError(tr("{path}: {key} 必须是非空字符串", path=mf_path, key=key))
    for key in ("author", "description", "homepage", "minAppVersion", "$schema"):
        if key in data and not isinstance(data[key], str):
            raise ManifestError(tr("{path}: {key} 必须是字符串", path=mf_path, key=key))
    pid = data["id"]
    validate_plugin_id(pid)

    entry = data["entry"].replace("\\", "/")
    relative = PurePosixPath(entry.replace("\\", "/"))
    if relative.is_absolute() or PureWindowsPath(entry).drive or ".." in relative.parts or relative.suffix != ".py":
        raise ManifestError(tr("{path}: entry 必须是插件目录内的 .py 相对路径", path=mf_path))
    if not (plugin_dir / entry).is_file():
        raise ManifestError(tr("{path}: entry 文件不存在: {entry}", path=mf_path, entry=entry))
    elif not (plugin_dir / entry).resolve().is_relative_to(plugin_dir.resolve()):
        raise ManifestError(tr("{path}: entry 越出插件目录", path=mf_path))
    if any(not part.isidentifier() or keyword.iskeyword(part) for part in relative.with_suffix("").parts):
        raise ManifestError(tr("{path}: entry 的目录和模块名必须是 Python 标识符", path=mf_path))

    raw_perms = data.get("permissions", [])
    if not isinstance(raw_perms, list) or not all(isinstance(p, str) for p in raw_perms):
        raise ManifestError(tr("{path}: permissions 必须是字符串数组", path=mf_path))
    perms = tuple(dict.fromkeys(raw_perms))
    if set(perms) - KNOWN_PERMISSIONS:
        raise ManifestError(
            tr(
                "{path}: 未知权限 {permissions}",
                path=mf_path,
                permissions=", ".join(sorted(set(perms) - KNOWN_PERMISSIONS)),
            )
        )
    contributes = data.get("contributes", {})
    if not isinstance(contributes, dict):
        raise ManifestError(tr("{path}: contributes 必须是对象", path=mf_path))
    dependencies = data.get("dependencies", {})
    if not isinstance(dependencies, dict):
        raise ManifestError(tr("{path}: dependencies 必须是对象", path=mf_path))
    try:
        Version.parse(data["version"])
        for dependency, constraint in dependencies.items():
            validate_plugin_id(dependency)
            if dependency == pid:
                raise ValueError(tr("插件不能依赖自身"))
            constraint_bounds(constraint)
        validate_contributions(pid, contributes, perms)
        events = activation_events(data, contributes)
        _validate_shortcuts(contributes, perms, mf_path)
    except (ValueError, TypeError) as exc:
        raise ManifestError(tr("{path}: 清单声明无效: {error}", path=mf_path, error=exc)) from exc
    files = data.get("files", [])
    if not isinstance(files, list) or not all(isinstance(item, str) and item for item in files):
        raise ManifestError(tr("{path}: files 必须是非空相对路径数组", path=mf_path))
    for item in files:
        part = PurePosixPath(item.replace("\\", "/"))
        resolved = (plugin_dir / item).resolve()
        if (
            part.is_absolute()
            or PureWindowsPath(item).drive
            or ".." in part.parts
            or ":" in item
            or not resolved.is_relative_to(plugin_dir.resolve())
            or not resolved.exists()
        ):
            raise ManifestError(tr("{path}: files 包含不存在或越界路径: {entry}", path=mf_path, entry=item))
    minimum = str(data.get("minAppVersion", "0.0.0"))
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", minimum):
        raise ManifestError(tr("{path}: minAppVersion 必须是点分数字版本", path=mf_path))
    api_version = str(_require(data, "apiVersion", mf_path))
    if not re.fullmatch(r"\d+\.\d+", api_version):
        raise ManifestError(tr("{path}: apiVersion 必须为 MAJOR.MINOR 数字版本", path=mf_path))

    return Manifest(
        id=pid,
        name=str(_require(data, "name", mf_path)),
        version=str(_require(data, "version", mf_path)),
        api_version=api_version,
        entry=entry,
        min_app_version=minimum,
        author=str(data.get("author", "Unknown")),
        description=str(data.get("description", "")),
        homepage=str(data.get("homepage", "")),
        permissions=perms,
        contributes=contributes,
        directory=plugin_dir,
        activation_events=events,
        dependencies=dict(dependencies),
        files=tuple(dict.fromkeys(item.replace("\\", "/") for item in files)),
    )
