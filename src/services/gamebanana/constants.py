"""GameBanana 接入常量与用户配置的游戏名称解析。"""

from __future__ import annotations

DEFAULT_PAGE_SIZE: int = 30
DEFAULT_USER_AGENT_TEMPLATE: str = "d3dxSkinManagePlus-GB/{ver}"


def gb_override_key(name_en: str, local_game_id: str = "") -> str:
    """在线匹配结果在 ``game_id_overrides`` 里的缓存键。

    有英文名时使用不区分大小写的英文名，否则使用本地游戏标识。
    修改英文名后重新匹配，避免复用其他游戏的缓存。
    """
    key = (name_en or "").strip().casefold()
    return key if key else (local_game_id or "")


def resolve_gb_game_id_by_name(
    name_en: str,
    local_game_id: str = "",
    overrides: dict[str, int] | None = None,
) -> int | None:
    """优先按官方英文名和同名缓存解析，未填写英文名时使用本地标识。

    已填写英文名但未匹配时返回 None，由调用方进行在线查询。
    """
    key = gb_override_key(name_en, local_game_id)
    return overrides.get(key) if key and overrides else None


def pick_gb_game_match(records: list[dict], name: str) -> int | None:
    """从在线候选中选择明确匹配的 GameBanana 游戏编号。

    忽略大小写和首尾空格后，优先选择名称完全相同的候选。
    没有精确匹配时仅接受唯一候选，多个候选则返回 None。

    `name` 应传游戏英文名（GameBanana 的 `_sName` 是英文官方名）。
    """
    if not records:
        return None
    target = (name or "").strip().casefold()

    def _id_of(rec: dict) -> int | None:
        """将在线游戏记录的标识转换为整数，缺失或格式无效时返回空值。"""
        try:
            return int(rec.get("_idRow"))
        except (TypeError, ValueError):
            return None

    if target:
        for rec in records:
            rname = str(rec.get("_sName", "")).strip().casefold()
            if rname and rname == target:
                gid = _id_of(rec)
                if gid is not None:
                    return gid
    if len(records) == 1:
        return _id_of(records[0])
    return None


def official_name_from_records(records: list[dict], gb_game_id: int) -> str:
    """从在线候选中读取指定游戏编号的英文名称，未找到时返回空字符串。"""
    for rec in records or []:
        try:
            if int(rec.get("_idRow")) == gb_game_id:
                return str(rec.get("_sName", "")).strip()
        except (TypeError, ValueError):
            continue
    return ""
