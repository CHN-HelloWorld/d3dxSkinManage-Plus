"""统一提供颜色、圆角、间距及字号。

QSS 模板通过占位符注入主题值，Python 内联样式通过取值函数读取。
明暗主题共用选择器和几何规则，仅切换调色板。
"""

from __future__ import annotations

import re

from src.utils.logger import logger
from src.utils.paths import get_resources_dir

# 几何 token（明暗共用）


RADII = {"card": 8, "control": 4, "small": 4, "large": 12, "pill": 999}
SPACING = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}
FONT = {
    "size": 13,
    "size_small": 12,
    "size_title": 18,
    "size_subtitle": 15,
    "size_body": 14,
    "size_secondary": 13,
    "size_caption": 12,
    "size_card_title": 14,
    "size_card_title_compact": 13,
}

# 卡片左上角类型角标的语义底色（白字 + 图标），用半透明确保叠在缩略图上也可读。
# 三类各用一种明确区分的色相：类别=蓝、角色/对象=紫、Mod=橙。
CARD_BADGE = {
    "category": "rgba(0, 120, 212, 0.92)",
    "character": "rgba(139, 92, 246, 0.92)",
    "mod": "rgba(202, 80, 16, 0.92)",
}


def card_badge_bg(kind: str) -> str:
    """返回卡片类型角标底色（``category`` / ``character`` / ``mod``）。"""
    return CARD_BADGE.get(kind, CARD_BADGE["category"])


# 基础调色板（accent 家族在运行期按系统强调色推导后并入）


_BASE: dict[str, dict[str, str]] = {
    "light": {
        "window": "#f3f3f3",
        "bg.base": "#f3f3f3",
        "bg.layer": "#fbfbfb",
        "bg.card": "#ffffff",
        "bg.subtle": "#f5f5f5",
        "bg.hover": "#ededed",
        "bg.pressed": "#e0e0e0",
        "stroke.card": "#e5e5e5",
        "stroke.control": "#d6d6d6",
        "stroke.divider": "#ebebeb",
        "text.primary": "#1a1a1a",
        "text.secondary": "#5f5f5f",
        "text.tertiary": "#8a8a8a",
        "text.disabled": "#a6a6a6",
        "text.link": "#0067c0",
        "success": "#0f7b0f",
        "danger": "#c42b1c",
        "warning": "#9d5d00",
        "control.bg": "#ffffff",
        "control.bg.hover": "#f9f9f9",
        "control.bg.disabled": "#f2f2f2",
        "scrollbar.handle": "#c4c4c4",
        "scrollbar.handle.hover": "#a0a0a0",
    },
    "dark": {
        "window": "#202020",
        "bg.base": "#202020",
        "bg.layer": "#272727",
        "bg.card": "#2b2b2b",
        "bg.subtle": "#2d2d2d",
        "bg.hover": "#323232",
        "bg.pressed": "#383838",
        "stroke.card": "#303030",
        "stroke.control": "#3d3d3d",
        "stroke.divider": "#2d2d2d",
        "text.primary": "#ffffff",
        "text.secondary": "#cccccc",
        "text.tertiary": "#969696",
        "text.disabled": "#6e6e6e",
        "text.link": "#60cdff",
        "success": "#6ccb5f",
        "danger": "#ff99a4",
        "warning": "#f5c518",
        "control.bg": "#2d2d2d",
        "control.bg.hover": "#333333",
        "control.bg.disabled": "#262626",
        "scrollbar.handle": "#4f4f4f",
        "scrollbar.handle.hover": "#5f5f5f",
    },
}

# 系统强调色读不到时的回退（Fluent 蓝）
_ACCENT_FALLBACK = {"light": "#0067c0", "dark": "#4cc2ff"}

# Mod 更新界面使用独立调色板，内联样式与 HTML 共用颜色。
_MOD_UPDATE_COLORS = {
    "light": {
        "surface": "#ffffff",
        "layer": "#fbfbfc",
        "subtle": "#f3f5f7",
        "hover": "#edf1f5",
        "line": "#dfe3e9",
        "divider": "#eaedf1",
        "control": "#ccd2db",
        "text": "#20242c",
        "secondary": "#626b78",
        "muted": "#77808b",
        "accent": "#0067c0",
        "accent_hover": "#075ba5",
        "accent_soft": "#eaf3fc",
        "accent_border": "#bdd8f1",
        "on_accent": "#ffffff",
        "green": "#19764b",
        "green_soft": "#eaf5ee",
        "warning": "#8a5c11",
        "warning_soft": "#fff7e7",
        "warning_border": "#ead7ae",
    },
    "dark": {
        "surface": "#272a2f",
        "layer": "#23262b",
        "subtle": "#2c3037",
        "hover": "#343942",
        "line": "#3d424b",
        "divider": "#353a43",
        "control": "#4b525e",
        "text": "#f2f4f8",
        "secondary": "#bac2ce",
        "muted": "#9ca7b6",
        "accent": "#66bfff",
        "accent_hover": "#8bcfff",
        "accent_soft": "#253b50",
        "accent_border": "#3a5a75",
        "on_accent": "#102b40",
        "green": "#81cfa5",
        "green_soft": "#293e33",
        "warning": "#e5c07a",
        "warning_soft": "#393326",
        "warning_border": "#5d5036",
    },
}


