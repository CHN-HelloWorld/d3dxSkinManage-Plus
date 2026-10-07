"""检查插件声明的贡献、权限和激活条件，避免运行时静默忽略错误配置。"""

from __future__ import annotations

import re

from src.plugin_sdk.configuration_schema import validate_settings
from src.plugin_sdk.events import ALL_EVENTS
from src.utils.i18n import t as tr

CONTRIBUTION_PERMISSIONS = {
    "toolsPage": "ui:tools-page",
    "menus": "ui:menus",
    "views": "ui:views",
    "settings": "ui:settings",
    "shortcuts": "ui:shortcuts",
}
MENU_LOCATIONS = {"mod-context", "character-context"}
WHEN_VALUES = {"", "always", "modSelected", "characterSelected"}


def require(condition: bool, field: str) -> None:
    """用一致的字段路径报告清单结构错误。"""
    if not condition:
        raise ValueError(tr("插件清单字段无效：{field}", field=field))


def _items(value, field: str, fields: set[str]) -> list[dict]:
    """校验贡献数组、条目对象及允许字段。"""
    require(isinstance(value, list), field)
    for item in value:
        require(isinstance(item, dict) and not set(item) - fields, field)
        for key in ("title", "description", "icon", "group"):
            require(key not in item or isinstance(item[key], str), f"{field}.{key}")
    return value


def validate_contributions(plugin_id: str, contributes: dict, permissions: tuple[str, ...]) -> None:
    """验证命令归属、贡献引用、权限、视图身份和设置声明。"""
    require(not set(contributes) - {"commands", *CONTRIBUTION_PERMISSIONS}, "contributes")
    commands = _items(contributes.get("commands", []), "contributes.commands", {"id", "title", "description", "icon"})
    command_ids = set()
    for item in commands:
        value = item.get("id")
        require(
            isinstance(value, str)
            and value.startswith(plugin_id + ".")
            and bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", value))
            and len(value) > len(plugin_id) + 1
            and value not in command_ids,
            "contributes.commands.id",
        )
        command_ids.add(value)
    for kind, permission in CONTRIBUTION_PERMISSIONS.items():
        if contributes.get(kind):
            require(permission in permissions, f"{kind}: {permission}")
    if contributes.get("settings"):
        require("storage" in permissions, "settings: storage")
    validate_settings(contributes.get("settings", []))
    tools = list(
        _items(contributes.get("toolsPage", []), "contributes.toolsPage", {"command", "title", "icon", "group"})
    )
    menus = contributes.get("menus", {})
    require(isinstance(menus, dict) and not set(menus) - MENU_LOCATIONS, "contributes.menus")
    for location, items in menus.items():
        tools.extend(_items(items, f"contributes.menus.{location}", {"command", "title", "icon", "when"}))
    for item in tools:
        require(isinstance(item.get("command"), str) and item["command"] in command_ids, "contributes.command")
        require(isinstance(item.get("when", ""), str) and item.get("when", "") in WHEN_VALUES, "contributes.menus.when")
    views = _items(contributes.get("views", []), "contributes.views", {"id", "title", "icon"})
    seen = set()
    for item in views:
        value = item.get("id")
        require(
            isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value)) and value not in seen,
            "contributes.views.id",
        )
        seen.add(value)


def activation_events(data: dict, contributes: dict) -> tuple[str, ...]:
    """验证显式激活条件，并从命令和视图声明推导惰性激活条件。"""
    events = data.get("activationEvents", [])
    require(isinstance(events, list) and all(isinstance(event, str) for event in events), "activationEvents")
    inferred = ["onCommand:" + item["id"] for item in contributes.get("commands", [])]
    inferred.extend("onView:" + item["id"] for item in contributes.get("views", []))
    allowed = {"onStartup", *inferred, *("onEvent:" + event for event in ALL_EVENTS)}
    require(all(event in allowed for event in events), "activationEvents")
    return tuple(dict.fromkeys([*events, *inferred]))
