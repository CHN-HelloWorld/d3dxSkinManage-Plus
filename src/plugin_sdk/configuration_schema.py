"""声明式插件设置的类型、默认值及取值约束，不依赖界面实现。"""

from __future__ import annotations

import math
import re
from typing import Any

from src.utils.i18n import t as tr

_TYPES = {"bool": bool, "int": int, "float": (int, float), "str": str}
_FIELDS = {
    "key",
    "type",
    "default",
    "title",
    "description",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "enum",
    "enumTitles",
}


def validate_setting_value(definition: dict, value: Any) -> None:
    """检查设置值的类型和约束；布尔值不作为整数或小数接受。"""
    kind = definition["type"]
    valid = isinstance(value, _TYPES[kind])
    if kind in {"int", "float"}:
        valid = valid and not isinstance(value, bool) and (not isinstance(value, float) or math.isfinite(value))
    if not valid:
        raise ValueError(tr("设置 {key} 的值类型应为 {type}", key=definition["key"], type=kind))
    if "enum" in definition and value not in definition["enum"]:
        raise ValueError(tr("设置 {key} 的值不在允许列表中", key=definition["key"]))
    for lower, upper, actual in [
        ("minimum", "maximum", value),
        ("minLength", "maxLength", len(value) if kind == "str" else 0),
    ]:
        if lower in definition and actual < definition[lower] or upper in definition and actual > definition[upper]:
            raise ValueError(tr("设置 {key} 的值超出允许范围", key=definition["key"]))


def validate_settings(definitions: list[dict]) -> None:
    """验证设置声明、默认值、枚举文案及数值范围，拒绝重复键和未知字段。"""
    if not isinstance(definitions, list):
        raise ValueError(tr("contributes.settings 必须是数组"))
    seen = set()
    for definition in definitions:
        if not isinstance(definition, dict) or set(definition) - _FIELDS:
            raise ValueError(tr("设置声明包含未知字段或不是对象"))
        key, kind = definition.get("key"), definition.get("type")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", key) or key in seen:
            raise ValueError(tr("设置键无效或重复：{key}", key=key))
        if not isinstance(kind, str) or kind not in _TYPES or "default" not in definition:
            raise ValueError(tr("设置 {key} 必须声明有效类型和默认值", key=key))
        seen.add(key)
        for field in ("title", "description"):
            if field in definition and not isinstance(definition[field], str):
                raise ValueError(tr("设置 {key} 的 {field} 必须是字符串", key=key, field=field))
        for low, high, kinds in (("minimum", "maximum", {"int", "float"}), ("minLength", "maxLength", {"str"})):
            for field in (low, high):
                if field not in definition:
                    continue
                bound = definition[field]
                numeric = type(bound) in (int, float) and (not isinstance(bound, float) or math.isfinite(bound))
                if kind not in kinds or not numeric or ((kind == "int" or kind == "str") and type(bound) is not int):
                    raise ValueError(tr("设置 {key} 的 {field} 约束无效", key=key, field=field))
                if kind == "str" and bound < 0:
                    raise ValueError(tr("设置 {key} 的长度不能为负数", key=key))
            if low in definition and high in definition and definition[low] > definition[high]:
                raise ValueError(tr("设置 {key} 的最小值大于最大值", key=key))
        if "enum" in definition:
            values = definition["enum"]
            if not isinstance(values, list) or not values:
                raise ValueError(tr("设置 {key} 的枚举不能为空", key=key))
            without_enum = {field: value for field, value in definition.items() if field != "enum"}
            for index, value in enumerate(values):
                validate_setting_value(without_enum, value)
                if value in values[:index]:
                    raise ValueError(tr("设置 {key} 的枚举值重复", key=key))
        if "enumTitles" in definition:
            titles = definition["enumTitles"]
            if (
                not isinstance(titles, list)
                or not all(isinstance(title, str) for title in titles)
                or len(titles) != len(definition.get("enum", []))
                or "enum" not in definition
            ):
                raise ValueError(tr("设置 {key} 的枚举标题必须与枚举逐项对应", key=key))
        validate_setting_value(definition, definition["default"])
