"""按当前主题着色并缓存 Fluent SVG 图标。

图标位于 resources/icons/fluent，get_icon 返回主题化图标，主题切换后调用 clear_cache。
"""

from __future__ import annotations

import re
from functools import lru_cache

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from src.ui import theme
from src.utils.logger import logger
from src.utils.paths import get_resources_dir

# 着色后的高分渲染尺寸；QIcon 会按控件 iconSize 平滑缩小，保证各尺寸清晰
_RENDER_PX = 64

_icon_cache: dict[tuple[str, str], QIcon] = {}
_svg_text_cache: dict[str, str | None] = {}

_FILL_RE = re.compile(r'(fill|stroke)="#[0-9a-fA-F]{3,8}"')
_CURRENT_RE = re.compile(r'(fill|stroke)="currentColor"')


def _icons_dir():
    """返回应用资源目录下 Fluent SVG 图标的位置。"""
    return get_resources_dir() / "icons" / "fluent"


def _load_svg(name: str) -> str | None:
    """按图标名缓存 UTF-8 SVG 文本；读取失败记录警告并缓存空结果。"""
    if name in _svg_text_cache:
        return _svg_text_cache[name]
    path = _icons_dir() / f"{name}.svg"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("图标缺失: %s", path)
        text = None
    _svg_text_cache[name] = text
    return text


def _recolor(svg: str, hex_color: str) -> str:
    """把 SVG 内所有非 ``none`` 的 fill/stroke 改为目标色。"""
    svg = _FILL_RE.sub(lambda m: f'{m.group(1)}="{hex_color}"', svg)
    svg = _CURRENT_RE.sub(lambda m: f'{m.group(1)}="{hex_color}"', svg)
    return svg


def _render_pixmap(svg: str, px: int) -> QPixmap:
    """将 SVG 渲染为指定像素边长的透明背景位图。"""
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    renderer.render(painter)
    painter.end()
    return pm


def get_icon(name: str, color_token: str = "text.primary", theme_name: str | None = None) -> QIcon:
    """加载并按主题着色的 QIcon。

    name: ``"<category>/<name>"``，对应 resources/icons/fluent 下的 SVG。
    color_token: :mod:`theme` 颜色 token（如 ``"accent"``、``"success"``）。
    """
    hex_color = theme.color(color_token, theme_name)
    key = (name, hex_color)
    cached = _icon_cache.get(key)
    if cached is not None:
        return cached

    svg = _load_svg(name)
    if svg is None:
        icon = QIcon()
        _icon_cache[key] = icon
        return icon

    pm = _render_pixmap(_recolor(svg, hex_color), _RENDER_PX)
    icon = QIcon(pm)
    _icon_cache[key] = icon
    return icon


def pixmap(name: str, size: int, color_token: str = "text.primary", theme_name: str | None = None) -> QPixmap:
    """渲染指定尺寸、已着色的 QPixmap（用于需要直接贴图的徽章等）。"""
    hex_color = theme.color(color_token, theme_name)
    return colored_pixmap_from(name, size, hex_color)


def colored_pixmap_from(name: str, size: int, hex_color: str) -> QPixmap:
    """按显式十六进制色渲染（不经 token），供动态色场景使用。"""
    # 像素图隐式共享，调用方可修改自己的副本而不改变缓存。
    return QPixmap(_cached_pixmap(name, size, hex_color))


@lru_cache(maxsize=256)
def _cached_pixmap(name: str, size: int, hex_color: str) -> QPixmap:
    """读取并着色 SVG 后渲染位图，缺失图标返回同尺寸透明占位图。"""
    svg = _load_svg(name)
    if svg is None:
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        return pm
    return _render_pixmap(_recolor(svg, hex_color), size)


def clear_cache() -> None:
    """主题切换后清空着色缓存（SVG 源文本缓存保留）。"""
    _icon_cache.clear()
    _cached_pixmap.cache_clear()


# 主题化图标「打标 + 整树重涂」：解决换主题后已设到控件上的旧色图标不更新的问题。
# clear_cache 只让**新**请求获取新色；已 setIcon/setPixmap 到控件上的 QIcon/QPixmap
# 仍是旧色。用 apply_icon/apply_pixmap 设图标时把 (name, token) 记成动态属性，换主题时
# 主题切换时遍历已登记图标，按当前颜色重新生成并应用。

