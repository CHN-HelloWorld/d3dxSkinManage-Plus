"""统一游戏运行路径的便携存储、旧位置识别和已验证目录移动后的重定位。"""

from __future__ import annotations

import os
import re
from pathlib import Path

from src.core.ini_loading import loader_include_entries
from src.core.migoto_user_ini import decode_ini_bytes
from src.utils import fs
from src.utils.constants import MIGOTO_LOADER_NAME
from src.utils.paths import get_app_dir, resolve_stored_path, to_stored_path

GAME_PATH_FIELDS = ("exe_path", "migoto_path", "mods_path", "preview")
_EXECUTABLE_END = re.compile(r"\.(?:exe|com|bat|cmd)(?=\s|$)", re.IGNORECASE)


def _path_parts(field: str, value: str) -> tuple[str, str, bool] | None:
    """分开程序路径与原始参数；目录字段不解释空格，无效引号保持原值。"""
    if field != "exe_path":
        return value, "", False
    if value.startswith('"'):
        end = value.find('"', 1)
        if end < 0 or (end + 1 < len(value) and not value[end + 1].isspace()):
            return None
        return value[1:end], value[end + 1 :], True
    matches = list(_EXECUTABLE_END.finditer(value))
    if not matches:
        return value, "", False
    selected = matches[0]
    if len(matches) > 1:
        # 目录名也可包含 .exe；有歧义时优先使用实际存在的最长程序路径。
        selected = next(
            (match for match in reversed(matches) if fs.is_file(resolve_stored_path(value[: match.end()]))),
            selected,
        )
    return value[: selected.end()], value[selected.end() :], False


def _join_path(path: str, suffix: str, quoted: bool) -> str:
    """替换程序位置并逐字保留参数及原有路径引号。"""
    return (f'"{path}"' if quoted else path) + suffix


def _convert_paths(game: dict, *, stored: bool) -> dict:
    """复制条目并只转换已存在的字符串路径，保留未知字段及外部绝对路径的写法。"""
    result = dict(game)
    for field in GAME_PATH_FIELDS:
        value = game.get(field)
        if not isinstance(value, str) or not value:
            continue
        parts = _path_parts(field, value)
        if parts is None:
            continue
        path, suffix, quoted = parts
        absolute = Path(fs.normal_path(path)).is_absolute()
        if stored:
            converted = to_stored_path(path)
            if absolute and Path(converted).is_absolute():
                continue
        else:
            if absolute:
                continue
            converted = resolve_stored_path(path)
        result[field] = _join_path(converted, suffix, quoted)
    return result


def store_game_paths(game: dict) -> dict:
    """应用内路径相对化，外部位置仍绝对保存，游戏启动参数不参与路径转换。"""
    return _convert_paths(game, stored=True)


def resolve_game_paths(game: dict) -> dict:
    """把便携路径解析到当前应用根，解析结果与启动工作目录无关。"""
    return _convert_paths(game, stored=False)


def _loader_exists(root: Path) -> bool:
    """以加载器配置或独立加载器程序验证候选目录，不以目录名称判定游戏。"""
    return fs.is_file(root / "d3dx.ini") or fs.is_file(root / MIGOTO_LOADER_NAME)


def _loads_deployment(root: Path, deployment: Path) -> bool:
    """检查加载器的递归包含目录是否覆盖当前显式部署目录。"""
    try:
        text = decode_ini_bytes(fs.read_bytes(root / "d3dx.ini"))[0]
    except (OSError, ValueError, UnicodeError):
        return False
    for key, value in loader_include_entries(text):
        if key != "include_recursive":
            continue
        value = value.strip().strip('"')
        if value and deployment.is_relative_to(Path(os.path.normpath(root / fs.normal_path(value)))):
            return True
    return False


