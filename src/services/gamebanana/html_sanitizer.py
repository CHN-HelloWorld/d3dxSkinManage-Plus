"""HTML 内容白名单清洗（用于 QTextBrowser 显示 GameBanana 描述）"""

from __future__ import annotations

import re
from typing import Literal

import bleach
from bleach.css_sanitizer import CSSSanitizer

from src.utils.i18n import t

# 仅允许展示所需的 CSS 属性；依赖由安装配置统一保证。
_CSS_SANITIZER = CSSSanitizer(
    allowed_css_properties=[
        "color",
        "background-color",
        "background",
        "font-weight",
        "font-style",
        "font-size",
        "font-family",
        "text-align",
        "text-decoration",
        "padding",
        "margin",
        "border",
        "border-radius",
        "border-color",
        "width",
        "height",
        "max-width",
        "max-height",
        "opacity",
    ]
)

ALLOWED_TAGS: list[str] = [
    "p",
    "br",
    "a",
    "img",
    "b",
    "i",
    "u",
    "strong",
    "em",
    "ul",
    "ol",
    "li",
    "blockquote",
    "code",
    "pre",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "table",
    "thead",
    "tbody",
    "tr",
    "td",
    "th",
    "div",
    "span",
    "hr",
    # GameBanana 描述仍使用这些标签，Qt 可直接渲染其字号、颜色和对齐属性。
    "font",
    "center",
]

ALLOWED_ATTRS: dict[str, list[str]] = {
    "a": ["href", "title", "target"],
    "img": ["src", "alt", "width", "height"],
    # <font size="6" color="#b388ff" face="Arial">——三者都是普通属性（非 style），
    # 无需 css_sanitizer 即可通过 bleach，Qt 原生按 HTML 逻辑字号(1~7)/颜色渲染。
    "font": ["size", "color", "face"],
    "*": ["class", "style", "align"],
}

ALLOWED_PROTOCOLS: list[str] = ["https", "http", "mailto"]

# 危险元素须连同正文删除，避免只移除标签后显示脚本内容。
_DANGEROUS_RE = re.compile(
    r"<(script|style|iframe|object|embed|noscript)\b[^>]*>.*?</\1>"
    r"|<(?:script|style|iframe|object|embed|noscript)\b[^>]*/>",
    re.IGNORECASE | re.DOTALL,
)

_VIDEO_RE = re.compile(r"<video\b[^>]*>.*?</video>|<video\b[^>]*/>", re.IGNORECASE | re.DOTALL)

# linkify 会转义 font 和 center 标签；这些标签及属性已由白名单校验，
# 可在链接处理后恢复，供 QTextBrowser 渲染。
_RESTORE_LEGACY_RE = re.compile(r"&lt;(/?(?:font|center)\b[^&]*?)&gt;", re.IGNORECASE)

# 按 HTML 颜色解析规则规范化 font 元素的 color 属性。
# 例如 #6ECFF 转换为 #6ECFF0，避免 Qt 将不完整的十六进制颜色显示为黑色。
_FONT_TAG_RE = re.compile(r"<font\b[^>]*>", re.IGNORECASE)
_FONT_COLOR_ATTR_RE = re.compile(r'color\s*=\s*"([^"]*)"', re.IGNORECASE)
_HEXCHARS = set("0123456789abcdefABCDEF")


def _parse_legacy_color(value: str) -> str | None:
    """按 HTML 颜色解析规则返回合法的 #RRGGBB 或具名颜色。

    空值、无效值和 transparent 返回 None，由调用方移除属性并使用继承颜色。
    具名颜色原样交给 Qt 解析。
    """
    s = (value or "").strip()
    if not s or s.lower() == "transparent":
        return None
    if re.fullmatch(r"[A-Za-z]+", s):
        return s  # 具名颜色交给 Qt 解析。
    if len(s) == 4 and s[0] == "#" and all(c in _HEXCHARS for c in s[1:]):
        r, g, b = s[1], s[2], s[3]
        return f"#{r}{r}{g}{g}{b}{b}"  # #RGB 简写 → #RRGGBB
    t = "".join("00" if ord(c) > 0xFFFF else c for c in s)[:128]
    if t.startswith("#"):
        t = t[1:]
    t = "".join(c if c in _HEXCHARS else "0" for c in t)
    while len(t) == 0 or len(t) % 3:
        t += "0"
    n = len(t) // 3
    r, g, b = t[:n], t[n : 2 * n], t[2 * n : 3 * n]
    if n > 8:
        r, g, b, n = r[n - 8 :], g[n - 8 :], b[n - 8 :], 8
    while n > 2 and r[0] == "0" and g[0] == "0" and b[0] == "0":
        r, g, b, n = r[1:], g[1:], b[1:], n - 1
    if n > 2:
        r, g, b = r[:2], g[:2], b[:2]
    return "#" + "".join(c.rjust(2, "0") for c in (r, g, b))


def _normalize_font_colors(html: str) -> str:
    """将保留的 font 标签颜色转换为 Qt 可识别的安全格式，移除无效颜色属性。"""

    def _fix_tag(m: re.Match[str]) -> str:
        """仅规范化当前 font 标签内的颜色属性，保留标签的其他内容。"""

        def _fix_color(cm: re.Match[str]) -> str:
            """将可解析颜色写为规范的 color 属性，无法解析时移除该属性。"""
            parsed = _parse_legacy_color(cm.group(1))
            return f'color="{parsed}"' if parsed else ""

        return _FONT_COLOR_ATTR_RE.sub(_fix_color, m.group(0))

    return _FONT_TAG_RE.sub(_fix_tag, html)


