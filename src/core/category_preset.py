"""分类预设的数据对象与内容规则，不依赖界面或文件读写。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.library_schema import directory_key
from src.utils.i18n import t as tr

CATEGORY_PRESET_EXTENSION = ".d3dxcp"
MAGIC = b"D3DXCP\r\n"
HEADER_SIZE = 52
MANIFEST_KIND = "d3dxSkinManage-Plus.category-preset"
MAX_NODES = 5000
MAX_PATH_DEPTH = 64
MAX_DIRECTORY_NAME = 200
MAX_WINDOWS_COMPONENT_UNITS = 255
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_IMAGES = 5000
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_FILE_BYTES = 1024 * 1024 * 1024
MAX_JSON_DEPTH = 16
MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/bmp", "image/gif", "image/webp"})
_IMAGE_ID = re.compile(r"[0-9a-f]{64}\Z")
_DEVICE_NAME = re.compile(r"(?:con|prn|aux|nul|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³])\Z", re.IGNORECASE)
_INVALID_DIRECTORY_CHARACTERS = frozenset('<>:"\\|?*')


class CategoryPresetError(ValueError):
    """预设内容无效，必须拒绝整份预设。"""


class CategoryPresetFormatError(CategoryPresetError):
    """所选文件的头部、清单或内嵌图片无效。"""


class CategoryPresetFileAccessError(CategoryPresetError):
    """无法从磁盘打开或读取所选文件。"""


class CategoryPresetCancelled(Exception):
    """调用方在发布前取消操作。"""


@dataclass(frozen=True)
class CategoryPresetNode:
    """保存分类预设节点的相对路径、显示名称、类型及图片引用。"""

    path: str
    name: str
    node_type: str
    name_en: str = ""
    sort_order: int = 0
    image_id: str = ""


@dataclass(frozen=True)
class CategoryPresetImage:
    """描述内嵌图片在图片区中的偏移、字节长度和媒体类型。"""

    image_id: str
    offset: int
    length: int
    media_type: str


@dataclass(frozen=True)
class CategoryPresetDocument:
    """保存已验证的分类清单、图片索引及源文件校验信息。"""

    name: str
    nodes: tuple[CategoryPresetNode, ...]
    suggested_game_name: str = ""
    suggested_game_name_en: str = ""
    images: tuple[CategoryPresetImage, ...] = ()
    source_path: Path | None = None
    manifest_length: int = 0
    images_length: int = 0
    payload_sha256: str = ""


def _check_json_values(data: Any) -> None:
    """未知字段同样遵守 JSON 资源限制和数值有效性规则。"""
    # 每层嵌套只保留一个迭代器，避免将大型未知列表复制到第二个工作队列。
    pending = [iter((data,))]
    while pending:
        try:
            value = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        if isinstance(value, (dict, list)):
            if len(pending) > MAX_JSON_DEPTH:
                raise CategoryPresetError(tr("清单 JSON 嵌套超过 16 层"))
            if isinstance(value, dict):
                if any(not isinstance(key, str) for key in value):
                    raise CategoryPresetError(tr("清单字段名必须是字符串"))
                pending.append(iter(value.values()))
            else:
                pending.append(iter(value))
        elif isinstance(value, float) and not math.isfinite(value):
            raise CategoryPresetError(tr("清单不能包含非有限数字"))
        elif not isinstance(value, (str, int, float, bool, type(None))):
            raise CategoryPresetError(tr("清单包含无效 JSON 数据"))


def _string(data: dict, key: str, default: str | None = None, *, nonempty: bool = False) -> str:
    """读取字符串字段并检查非空要求；类型或 Unicode 代理字符无效时拒绝清单。"""
    value = data.get(key, default)
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise CategoryPresetError(tr("字段 {key} 必须是{value}字符串", key=key, value=tr("非空") if nonempty else ""))
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise CategoryPresetError(tr("字段 {key} 包含无效 Unicode 字符", key=key))
    return value


def _integer(data: dict, key: str, default: int | None = None) -> int:
    """读取整数字段并排除布尔值；字段类型不符时抛出预设内容错误。"""
    value = data.get(key, default)
    if type(value) is not int:
        raise CategoryPresetError(tr("字段 {key} 必须是整数", key=key))
    return value


def _validate_path(path: str) -> None:
    """校验分类相对路径的层数、目录长度及 Windows 保留名称和字符。"""
    parts = path.split("/")
    if len(parts) > MAX_PATH_DEPTH:
        raise CategoryPresetError(tr("分类路径超过 64 层：{path}", path=path))
    for part in parts:
        if (
            not part
            or len(part) > MAX_DIRECTORY_NAME
            or len(part.encode("utf-16-le")) // 2 > MAX_WINDOWS_COMPONENT_UNITS
            or part.startswith(".")
            or part.endswith((".", " "))
            or _DEVICE_NAME.fullmatch(part.partition(".")[0].rstrip(" "))
            or any(ord(char) < 32 or char in _INVALID_DIRECTORY_CHARACTERS for char in part)
        ):
            raise CategoryPresetError(tr("分类路径包含无效目录名：{path}", path=path))


def validate_manifest(data, *, fallback_name, images_length) -> CategoryPresetDocument:
    """读取已知字段并应用默认值；内容无效时拒绝整份清单。"""
    if not isinstance(data, dict):
        raise CategoryPresetError(tr("清单顶层必须是对象"))
    _check_json_values(data)
    if type(images_length) is not int or not 0 <= images_length <= MAX_FILE_BYTES - HEADER_SIZE:
        raise CategoryPresetError(tr("图片区长度无效或超过文件上限"))
    if _string(data, "kind") != MANIFEST_KIND:
        raise CategoryPresetError(tr("清单 kind 不是分类预设"))
    if _string(data, "scope", "game") != "game":
        raise CategoryPresetError(tr("分类预设只支持完整游戏范围"))
    name = _string(data, "name", fallback_name, nonempty=True)
    suggested = data.get("suggested_game", {})
    if not isinstance(suggested, dict):
        raise CategoryPresetError(tr("字段 suggested_game 必须是对象"))
    game_name = _string(suggested, "name", "")
    game_name_en = _string(suggested, "name_en", "")
    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, list) or not 1 <= len(raw_nodes) <= MAX_NODES:
        raise CategoryPresetError(tr("分类节点列表必须包含 1 至 5,000 个节点"))
    nodes = []
    paths = set()
    sibling_counts: dict[str, int] = {}
    referenced = set()
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            raise CategoryPresetError(tr("分类节点必须是对象"))
        path = _string(raw, "path", nonempty=True)
        _validate_path(path)
        key = directory_key(path)
        if key in paths:
            raise CategoryPresetError(tr("分类路径重复（不区分大小写）：{path}", path=path))
        paths.add(key)
        parent = path.rpartition("/")[0]
        parent_key = directory_key(parent)
        ordinal = sibling_counts.get(parent_key, 0)
        sibling_counts[parent_key] = ordinal + 1
        node_type = _string(raw, "node_type")
        if node_type not in ("category", "character"):
            raise CategoryPresetError(tr("分类节点类型无效：{path}", path=path))
        image_id = _string(raw, "image_id", "")
        if image_id:
            if not _IMAGE_ID.fullmatch(image_id):
                raise CategoryPresetError(tr("图片 ID 无效：{path}", path=path))
            referenced.add(image_id)
        nodes.append(
            CategoryPresetNode(
                path=path,
                name=_string(raw, "name", path.rsplit("/", 1)[-1], nonempty=True),
                node_type=node_type,
                name_en=_string(raw, "name_en", ""),
                sort_order=_integer(raw, "sort_order", ordinal),
                image_id=image_id,
            )
        )
    for node in nodes:
        parent = node.path.rpartition("/")[0]
        if parent and directory_key(parent) not in paths:
            raise CategoryPresetError(tr("分类父路径缺失：{path}", path=node.path))
    raw_images = data.get("images", [])
    if not isinstance(raw_images, list) or len(raw_images) > MAX_IMAGES:
        raise CategoryPresetError(tr("图片索引必须是列表且不能超过 5,000 张"))
    images = []
    image_ids = set()
    end = 0
    for raw in raw_images:
        if not isinstance(raw, dict):
            raise CategoryPresetError(tr("图片索引项必须是对象"))
        image_id = _string(raw, "id")
        if not _IMAGE_ID.fullmatch(image_id) or image_id in image_ids:
            raise CategoryPresetError(tr("图片 ID 无效或重复"))
        offset = _integer(raw, "offset")
        length = _integer(raw, "length")
        media_type = _string(raw, "media_type")
        if media_type not in MEDIA_TYPES:
            raise CategoryPresetError(tr("不支持的图片类型：{media_type}", media_type=media_type))
        if offset != end or not 1 <= length <= MAX_IMAGE_BYTES:
            raise CategoryPresetError(tr("图片索引区间不连续或单张图片超过 20 MiB"))
        end = offset + length
        if end > images_length:
            raise CategoryPresetError(tr("图片索引超出图片区"))
        image_ids.add(image_id)
        images.append(CategoryPresetImage(image_id, offset, length, media_type))
    if end != images_length:
        raise CategoryPresetError(tr("图片区存在未索引字节或图片索引缺失"))
    if image_ids != referenced:
        raise CategoryPresetError(tr("图片引用缺失或存在未引用的图片"))
    return CategoryPresetDocument(
        name,
        tuple(nodes),
        game_name,
        game_name_en,
        tuple(images),
        images_length=images_length,
    )
