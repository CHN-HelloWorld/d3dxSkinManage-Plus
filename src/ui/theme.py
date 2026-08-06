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


def surface_color(token: str, role: str = "panel", theme: str | None = None) -> str:
    """表面色：无背景图时返回不透明 token 色；启用背景图时返回亚克力叠色。

    供「内联样式」调用点使用（QSS 模板内的选择器由 overlay 统一处理）。
    ``role="placeholder"`` 时启用背景图返回 ``transparent``——让预览图占位区
    直接透出其所在卡片/面板的亚克力，而非再叠一层灰块。
    """
    hexc = color(token, theme)
    if not _background_active:
        return hexc
    if role == "placeholder":
        return "transparent"
    return _rgba(hexc, ACRYLIC.get(role, ACRYLIC["panel"]))


def rgba(token: str, alpha: float, theme: str | None = None) -> str:
    """公开的 token→rgba 工具：供需要任意不透明度叠色的调用点（如亚克力弹窗过滤器）使用。"""
    return _rgba(color(token, theme), alpha)


def surface_text_color(token: str, theme: str | None = None) -> str:
    """保持浅色透明表面上的次要文字可读。"""
    mode = resolve_auto(theme or current_theme())
    if _background_active and mode == "light" and token in ("settings.secondary", "settings.muted"):
        return _mix(
            color("settings.secondary", mode),
            color("settings.text", mode),
            0.55 if token == "settings.secondary" else 0.40,
        )
    if _background_active and mode == "light" and token in ("text.secondary", "text.tertiary"):
        return _mix(
            color("text.secondary", mode), color("text.primary", mode), 0.55 if token == "text.secondary" else 0.30
        )
    return color(token, mode)


def resolve_auto(theme: str) -> str:
    """把 ``auto`` 解析成 light/dark（按 Windows 应用主题）；其余原样校验。"""
    if theme in _VALID_THEMES:
        return theme
    if theme == "auto":
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if light else "dark"
        except Exception:
            return "light"
    return "light"


def color(token: str, theme: str | None = None) -> str:
    """解析颜色 token → ``#rrggbb``。未知 token 返回品红以便目视发现。"""
    pal = _palette(theme)
    val = pal.get(token)
    if val is None:
        logger.warning("未知颜色 token: %s", token)
        return "#ff00ff"
    return val


def radius(name: str) -> int:
    """读取命名圆角尺寸，未知名称回退标准控件圆角。"""
    return RADII.get(name, RADII["control"])


def space(name: str) -> int:
    """读取命名间距，未知名称回退中等间距。"""
    return SPACING.get(name, SPACING["md"])


def font(key: str) -> str:
    """返回字体令牌的字符串值；字体族实时读取已配置的 QSS 字体列表。"""
    if key == "family":
        from src.ui.fonts import qss_family

        return qss_family()
    return str(FONT.get(key, ""))


# QSS 渲染


def _qss_vars(theme: str) -> dict[str, str]:
    """汇总颜色、圆角、间距和字体为 QSS 占位符映射，并为尺寸补充像素单位。"""
    pal = _palette(theme)
    vars_: dict[str, str] = dict(pal)
    for k, v in RADII.items():
        vars_[f"radius.{k}"] = f"{v}px"
    for k, v in SPACING.items():
        vars_[f"space.{k}"] = f"{v}px"
    vars_["font.family"] = font("family")
    vars_["font.size"] = f"{FONT['size']}px"
    vars_["font.size_small"] = f"{FONT['size_small']}px"
    vars_["font.size_title"] = f"{FONT['size_title']}px"
    vars_["font.size_subtitle"] = f"{FONT['size_subtitle']}px"
    return vars_


_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def render_qss(theme: str) -> str:
    """读取 QSS 模板并注入当前主题 token，返回最终样式表字符串。"""
    theme = resolve_auto(theme)
    tmpl_path = get_resources_dir() / "themes" / "fluent.qss"
    try:
        template = tmpl_path.read_text(encoding="utf-8")
    except OSError as e:
        logger.error("读取 QSS 模板失败: %s", e)
        return ""

    vars_ = _qss_vars(theme)

    # 下拉箭头：按主题着色的 chevron-down，供 QSS QComboBox::down-arrow 引用。
    # 延迟导入 icons 避免与其对 theme 的依赖形成循环。
    try:
        from src.ui import icons

        arrow = icons.write_themed_svg("actions/chevron-down", "text.secondary", theme, tag=f"down-{theme}")
    except Exception:
        arrow = None
    if not arrow:
        arrow = str(get_resources_dir() / "icons" / "fluent" / "actions" / "chevron-down.svg")
    vars_["arrow.down"] = arrow.replace("\\", "/")

    def _sub(m: re.Match[str]) -> str:
        """替换一个已知样式令牌，未知占位符记录警告并显示洋红色以暴露配置问题。"""
        key = m.group(1)
        if key not in vars_:
            logger.warning("QSS 未知占位符: {{%s}}", key)
            return "#ff00ff"
        return vars_[key]

    return _PLACEHOLDER.sub(_sub, template)