# 背景启用时，内容表面使用统一配置的半透明填充。
#   nav     —— 左侧导航（单层，勿与容器叠加）
#   card    —— 卡片主体
#   panel   —— 信息面板 / 分组框 / 列表表格等内容承载面
#   list    —— 列表/表格行区
#   strong  —— hover / 需更实的叠色
#   placeholder —— 预览图占位区：直接透出所在卡片/面板的亚克力（返回 transparent）

ACRYLIC = {
    "nav": 0.60,
    "card": 0.70,
    "panel": 0.80,
    "list": 0.80,
    "strong": 0.88,
}

# 浅色设置使用专用配色，深色设置复用应用表面、文字和系统强调色。
_SETTINGS_LIGHT_COLORS = {
    "canvas": "#f5f6f8",
    "surface": "#ffffff",
    "soft": "#f5f7fa",
    "hover": "#eef1f5",
    "text": "#202a37",
    "secondary": "#526174",
    "muted": "#626f80",
    "line": "#e0e5ed",
    "control": "#ccd5e0",
    "accent": "#0067c0",
    "accent_soft": "#e8f2fc",
    "teal": "#138577",
    "teal_soft": "#e9f6f3",
    "danger": "#b73137",
    "warning": "#906311",
    "warning_bg": "#fff6e3",
    "on_accent": "#ffffff",
}
_SETTINGS_DARK_TOKENS = {
    "canvas": "bg.base",
    "surface": "bg.card",
    "soft": "bg.subtle",
    "hover": "bg.hover",
    "text": "text.primary",
    "secondary": "text.secondary",
    "muted": "text.tertiary",
    "line": "stroke.card",
    "control": "stroke.control",
    "accent": "accent",
    "accent_soft": "accent.subtle",
    "teal": "success",
    "danger": "danger",
    "warning": "warning",
    "on_accent": "text.on_accent",
}

_VALID_THEMES = ("light", "dark")
_current_theme = "light"
_background_active = False  # 是否启用了整窗背景图（决定表面走亚克力还是纯不透明色）
_palette_cache: dict[str, dict[str, str]] = {}
_system_accent_cache: str | None = None
_system_accent_read = False


# 颜色工具


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    """将三位或六位十六进制颜色转换为三个整数通道。"""
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    """将 RGB 通道舍入并限制到有效范围，再编码为六位十六进制颜色。"""

    def clamp(v):
        """把单个颜色通道舍入到零至二百五十五的整数范围。"""
        return max(0, min(255, int(round(v))))

    return f"#{clamp(r):02x}{clamp(g):02x}{clamp(b):02x}"


def _mix(a: str, b: str, t: float) -> str:
    """按比例 t(0..1) 把 a 向 b 混合。t=0→a，t=1→b。"""
    ar, ag, ab = _hex_to_rgb(a)
    br, bg, bb = _hex_to_rgb(b)
    return _rgb_to_hex(ar + (br - ar) * t, ag + (bg - ag) * t, ab + (bb - ab) * t)


def _luminance(h: str) -> float:
    """按 RGB 加权估算零到一的亮度，用于选择强调色上的文字颜色。"""
    r, g, b = _hex_to_rgb(h)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def system_accent() -> str | None:
    """读取 Win11 系统强调色 (#RRGGBB)；非 Windows 或失败返回 None。"""
    global _system_accent_cache, _system_accent_read
    if _system_accent_read:
        return _system_accent_cache
    _system_accent_read = True
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM") as key:
            value, _ = winreg.QueryValueEx(key, "AccentColor")
        # DWORD 以 0xAABBGGRR 存储
        r = value & 0xFF
        g = (value >> 8) & 0xFF
        b = (value >> 16) & 0xFF
        _system_accent_cache = _rgb_to_hex(r, g, b)
        logger.debug("系统强调色: %s", _system_accent_cache)
    except Exception as e:
        logger.debug("读取系统强调色失败，回退 Fluent 蓝: %s", e)
        _system_accent_cache = None
    return _system_accent_cache


