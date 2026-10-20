"""把 Markdown 教程编译成**单文件 HTML**（图片 base64 内嵌，双击即开）。

每份文档的图片、样式和脚本均内嵌，可以独立离线阅读。
每份文件内置对应译文，语言切换保留当前页面和阅读位置。

同时负责把 `chapters/zh/` 和 `chapters/en/` 中的章节拼成完整手册。

用法::

    .venv/Scripts/python.exe scripts/tutorials/build.py

依赖：只用标准库 + 一个极小的自写 Markdown 子集渲染器（避免为教程引入新依赖）。
支持：ATX 标题、段落、无序/有序列表、表格、围栏代码块、引用块、图片、行内
``**粗体**`` / ``*斜体*`` / ``` `代码` ``` / 链接。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import mimetypes
import os
import re
import sys
from functools import cache
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

TUT = Path(__file__).resolve().parents[2] / "docs" / "tutorials"
IMAGES = TUT / "images"
DRAFT = TUT / "chapters"
DEFAULT_OUTPUT = TUT.parent.parent / "build" / "tutorials"
QUICK = TUT / "zh/5分钟快速上手.md"
MANUAL = TUT / "zh/完整功能手册.md"
OUT_HTML = TUT / "d3dxSkinManage-Plus 使用教程.html"
PLUGIN_GUIDE = TUT / "zh/插件开发指南.md"
OUT_PLUGIN_HTML = TUT / "d3dxSkinManage-Plus 插件开发指南.html"
EN_QUICK = TUT / "en/Quickstart.en.md"
EN_MANUAL = TUT / "en/Manual.en.md"
EN_PLUGIN_GUIDE = TUT / "en/PluginGuide.en.md"
EN_OUT_HTML = TUT / "d3dxSkinManage-Plus User Guide.html"
EN_OUT_PLUGIN_HTML = TUT / "d3dxSkinManage-Plus Plugin Development Guide.html"

EN_CHAPTER_ORDER = [
    "ch1-interface-and-navigation.md",
    "ch2-games-and-setup.md",
    "ch3-categories-and-mods.md",
    "ch4-presets-deployment-and-keys.md",
    "ch5-online-mods-and-updates.md",
    "ch6-settings.md",
    "ch7-tools-plugins-and-about.md",
    "ch8-data-backup-and-troubleshooting.md",
]

CHAPTER_ORDER = [
    "ch1-界面与导航.md",
    "ch2-游戏与首次配置.md",
    "ch3-类别总览与Mod管理.md",
    "ch4-预设部署与按键.md",
    "ch5-在线Mod与更新.md",
    "ch6-设置详解.md",
    "ch7-工具箱插件关于.md",
    "ch8-数据备份与排障.md",
]

MANUAL_HEAD = """# 完整功能手册

只想先装好一个 Mod，请从《5 分钟快速上手》开始；
本手册按功能提供详细步骤、设置说明和数据维护方法。

界面入口以简体中文标注。所有软件截图中的游戏、分类、Mod 与预览图均为占位数据，
不包含任何具体游戏的资源；正文外的普拉莎插画为本项目原创看板娘。

快捷键表列出默认绑定，自定义后的操作以「设置 → 快捷键」为准。
配图中的编号对应图下的操作说明，可点击图片放大查看。希望扩展程序功能时，继续阅读《插件开发指南》。
"""

EN_MANUAL_HEAD = """# Complete user manual

To get your first Mod working, begin with the 5-minute quick start.
This manual provides detailed steps, settings and data-maintenance guidance by feature.

Screenshots show the English interface with fictional games, categories, Mods and geometric preview placeholders.
Some sample library names, such as 外观 (Appearance), remain in Chinese because they are user-entered data.
Plusa is the project's original mascot.