def _rgba(hex_color: str, alpha: float) -> str:
    """把 #rrggbb 转成 QSS 的 rgba(r,g,b,a)。解析失败回退原值。"""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError):
        return hex_color
    return f"rgba({r}, {g}, {b}, {alpha:.2f})"


def background_overlay_qss(theme: str) -> str:
    """构造自定义背景启用时的表面覆盖样式。

    布局容器透明，内容表面使用半透明底色；导航只保留一层填充，
    预览占位透出所属表面。输入及弹窗使用各自可读的背景。
    """
    theme = resolve_auto(theme)
    layer = color("bg.layer", theme)
    card = color("bg.card", theme)
    accent = color("accent", theme)
    stroke = color("stroke.card", theme)
    radius_card = f"{RADII['card']}px"

    card_fill = _rgba(card, ACRYLIC["card"])
    panel_fill = _rgba(layer, ACRYLIC["panel"])
    list_fill = _rgba(card, ACRYLIC["list"])
    hover_fill = _rgba(color("bg.hover", theme), ACRYLIC["strong"])
    nav_fill = _rgba(layer, ACRYLIC["nav"])
    # 导航已有 60% 容器底色；轻叠交互色，避免再次用 strong 遮住背景。
    nav_hover_fill = _rgba(color("bg.pressed", theme), 0.50)
    nav_pressed_fill = _rgba(color("bg.pressed", theme), 0.65)
    nav_selected_fill = _rgba(accent, 0.26)
    # 详情面板提高填充不透明度，保证长文本和文件列表可读。
    gb_detail_fill = _rgba(layer, 0.90)
    # 菜单和提示的透明属性由弹窗过滤器设置，此处提供未应用原生效果时的底色。
    menu_solid = color("bg.card", theme)
    input_fill = _rgba(color("control.bg", theme), 0.55)
    popup_fill = _rgba(card, 0.62)
    btn_fill = _rgba(card, 0.55)
    group_fill = _rgba(layer, 0.50)

    return (
        "/* ===== F2 背景图：亚克力材质覆盖层 ===== */\n"
        # 普通容器透明，透出底层背景绘制层
        "QWidget { background-color: transparent; }\n"
        # 对话框 / 菜单栏：不透明，聚焦优先
        f"QDialog, QMessageBox, QMenuBar {{ background-color: {layer}; }}\n"
        # 左侧导航：单层亚克力（容器着色，列表/按钮透出，避免双层叠加过暗）
        f"#navContainer {{ background-color: {nav_fill}; }}\n"
        "#navList, #navLaunchBtn, #navCollapseBtn { background-color: transparent; }\n"
        "#navList::item:hover:!selected, #navLaunchBtn:hover, #navCollapseBtn:hover {"
        f" background-color: {nav_hover_fill}; }}\n"
        "#navLaunchBtn:pressed, #navCollapseBtn:pressed {"
        f" background-color: {nav_pressed_fill}; }}\n"
        # 选中态保留强调色与指示条，同时透出导航下方的背景。
        '#navList::item:selected, #navLaunchBtn[current="true"] {'
        f" background-color: {nav_selected_fill}; }}\n"
        # 设置卡片背景后须显式绘制边框和圆角，以替代失效的原生面板边缘。
        "#charCard, #modCard, #galleryItem, #gameCard, #gbModCard {\n"
        f"    background-color: {card_fill};\n"
        f"    border: 1px solid {stroke};\n"
        f"    border-radius: {radius_card};\n"
        "}\n"
        "#charCard:hover, #modCard:hover, #galleryItem:hover,"
        " #gameCard:hover, #gbModCard:hover {\n"
        f"    background-color: {hover_fill};\n"
        f"    border-color: {accent};\n"
        "}\n"
        # 商店卡片使用统一的亚克力底色，信息区与预览占位区保持透明，
        # 避免子控件背景叠加或形成独立的矩形边界。
        "#charThumb, #modPreview, #galleryImage, #gamePreview"
        " { background-color: transparent; }\n"
        # 预览面板只绘制一层亚克力背景，内部信息区保持透明，
        # 并移除基础样式边框，避免多层叠加和矩形边缘遮盖圆角。
        f"#previewPanel {{ background-color: {panel_fill}; border-radius: {radius_card};"
        f" border: none; }}\n"
        "#previewInfoFrame { background-color: transparent; }\n"
        # 商店详情栏（悬浮 QFrame）：单层亚克力面板 + 边框，内部滚动区由内联置透明
        f"#gbDetailPanel {{ background-color: {gb_detail_fill}; border: 1px solid {stroke};"
        f" border-radius: {radius_card}; }}\n"
        # 面包屑条：亚克力（其底色实际由内联 surface_color 设定，这里回退处理）
        f"#breadcrumbFrame {{ background-color: {panel_fill}; border-radius: {radius_card}; }}\n"
        # 设置和工具页的分组框使用低不透明度面板，并将标题置于面板内。
        # 按 objectName 定位面板，避免 frameShape="6" 选择器同时匹配 QScrollArea，
        # 使画廊、商店和工具页的滚动内容区出现不透明底色。
        "QGroupBox {\n"
        f"    background-color: {group_fill};\n"
        "    margin-top: 0;\n"
        "    padding: 26px 12px 12px 12px;\n"
        "}\n"
        "QGroupBox::title {\n"
        "    subcontrol-origin: padding;\n"
        "    subcontrol-position: top left;\n"
        "    top: 6px; left: 10px;\n"
        "}\n"
        # 滚动区视口的底色（默认 StyledPanel + 视口 autofill）无法被全局选择器清掉，
        # 只有「内联」QScrollArea{background:transparent} 才能透出——见各页 scroll 代码。
        # 列表 / 表格 / 树：内容承载面，亚克力（#navList 已被上面置透明，特异度更高）
        "QTableWidget, QTableView, QTreeWidget, QTreeView, QListView {\n"
        f"    background-color: {list_fill};\n"
        "}\n"
        # 为输入和弹窗重新声明背景，避免继承通用容器的透明规则。
        # 右键菜单 / 提示：不透明，避免「过于透明」看不清
        f"QMenu {{ background-color: {menu_solid}; }}\n"
        f"QToolTip {{ background-color: {menu_solid}; }}\n"
        # 输入 / 下拉 / 数字框：亚克力（搜索栏、排序/筛选下拉等）
        "QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {\n"
        f"    background-color: {input_fill};\n"
        "}\n"
        # 下拉弹窗：亚克力（特异度 2，需 ≥ 基础同名规则）
        f"QComboBox QAbstractItemView {{ background-color: {popup_fill}; }}\n"
        # 普通按钮：轻亚克力可见（强调按钮 [accent] 特异度更高，保持原强调色）
        f"QPushButton {{ background-color: {btn_fill}; }}\n"
        # 预设栏按钮（QToolButton，需做成下拉框那样的亚克力条）
        f"#presetBtn {{ background-color: {input_fill};"
        f" border: 1px solid {color('stroke.control', theme)}; border-radius: {RADII['control']}px; }}\n"
        # 悬停反馈：#presetBtn(id) 比基础 QToolButton:hover 特异度高会盖掉它，故需显式补回
        f"#presetBtn:hover {{ background-color: {hover_fill}; }}\n"
    )