def resolve_deployment_migoto_root(deployment: str | Path | None) -> Path | None:
    """从当前部署位置核对最近的加载器配置，不跨过独立环境查找其他加载器。"""
    if not deployment:
        return None
    target = Path(os.path.abspath(fs.normal_path(deployment)))
    if not fs.is_dir(target):
        return None
    for root in (target, *target.parents):
        if fs.is_file(root / "d3dx.ini"):
            return root if _loads_deployment(root, target) else None
    return None


def relocate_game_paths(game: dict, old: Path, new: Path, *, existing_only: bool = False) -> dict:
    """按已确定的根映射重定位相关字段，旧配置发现阶段只接纳实际存在的目标。"""
    result = dict(game)
    for field in GAME_PATH_FIELDS:
        value = game.get(field)
        if not isinstance(value, str) or not value:
            continue
        parts = _path_parts(field, value)
        if parts is None:
            continue
        path, suffix, quoted = parts
        try:
            relative = Path(fs.normal_path(path)).relative_to(old)
        except ValueError:
            continue
        candidate = new / relative
        if existing_only and not (fs.is_file(candidate) if field in {"exe_path", "preview"} else fs.is_dir(candidate)):
            continue
        result[field] = _join_path(str(candidate), suffix, quoted)
    return result


def recover_game_paths(game: dict) -> dict:
    """旧加载器失效时按当前部署包含关系或唯一的应用内布局恢复，候选有歧义时保留配置。"""
    configured = game.get("migoto_path", "")
    if not configured or fs.is_dir(configured):
        return game
    previous = Path(fs.normal_path(configured))
    if not previous.is_absolute():
        return game
    deployment = Path(fs.normal_path(game.get("mods_path", ""))) if game.get("mods_path") else None
    if deployment is not None and fs.is_dir(deployment):
        loaders = [parent for parent in deployment.parents if _loads_deployment(parent, deployment)]
        if len(loaders) == 1:
            return {
                **relocate_game_paths(game, previous, loaders[0], existing_only=True),
                "migoto_path": str(loaders[0]),
            }
    root = get_app_dir()
    # 已锚定当前应用根的位置不是旧设备地址，不能删掉中间目录去匹配另一套加载器。
    if previous.is_relative_to(root):
        return game
    candidates = {}
    for start in range(1, len(previous.parts)):
        candidate = root.joinpath(*previous.parts[start:])
        if not _loader_exists(candidate):
            continue
        if deployment is not None and deployment.is_relative_to(previous):
            if not fs.is_dir(candidate / deployment.relative_to(previous)):
                continue
        candidates[os.path.normcase(str(candidate))] = (Path(*previous.parts[:start]), root)
    if len(candidates) != 1:
        return game
    old, new = next(iter(candidates.values()))
    return relocate_game_paths(game, old, new, existing_only=True)


def rebase_game_paths(game: dict, old_path: str, new_path: str) -> dict:
    """部署根已由文件身份确认移动后，同步重定位同一移动父目录中的运行路径。"""
    old, new = Path(fs.normal_path(old_path)), Path(fs.normal_path(new_path))
    if old == new:
        return {**game, "mods_path": new_path}
    common = 0
    for left, right in zip(reversed(old.parts), reversed(new.parts)):
        if os.path.normcase(left) != os.path.normcase(right):
            break
        common += 1
    old_base = old.parents[common - 1] if common else old
    new_base = new.parents[common - 1] if common else new
    # 换盘符不能被解释成整个盘符都移动；最多扩大到已配置加载器与部署目录的公共根。
    if old_base == old_base.parent or new_base == new_base.parent:
        loader = Path(fs.normal_path(game.get("migoto_path", ""))) if game.get("migoto_path") else old
        if loader.is_absolute() and old.is_relative_to(loader):
            depth = len(old.relative_to(loader).parts)
            old_base = loader
            new_base = new.parents[depth - 1] if depth else new
        else:
            old_base, new_base = old, new
    return {**relocate_game_paths(game, old_base, new_base), "mods_path": new_path}