The shortcut table lists default bindings. If you customize them, follow Settings → Shortcuts instead.
Numbers in the screenshots match the explanations below them.
Click an image, then choose Original size to inspect its details.
For application extensions, continue to the Plugin development guide.
"""

#: 章标题里出现哪个关键字 → 用哪张普拉莎表情当小图标（看板娘轻量点缀）
CHAPTER_EMOTE = {
    "第 1 步": "01-自信微笑",
    "第 2 步": "10-眨眼竖拇指",
    "第 3 步": "09-思考",
    "第 4 步": "01-自信微笑",
    "第 5 步": "08-星星眼",
    "第 6 步": "10-眨眼竖拇指",
    "开始之前": "09-思考",
    "没生效": "03-无语滴汗",
    "接下来看什么": "06-坏笑",
    "第 1 章": "01-自信微笑",
    "第 2 章": "10-眨眼竖拇指",
    "第 3 章": "08-星星眼",
    "第 4 章": "09-思考",
    "第 5 章": "08-星星眼",
    "第 6 章": "06-坏笑",
    "第 7 章": "11-害羞",
    "第 8 章": "03-无语滴汗",
    "附录 A": "10-眨眼竖拇指",
    "附录 B": "02-惊讶",
}

CHAPTER_EMOTE.update({f"Chapter {i}": CHAPTER_EMOTE[f"第 {i} 章"] for i in range(1, 9)})
CHAPTER_EMOTE.update({f"Step {i}": CHAPTER_EMOTE[f"第 {i} 步"] for i in range(1, 7)})
CHAPTER_EMOTE.update({"Appendix A": "10-眨眼竖拇指", "Appendix B": "02-惊讶"})
CHAPTER_EMOTE.update({"Before you start": "09-思考", "No effect?": "03-无语滴汗", "What to read next": "06-坏笑"})


def emote_for(text: str) -> str | None:
    """按章节标题选择用于导航提示的插画。"""
    for key, name in CHAPTER_EMOTE.items():
        if key in text:
            path = TUT / "images" / "plusa" / "face" / f"{name}.webp"
            return f"images/plusa/face/{name}.webp" if path.is_file() else None
    return None


def assemble_manual(language: str = "zh-CN") -> str:
    """按章节顺序拼装手册，将链接换算为完整手册所在目录的相对路径。"""
    english = language == "en-US"
    parts = [(EN_MANUAL_HEAD if english else MANUAL_HEAD).rstrip()]
    chapter_order = EN_CHAPTER_ORDER if english else CHAPTER_ORDER
    draft = DRAFT / ("en" if english else "zh")
    destination = EN_MANUAL if english else MANUAL
    for name in chapter_order:
        path = draft / name
        if not path.is_file():
            raise FileNotFoundError(f"缺少章节：{path}")
        parts.append(rebase_markdown_links(path.read_text(encoding="utf-8"), path, destination.parent).strip())
    text = "\n\n---\n\n".join(parts) + "\n"
    destination.write_text(text, encoding="utf-8")
    print(f"  [拼装] {destination.name}  {len(text)} 字符")
    return text


def rebase_markdown_links(text: str, source: Path, destination: Path | None = None) -> str:
    """按目标文档目录换算本地链接；渲染 HTML 时统一使用教程素材根目录。"""
    destination = destination or TUT

    def replace(match: re.Match[str]) -> str:
        """仅换算本地文档和图片路径，保留可选的页内锚点。"""
        target = match[2].strip("<>")
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            return match[0]
        path = (source.parent / unquote(parsed.path)).resolve()
        relative = Path(os.path.relpath(path, destination)).as_posix()
        if parsed.fragment:
            relative += f"#{parsed.fragment}"
        return f"{match[1]}{relative}{match[3]}"

    lines, fenced = [], False
    for line in text.splitlines(keepends=True):
        if line.strip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            line = re.sub(r"(!?\[[^\]]*\]\()([^)]+)(\))", replace, line)
        lines.append(line)
    return "".join(lines)


def embed_source_references(body: str, *, language: str) -> str:
    """将正文引用的仓库文本内嵌为源码附录，使离线指南无需旁带源码目录。"""
    repository = TUT.parent.parent.resolve()
    references: dict[Path, str] = {}

    def replace(match: re.Match[str]) -> str:
        """把仓库内可读文本的链接指向去重后的附录，并拒绝目录越界。"""
        target = html.unescape(match[1])
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            return match[0]
        source = (TUT / unquote(parsed.path)).resolve()
        if not source.is_relative_to(repository) or source.suffix not in {".md", ".py", ".json"}:
            raise ValueError(f"源码引用必须是仓库内的 Markdown、Python 或 JSON 文件：{target}")
        if not source.is_file():
            raise FileNotFoundError(f"源码引用不存在：{source}")
        relative = source.relative_to(repository).as_posix()
        anchor = "source-" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
        references[source] = anchor
        return f'<a href="#{anchor}">{match[3]}</a>'

    body = re.sub(r'<a href="([^"]+)"([^>]*)>(.*?)</a>', replace, body, flags=re.DOTALL)
    if not references:
        return body
    label = "Referenced source files" if language == "en-US" else "引用的源码文件"
    parts = [f'<section class="source-references" aria-label="{label}"><p><strong>{label}</strong></p>']
    for source, anchor in references.items():
        relative = html.escape(source.relative_to(repository).as_posix())
        contents = html.escape(source.read_text(encoding="utf-8"), quote=False)
        parts.append(
            f'<details class="source-file" id="{anchor}"><summary>{relative}</summary>'
            f"<pre><code>{contents}</code></pre></details>"
        )
    return body + "\n" + "\n".join(parts) + "</section>"


_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_IMG = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$")

DOC_ANCHORS = {
    "zh/插件开发指南.md": "#插件开发指南",
    "zh/5分钟快速上手.md": "#5-分钟快速上手",
    "zh/完整功能手册.md": "#完整功能手册",
    "en/Quickstart.en.md": "#5-minute-quick-start",
    "en/Manual.en.md": "#complete-user-manual",
    "en/PluginGuide.en.md": "#plugin-development-guide",
}


def inline(text: str, embed) -> str:
    """转义正文并渲染行内代码、强调及链接。"""
    out = html.escape(text, quote=False)
    holes: list[str] = []

    def stash(rendered: str) -> str:
        """暂存已渲染片段，避免后续替换重复转义。"""
        holes.append(rendered)
        return f"\x00{len(holes) - 1}\x00"

    def link(match) -> str:
        """将教程内文档链接转换为当前页面锚点。"""
        href = html.unescape(match.group(2)).strip("<>")
        href = DOC_ANCHORS.get(href, href)
        target = "" if href.startswith("#") else ' target="_blank" rel="noopener"'
        return stash(f'<a href="{html.escape(href, quote=True)}"{target}>{match.group(1)}</a>')

    out = _INLINE_CODE.sub(lambda m: stash(f"<code>{m.group(1)}</code>"), out)
    out = _BOLD.sub(lambda m: stash(f"<strong>{m.group(1)}</strong>"), out)
    out = _ITALIC.sub(lambda m: stash(f"<em>{m.group(1)}</em>"), out)
    out = _LINK.sub(link, out)
    out = re.sub(r"\\n", "<br>", out)
    # 外层样式可能包含先保存的代码片段，先还原外层再展开内层。
    for i in range(len(holes) - 1, -1, -1):
        out = out.replace(f"\x00{i}\x00", holes[i])
    return out


def slugify(text: str, used: set[str]) -> str:
    """生成可分享且在文档内唯一的小节标识。"""
    base = re.sub(r"[^\w一-鿿]+", "-", text).strip("-").lower() or "sec"
    slug, n = base, 2
    while slug in used:
        slug, n = f"{base}-{n}", n + 1
    used.add(slug)
    return slug


def translation_ids(docs: list[tuple[str, str]], translated_docs: list[tuple[str, str]]) -> dict[str, str]:
    """按各篇文档的标题结构配对，保留两种语言各自可分享的原有锚点。"""

    def outlines(documents):
        """按正文顺序读取标题，跳过代码示例。"""
        used: set[str] = set()
        result = []
        for _, md in documents:
            headings, fenced = [], False
            for line in md.splitlines():
                if line.startswith("```"):
                    fenced = not fenced
                elif not fenced and (match := re.match(r"^(#{1,6})\s+(.*)$", line)):
                    level, title = len(match[1]), match[2].strip()
                    headings.append((level, title, slugify(title, used)))
            result.append(headings)
        return result

    if len(docs) != len(translated_docs):
        raise ValueError("中英文文档数量不同，无法配对语言切换目标。")
    pairs = {}
    number_pattern = r"^(?:(?:第|Chapter|Step)\s*)?(\d+(?:\.\d+)*)(?=[\s.:：])"
    for (title, _), source, target in zip(docs, outlines(docs), outlines(translated_docs), strict=True):
        if [item[0] for item in source] != [item[0] for item in target]:
            raise ValueError(f"{title} 中英文标题数量或层级不同，无法配对语言切换目标。")
        for (level, text, slug), (_, translated_text, translated_slug) in zip(source, target, strict=True):
            if level > 1:
                numbers = [
                    match[1] if (match := re.match(number_pattern, value)) else None
                    for value in (text, translated_text)
                ]
                if numbers[0] != numbers[1]:
                    raise ValueError(f"{title} 中英文小节编号不一致：{text} / {translated_text}")
            pairs[slug] = translated_slug
    return pairs


#: 内嵌统计：{"alpha": 带透明通道的图数, "flat": 不透明图数}，构建结束时打印，
#: 让「alpha 又被烤掉」这类回归在构建输出里一眼可见
EMBED_STATS = {"alpha": 0, "flat": 0}


@cache
def data_uri(rel: str) -> str:
    """保留原始尺寸与 RGBA 像素，使用更小的无损 WebP 或原文件字节内嵌。

    缩放仅由浏览器排版完成；放大查看时仍使用完整原图。相同来源只编码一次。
    """
    path = (TUT / rel).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"图片不存在：{rel}")
    raw, mime = path.read_bytes(), mimetypes.guess_type(path.name)[0] or "image/png"
    try:
        import io

        from PIL import Image

        with Image.open(path) as im:
            has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
            buf = io.BytesIO()
            im.convert("RGBA" if has_alpha else "RGB").save(buf, "WEBP", lossless=True, exact=True, method=4)
            EMBED_STATS["alpha" if has_alpha else "flat"] += 1
        if buf.tell() < len(raw):
            raw, mime = buf.getvalue(), "image/webp"
    except Exception as e:
        print(f"  ! {rel} 转 WebP 失败，按原图内嵌：{e}", file=sys.stderr)
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
