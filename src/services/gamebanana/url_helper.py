"""GameBanana URL 解析与构造"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from src.services.gamebanana.models import GbModel

_MODEL_BY_PATH = {
    "mods": GbModel.MOD,
    "wips": GbModel.WIP,
    "tools": GbModel.TOOL,
}

_PATH_BY_MODEL = {
    GbModel.MOD: "mods",
    GbModel.WIP: "wips",
    GbModel.TOOL: "tools",
}

_PATH_RE = re.compile(r"^/(mods|wips|tools)/(\d+)/?$")


def parse_gb_url(url: str) -> tuple[GbModel, int] | None:
    """解析受支持的 GameBanana 页面地址并返回资源类型与编号，域名、协议或路径不符时返回空值。"""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.netloc.lower() not in ("gamebanana.com", "www.gamebanana.com"):
        return None
    match = _PATH_RE.match(parsed.path)
    if not match:
        return None
    model = _MODEL_BY_PATH.get(match.group(1))
    if model is None:
        return None
    return (model, int(match.group(2)))


def build_mod_page_url(model: GbModel, mod_id: int) -> str:
    """根据受支持的资源类型和编号构造规范的 GameBanana 页面地址。"""
    path = _PATH_BY_MODEL[model]
    return f"https://gamebanana.com/{path}/{mod_id}"