_ICON_NAME_PROP = "_themed_icon_name"
_ICON_TOKEN_PROP = "_themed_icon_token"
_PIX_PROP = "_themed_pixmap"  # [name, size, token]


def apply_icon(target, name: str, color_token: str = "text.primary", theme_name: str | None = None):
    """给 QAbstractButton / QAction 设主题化图标并打标（换主题时 retheme_icons 会重涂）。"""
    try:
        target.setProperty(_ICON_NAME_PROP, name)
        target.setProperty(_ICON_TOKEN_PROP, color_token)
    except Exception:
        pass
    target.setIcon(get_icon(name, color_token, theme_name))
    return target


def apply_pixmap(label, name: str, size: int, color_token: str = "text.primary", theme_name: str | None = None):
    """给 QLabel 设主题化 pixmap 并打标（换主题时 retheme_icons 会重涂）。"""
    try:
        label.setProperty(_PIX_PROP, [name, int(size), color_token])
    except Exception:
        pass
    label.setPixmap(pixmap(name, size, color_token, theme_name))
    return label


def apply_combo_item_icon(combo, index: int, name: str, color_token: str = "text.primary") -> None:
    """为下拉框选项设置主题图标，并保存刷新所需信息。

    选项图标不属于 QObject 子树，因此在下拉框上记录索引、图标名及颜色标识，
    供主题刷新时调用 setItemIcon。须在选项创建后调用。
    """
    try:
        m = getattr(combo, "_themed_item_icons", None)
        if m is None:
            m = {}
            combo._themed_item_icons = m
        m[int(index)] = (name, color_token)
    except Exception:
        pass
    try:
        combo.setItemIcon(int(index), get_icon(name, color_token))
    except Exception:
        pass


def retheme_icons(root) -> None:
    """主题切换后，重涂 ``root`` 子树里所有用 apply_icon/apply_pixmap/apply_combo_item_icon
    打过标的图标控件（按钮 / 动作 / 标签 / 下拉项）。

    须在 :func:`clear_cache` 之后调用（让重涂取到新色）。findChildren(QObject) 也能取到
    parented 的 QAction（菜单 / QLineEdit 行内动作）。逐个 try/except，任一控件失败不影响其余。
    """
    from PySide6.QtCore import QObject

    try:
        targets = root.findChildren(QObject)
    except Exception:
        return
    targets.append(root)
    for w in targets:
        try:
            n = w.property(_ICON_NAME_PROP)
            if n:
                w.setIcon(get_icon(n, w.property(_ICON_TOKEN_PROP) or "text.primary"))
            p = w.property(_PIX_PROP)
            if p:
                w.setPixmap(pixmap(p[0], int(p[1]), p[2]))
            items = getattr(w, "_themed_item_icons", None)
            if items:
                for idx, (name, token) in items.items():
                    w.setItemIcon(idx, get_icon(name, token))
            # 自绘图标的按钮（RingProgressButton）——中心图标不是 setIcon，属性遍历
            # 无法获取，改由它自己的 retheme 重建 pixmap。
            if w.property("_ring_progress_button"):
                w.retheme()
        except Exception:
            pass


def write_themed_svg(
    name: str, color_token: str = "text.secondary", theme_name: str | None = None, tag: str | None = None
) -> str | None:
    """把图标按 token 着色后写入临时目录，返回绝对路径（供 QSS ``url`` 用）。

    用于 QComboBox::down-arrow 等需要在样式表里引用图片文件的场景。临时目录用
    纯 ASCII 路径，避免 Qt 在含非 ASCII 项目路径下解析 QSS url 失败。
    """
    hex_color = theme.color(color_token, theme_name)
    svg = _load_svg(name)
    if svg is None:
        return None
    try:
        from src.utils.paths import get_qss_icon_cache_dir

        out_dir = get_qss_icon_cache_dir()
        safe = name.replace("/", "_")
        out = out_dir / f"{safe}_{tag or hex_color.lstrip('#')}.svg"
        # 文件名已含主题/颜色 → 内容确定，存在就不重复写盘（每次换主题都 render_qss，
        # 避免无谓磁盘 I/O 拖慢换肤）。
        if not out.exists():
            out.write_text(_recolor(svg, hex_color), encoding="utf-8")
        return str(out)
    except OSError:
        logger.debug("写入主题化 SVG 失败: %s", name)
        return None
