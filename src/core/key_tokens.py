"""解析 3DMigoto 按键词项并生成显示名称。

模块不依赖界面库；Qt 按键映射由界面层的 key_capture 提供。
"""

from __future__ import annotations

import re

from src.core.migoto_key_codes import key_identity
from src.utils.i18n import N_
from src.utils.i18n import t as tr


def _expression_tokens(expression: str) -> list[str]:
    # 控制器触发阈值属于同一个词项，去掉内部空格以适配按空格分词的 InputButtonList。
    """合并手柄扳机阈值表达式中的空格，再按空白拆分按键标记。"""
    compact = re.sub(
        r"((?:no_)?xb[1-4]?_(?:left|right)_trigger)\s*>\s*(\d+)", r"\1>\2", expression, flags=re.IGNORECASE
    )
    return compact.split()


def compatible_tail_flags(expression: str, tail: str) -> str:
    """保留排除键，除非新组合明确要求该键。

    3DMigoto 对所有词项执行逻辑与；Ctrl+X 与 NO_MODIFIERS 同时存在会使绑定无效，
    因此只在出现冲突时展开排除键。
    """
    pressed = {key_identity(token) for token in _expression_tokens(expression) if not token.lower().startswith("no_")}
    groups = {
        "ctrl": {0x11, 0xA2, 0xA3},
        "alt": {0x12, 0xA4, 0xA5},
        "shift": {0x10, 0xA0, 0xA1},
        "lwin": {0x5B},
        "rwin": {0x5C},
    }
    excluded_groups = {key_identity(name): keys for name, keys in groups.items()}
    result = []
    for flag in _expression_tokens(tail):
        if flag.lower() == "no_modifiers" and any(pressed & keys for keys in groups.values()):
            result.extend(f"no_{name}" for name, keys in groups.items() if not pressed & keys)
        elif not pressed & excluded_groups.get(key_identity(flag[3:]), {key_identity(flag[3:])}):
            result.append(flag)
    return " ".join(result)


# token(小写) → 友好显示名；未覆盖的 token 原样/大写显示
_FRIENDLY = {
    "vk_up": N_("↑ 方向键上"),
    "vk_down": N_("↓ 方向键下"),
    "vk_left": N_("← 方向键左"),
    "vk_right": N_("→ 方向键右"),
    "vk_return": "Enter",
    "vk_space": N_("空格"),
    "vk_escape": "Esc",
    "vk_tab": "Tab",
    "vk_back": "Backspace",
    "vk_delete": "Delete",
    "vk_insert": "Insert",
    "vk_home": "Home",
    "vk_end": "End",
    "vk_prior": "PageUp",
    "vk_next": "PageDown",
    **{f"vk_f{i}": f"F{i}" for i in range(1, 25)},
}


def split_key_expression(rhs: str) -> tuple[str, str]:
    """把 `key=` 右侧（已去掉行尾注释）拆成 (display_expr, tail_flags)。

    - 提取 no_* 排除条件 → tail_flags（写回时保留不冲突的条件）。
    - 其余（含 ctrl/alt 前缀修饰 + 主键）= display_expr，即可展示/可替换的键值。
    """
    tokens = _expression_tokens(rhs)
    if not tokens:
        return "", ""
    tail = [token for token in tokens if token.lower().startswith("no_")]
    keys = [token for token in tokens if not token.lower().startswith("no_")]
    return " ".join(keys), " ".join(tail)


def friendly_name(token: str) -> str:
    """token → 面板里展示的友好名。未知 token 原样返回。"""
    if not token:
        return ""
    if " " in token:
        return " + ".join(friendly_name(part) for part in token.split())
    modifier_names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "vk_lwin": tr("左 Win"), "vk_rwin": tr("右 Win")}
    if token.lower() in modifier_names:
        return modifier_names[token.lower()]
    mapped = _FRIENDLY.get(token.lower())
    if mapped is not None:
        return tr(mapped)
    return token.upper() if len(token) == 1 else token