# 样式仅使用 QTextBrowser 支持的 CSS 属性，统一设置正文、标题、链接及段落间距。
# 不使用 flex、hover 等不受支持的布局或交互规则。
_CSS_DARK = """
<style>
body {
    color: #cdd1d6;
    background: transparent;
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: 14px;
    }
p { margin-top: 8px; margin-bottom: 8px; }
a { color: #56a2ff; text-decoration: none; }
h1, h2, h3, h4, h5, h6 { color: #f5f7fa; font-weight: bold; margin-top: 14px; margin-bottom: 6px; }
h1 { font-size: 20px; }
h2 { font-size: 17px; }
h3 { font-size: 15px; }
h4, h5, h6 { font-size: 14px; }
ul, ol { margin-top: 6px; margin-bottom: 6px; }
li { margin-top: 2px; margin-bottom: 2px; }
img { max-width: 100%; }
code,
pre {
    background: #1f1f1f;
    color: #f1f1f1;
    padding: 2px 4px;
    font-family: Consolas, "Courier New", monospace;
    }
blockquote { color: #aeb4bb; border-left: 3px solid #3a3f45; padding-left: 10px; margin-left: 0; }
table { border-collapse: collapse; }
td, th { border: 1px solid #444; padding: 4px 8px; }
hr { border: none; border-top: 1px solid #333; }
/* GameBanana 富文本编辑器的具名色类（取自其站点 Generic.css 的 --Default*Color，
   实测 mods/689309 用 <span class="RedColor">）。站点恒为深色底，故明暗主题同值以求一致。 */
.RedColor    { color: #FF4E4E; }
.GreenColor  { color: #6EE16C; }
.BlueColor   { color: #6CB1E1; }
.OrangeColor { color: #FF7238; }
.PurpleColor { color: #FF5E9D; }
.YellowColor { color: #FFE033; }
.GreyColor   { color: #999999; }
.tag-bugfix       { background: #c0392b; color: white; padding: 2px 6px; border-radius: 4px; }
.tag-addition     { background: #27ae60; color: white; padding: 2px 6px; border-radius: 4px; }
.tag-improvement  { background: #2980b9; color: white; padding: 2px 6px; border-radius: 4px; }
.tag-removal      { background: #7f8c8d; color: white; padding: 2px 6px; border-radius: 4px; }
.tag-tweak        { background: #8e44ad; color: white; padding: 2px 6px; border-radius: 4px; }
</style>
"""

_CSS_LIGHT = """
<style>
body {
    color: #2b2f33;
    background: transparent;
    font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: 14px;
    }
p { margin-top: 8px; margin-bottom: 8px; }
a { color: #1565c0; text-decoration: none; }
h1, h2, h3, h4, h5, h6 { color: #14171a; font-weight: bold; margin-top: 14px; margin-bottom: 6px; }
h1 { font-size: 20px; }
h2 { font-size: 17px; }
h3 { font-size: 15px; }
h4, h5, h6 { font-size: 14px; }
ul, ol { margin-top: 6px; margin-bottom: 6px; }
li { margin-top: 2px; margin-bottom: 2px; }
img { max-width: 100%; }
code, pre { background: #f5f5f5; color: #111; padding: 2px 4px; font-family: Consolas, "Courier New", monospace; }
blockquote { color: #56606a; border-left: 3px solid #d0d7de; padding-left: 10px; margin-left: 0; }
table { border-collapse: collapse; }
td, th { border: 1px solid #ccc; padding: 4px 8px; }
hr { border: none; border-top: 1px solid #ddd; }
/* GameBanana 具名色类（同深色主题取值，与网页一致）。 */
.RedColor    { color: #FF4E4E; }
.GreenColor  { color: #6EE16C; }
.BlueColor   { color: #6CB1E1; }
.OrangeColor { color: #FF7238; }
.PurpleColor { color: #FF5E9D; }
.YellowColor { color: #FFE033; }
.GreyColor   { color: #999999; }
.tag-bugfix       { background: #e74c3c; color: white; padding: 2px 6px; border-radius: 4px; }
.tag-addition     { background: #2ecc71; color: white; padding: 2px 6px; border-radius: 4px; }
.tag-improvement  { background: #3498db; color: white; padding: 2px 6px; border-radius: 4px; }
.tag-removal      { background: #95a5a6; color: white; padding: 2px 6px; border-radius: 4px; }
.tag-tweak        { background: #9b59b6; color: white; padding: 2px 6px; border-radius: 4px; }
</style>
"""


def theme_css(theme: Literal["dark", "light"] = "dark") -> str:
    """返回描述区域的 CSS，供 QTextDocument.setDefaultStyleSheet 使用。

    样式与正文分别设置，避免富文本中的内嵌样式影响后续规则解析。
    """
    block = _CSS_DARK if theme == "dark" else _CSS_LIGHT
    return block.replace("<style>", "").replace("</style>", "").strip()


def sanitize_html(html: str, *, theme: Literal["dark", "light"] = "dark") -> str:
    """删除危险内容，按标签、属性和协议白名单清理富文本，再补充链接与兼容颜色表示。"""
    if not html:
        return ""

    body = _DANGEROUS_RE.sub("", html)
    body = _VIDEO_RE.sub(t('<p style="opacity:0.6">[视频内容已移除]</p>'), body)

    cleaned = bleach.clean(
        body,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
        strip_comments=True,
        css_sanitizer=_CSS_SANITIZER,
    )
    cleaned = bleach.linkify(cleaned, skip_tags=["pre", "code"])
    cleaned = _RESTORE_LEGACY_RE.sub(r"<\1>", cleaned)
    cleaned = _normalize_font_colors(cleaned)
    return cleaned