def _build_palette(theme: str) -> dict[str, str]:
    """从明暗基础色板生成强调色、选中色和设置页色板，优先采用系统强调色。"""
    pal = dict(_BASE[theme])
    pal.update({f"mod_updates.{key}": value for key, value in _MOD_UPDATE_COLORS[theme].items()})
    sys_accent = system_accent()
    if sys_accent:
        # 暗色下把系统强调色提亮以保证对比度
        accent = sys_accent if theme == "light" else _mix(sys_accent, "#ffffff", 0.28)
    else:
        accent = _ACCENT_FALLBACK[theme]

    pal["accent"] = accent
    pal["accent.hover"] = _mix(accent, "#ffffff", 0.12)
    pal["accent.pressed"] = _mix(accent, "#000000", 0.12)
    pal["accent.subtle"] = _mix(accent, pal["bg.base"], 0.86)  # 选中态柔和底色
    pal["text.on_accent"] = "#1a1a1a" if _luminance(accent) > 0.62 else "#ffffff"
    pal["selection.bg"] = accent
    pal["selection.text"] = pal["text.on_accent"]
    if theme == "dark":
        settings_colors = {key: pal[token] for key, token in _SETTINGS_DARK_TOKENS.items()}
        settings_colors["teal_soft"] = _mix(pal["success"], pal["bg.card"], 0.9)
        settings_colors["warning_bg"] = _mix(pal["warning"], pal["bg.card"], 0.9)
    else:
        settings_colors = _SETTINGS_LIGHT_COLORS
    pal.update({f"settings.{key}": value for key, value in settings_colors.items()})
    return pal


def _palette(theme: str | None = None) -> dict[str, str]:
    """解析主题名并按主题缓存完整语义色板。"""
    theme = _resolve(theme)
    if theme not in _palette_cache:
        _palette_cache[theme] = _build_palette(theme)
    return _palette_cache[theme]


def _resolve(theme: str | None) -> str:
    """接受有效的显式主题名，否则回退当前应用主题。"""
    if theme in _VALID_THEMES:
        return theme  # type: ignore[return-value]
    return _current_theme


# 公共 API


def set_theme(theme: str) -> str:
    """设置当前主题（``light`` / ``dark`` / ``auto``）。返回解析后的实际主题名。"""
    global _current_theme
    _current_theme = resolve_auto(theme)
    return _current_theme


def current_theme() -> str:
    """返回当前应用使用的主题名称。"""
    return _current_theme


# 快捷键页面使用局部对比度调色板，强调色仍跟随全局主题。
_SHORTCUT_COLORS = {
    "light": {
        "text.tertiary": "#737373",
        "warning": "#8d5300",
        "warning.subtle": "#fff5e4",
        "success.subtle": "#edf6ec",
        "accent.subtle": "#e8f1fa",
        "accent.hover": "#005aab",
        "key.shadow": "#10000000",
        "scrollbar.handle": "#bfbfbf",
    },
    "dark": {
        "text.tertiary": "#a0a0a0",
        "stroke.card": "#383838",
        "stroke.control": "#494949",
        "stroke.divider": "#383838",
        "bg.hover": "#343434",
        "bg.pressed": "#3c3c3c",
        "warning.subtle": "#413821",
        "success.subtle": "#293c28",
        "accent.subtle": "#243e4b",
        "accent.hover": "#78d2ff",
        "text.on_accent": "#14222a",
        "key.shadow": "#33000000",
        "scrollbar.handle": "#575757",
    },
}


def shortcut_color(token: str) -> str:
    """返回快捷键页局部颜色，其他颜色复用全局主题。"""
    if system_accent() and token in {"accent.subtle", "accent.hover", "text.on_accent"}:
        return color(token)
    return _SHORTCUT_COLORS[current_theme()].get(token) or color(token)


def set_background_active(on: bool) -> None:
    """记录当前是否启用整窗背景图。

    由 :meth:`App.apply_theme` 在渲染 QSS 前调用。置位后，所有走
    :func:`surface_color` 的内联样式会改用半透明亚克力叠色（QSS 模板里的
    表面由 :func:`background_overlay_qss` 统一覆盖）。
    """
    global _background_active
    _background_active = bool(on)


def is_background_active() -> bool:
    """返回主题系统是否启用了自定义背景材质。"""
    return _background_active