def _with_selector_attribute(qss: str, attribute: str) -> str:
    """在伪选择器之前给规则目标增加属性。

    跟踪括号和引号，保留属性值、后代关系和子控件选择器的语义。
    """
    qss = re.sub(r"/\*.*?\*/", "", qss, flags=re.S)

    def selectors(source: str) -> str:
        """解析逗号分隔的 QSS 选择器，在末级伪状态之前插入属性，并保留引号与属性内部字符。"""
        pieces = []
        start = 0
        depth = 0
        quote = ""
        escaped = False
        pseudo = None
        for i, char in enumerate(source + ","):
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
            elif char in "\"'":
                quote = char
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
            elif depth == 0:
                if char == ",":
                    end = len(source[:i].rstrip())
                    at = pseudo if pseudo is not None else end
                    pieces.append(source[start:at].strip() + attribute + source[at:end])
                    start = i + 1
                    pseudo = None
                elif char.isspace() or char in ">+~":
                    # 属性加在最右侧目标控件上，并忽略伪选择器后的空白。
                    if source[i:].strip() and not source[i:].lstrip().startswith(","):
                        pseudo = None
                elif char == ":" and pseudo is None:
                    pseudo = i
        return ", ".join(pieces)

    return re.sub(r"([^{}]+)\{([^{}]*)\}", lambda match: selectors(match[1]) + " {" + match[2] + "}", qss)


def appearance_qss(theme: str) -> str:
    """在同一样式表保留两种背景状态，便于分批切换。

    为基础规则补充等权重条件，避免背景属性意外覆盖强调、禁用和悬停规则。
    """
    return (
        _with_selector_attribute(render_qss(theme), "[enabled]")
        + "\n"
        + _with_selector_attribute(background_overlay_qss(theme), '[_backgroundSurface="true"]')
        + f'\nQDialog[dialogFrameInstalled="true"] {{ background-color: {color("mod_updates.surface", theme)};'
        + f" border: 1px solid {color('mod_updates.control', theme)}; border-radius: 10px; }}"
        + '\nQDialog[_dialog_acrylic="true"],'
        + ' QDialog[dialogFrameInstalled="true"][_dialog_acrylic="true"]'
        + " { background-color: transparent; border: none; }"
    )
