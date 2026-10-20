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


def render(
    md: str,
    toc: list[tuple[int, str, str]],
    used_slugs: set[str],
    id_prefix: str,
    translations: dict[str, str] | None = None,
) -> str:
    """将教程 Markdown 转换为正文，并记录导航和译文目标。"""
    lines = md.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        if line.startswith("```"):
            lang = line[3:].strip()
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            code = html.escape("\n".join(buf), quote=False)
            cls = f' class="lang-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre{cls}><code>{code}</code></pre>")
            continue

        if re.match(r"^(-{3,}|\*{3,})\s*$", line):
            out.append("<hr>")
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level, text = len(m.group(1)), m.group(2).strip()
            slug = f"{id_prefix}{slugify(text, used_slugs)}"
            if level <= 3:
                toc.append((level, text, slug))
            emo = emote_for(text) if level == 2 else None
            icon = f'<img class="emo" src="{data_uri(emo)}" data-source="{emo}" alt="">' if emo else ""
            translated = f' data-translation-id="{html.escape(translations[slug], quote=True)}"' if translations else ""
            out.append(f'<h{level} id="{slug}"{translated}>{icon}{inline(text, None)}</h{level}>')
            i += 1
            continue

        mi = _IMG.match(line.strip())
        if mi:
            alt, src = mi.group(1), mi.group(2)
            cap = ""
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            mcap = re.match(r"^\*([^*].*)\*$", lines[j].strip()) if j < n else None
            if mcap:
                cap = f"<figcaption>{inline(mcap.group(1), None)}</figcaption>"
                i = j
            # 看板娘立绘不占整行：浮动在正文旁边当点缀。指路是朝右指的，浮左才指向文字
            cls = ""
            if "images/plusa/" in src:
                cls = ' class="mascot left"' if "指路" in src else ' class="mascot right"'
            out.append(
                f'<figure{cls}><img loading="lazy" src="{data_uri(src)}" data-source="{html.escape(src, quote=True)}" '
                f'alt="{html.escape(alt, quote=True)}">{cap}</figure>'
            )
            i += 1
            continue

        if line.lstrip().startswith(">"):
            buf = []
            while i < n and lines[i].lstrip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            body = " ".join(x.strip() for x in buf if x.strip())
            kind = "warn" if body.startswith("⚠️") else ("tip" if body.startswith("💡") else "note")
            out.append(f'<blockquote class="{kind}">{inline(body, None)}</blockquote>')
            continue

        if "|" in line and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1]):
            head = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            th = "".join(f"<th>{inline(c, None)}</th>" for c in head)
            body = "".join("<tr>" + "".join(f"<td>{inline(c, None)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(f"<div class='tw'><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>")
            continue

        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
        if m:
            ordered = m.group(2) not in ("-", "*", "+")
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while i < n:
                mm = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", lines[i])
                if not mm:
                    if lines[i].startswith(("  ", "\t")) and lines[i].strip() and items:
                        items[-1] += " " + inline(lines[i].strip(), None)
                        i += 1
                        continue
                    break
                items.append(inline(mm.group(3), None))
                i += 1
            lis = "".join(f"<li>{x}</li>" for x in items)
            out.append(f"<{tag}>{lis}</{tag}>")
            continue

        buf = []
        while i < n and lines[i].strip() and not re.match(r"^(#{1,6}\s|```|>|\s*([-*+]|\d+[.)])\s)", lines[i]):
            buf.append(lines[i].strip())
            i += 1
        if buf:
            out.append(f"<p>{inline(' '.join(buf), None)}</p>")
    return "\n".join(out)


CSS = """
:root{
  --bg:#191b1d; --bg2:#22262a; --card:#23272b; --line:#33383d;
  --fg:#e8eaed; --fg2:#b6bcc2; --fg3:#8b9299;
  --accent:#4cc2ff; --accent-soft:rgba(76,194,255,.14);
  --warn:#ffb454; --warn-bg:rgba(255,180,84,.10);
  --tip:#6ccb5f; --tip-bg:rgba(108,203,95,.10);
  --scroll-thumb:#5e6d7c; --scroll-hover:#8294a5;
  --theme-duration:280ms; --switch-duration:220ms; --ease:cubic-bezier(.2,.8,.2,1);
  --sidebar:304px; --top-height:60px; --reading-height:38px;
  --sticky-height:calc(var(--top-height) + var(--reading-height));
  color-scheme:dark;
}
html[data-theme=light]{
  --bg:#f4f6f8; --bg2:#fff; --card:#fff; --line:#dfe3e8;
  --fg:#1c2126; --fg2:#4a545e; --fg3:#79838d;
  --accent:#0a84c8; --accent-soft:rgba(10,132,200,.10);
  --warn:#a2620a; --warn-bg:rgba(255,180,84,.16);
  --tip:#2f7a22; --tip-bg:rgba(108,203,95,.16);
  --scroll-thumb:#7b8896; --scroll-hover:#526574;
  color-scheme:light;
}
*{box-sizing:border-box}
html{background:var(--bg);scrollbar-gutter:stable}
/* Firefox 使用标准属性；Chromium / WebKit 使用圆角滑块并隐藏两端箭头。 */
*{scrollbar-width:thin;scrollbar-color:var(--scroll-thumb) var(--bg)}
@supports selector(::-webkit-scrollbar){
  *{scrollbar-width:auto;scrollbar-color:auto}
  ::-webkit-scrollbar{width:10px;height:10px}
  ::-webkit-scrollbar-track{background:var(--bg)}
  ::-webkit-scrollbar-thumb{background:var(--scroll-thumb);background-clip:padding-box;
    border:3px solid transparent;border-radius:999px}
  ::-webkit-scrollbar-thumb:hover{background-color:var(--scroll-hover)}
  ::-webkit-scrollbar-button{display:none;width:0;height:0}
  ::-webkit-scrollbar-corner{background:transparent}
}
html,body,#top,#reading,#side,#cover,#cover p,#cover .said,#top .brand span,#side .grp,#side a,
.btn,#q,#languages,#languages a,#languages span,code,pre,hr,h1,h2,h3,h4,h5,h6,
.tw,th,td,tbody tr,blockquote,figure img,figcaption,h2 .emo,.guide-path,.side-heading{
  transition-property:background-color,color,border-color,box-shadow,scrollbar-color;
  transition-duration:var(--theme-duration);transition-timing-function:var(--ease);
}
::-webkit-scrollbar-thumb,::-webkit-scrollbar-track{
  transition:background-color var(--theme-duration) var(--ease);
}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.85 "Microsoft YaHei","Segoe UI",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
a:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
.skip-link{position:fixed;top:8px;left:18px;z-index:100;padding:8px 16px;
  background:var(--bg2);border:1px solid var(--accent);border-radius:8px;transform:translateY(-150%)}
.skip-link:focus{transform:none}
code{background:var(--accent-soft);color:var(--fg);padding:1px 5px;border-radius:4px;
  font:13px/1.6 Consolas,"Cascadia Mono",monospace;word-break:break-all}
pre{background:var(--bg2);border:1px solid var(--line);border-radius:8px;
  padding:14px 16px;overflow:auto}
pre code{background:none;padding:0;font-size:12.5px;line-height:1.7}
hr{border:0;border-top:1px solid var(--line);margin:38px 0}

#top{position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:14px;
  padding:0 18px;height:var(--top-height);background:var(--bg2);border-bottom:1px solid var(--line)}
#top .brand{flex:1;font-weight:700;font-size:15px;white-space:nowrap;min-width:0;
  overflow:hidden;text-overflow:ellipsis}
#top .brand span{color:var(--accent)}
#top .brand #brand-title{color:var(--fg2);font-weight:500}
#q{flex:0 0 300px;width:300px;min-width:0;height:34px;padding:7px 12px;border-radius:16px;border:1px solid var(--line);
  background:var(--bg);color:var(--fg);outline:none;font-size:13px}
#q:focus{border-color:var(--accent)}
.btn{cursor:pointer;border:1px solid var(--line);background:var(--bg);color:var(--fg2);
  border-radius:14px;padding:6px 12px;font-size:13px;white-space:nowrap}
.btn:hover{border-color:var(--accent);color:var(--fg)}
#theme{flex:0 0 100px;width:100px;height:34px;line-height:20px}
#languages{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-items:center;
  position:relative;isolation:isolate;flex:0 0 136px;width:136px;height:34px;white-space:nowrap;
  padding:3px;border:1px solid var(--line);border-radius:16px;font-size:13px;line-height:1.5}
#languages::before{content:"";position:absolute;z-index:-1;left:3px;top:3px;bottom:3px;
  width:calc((100% - 6px)/2);border-radius:12px;background:var(--accent-soft);
  transform:translateX(0);pointer-events:none;
  transition:transform var(--switch-duration) var(--ease),background-color var(--theme-duration) var(--ease)}
#languages[data-active-language="en-US"]::before{transform:translateX(100%)}
#languages a,#languages span{padding:3px 9px;border-radius:12px;text-align:center;color:var(--fg2)}
#languages a:hover{color:var(--fg);text-decoration:none}
#languages a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
#languages[data-active-language="zh-CN"] [lang="zh-CN"],
#languages[data-active-language="en-US"] [lang="en-US"]{color:var(--accent)}
#burger{display:none}

#wrap{display:flex;align-items:flex-start}
html.initial-language #wrap,html.initial-language #reading{visibility:hidden}
::view-transition-group(root){animation-duration:180ms;animation-timing-function:var(--ease)}
::view-transition-old(root){animation:reader-out 180ms linear both;mix-blend-mode:plus-lighter}
::view-transition-new(root){animation:reader-in 180ms linear both;mix-blend-mode:plus-lighter}
@keyframes reader-out{from{opacity:1}to{opacity:0}}
@keyframes reader-in{from{opacity:0}to{opacity:1}}
#reading{position:sticky;top:var(--top-height);z-index:29;height:var(--reading-height);
  display:flex;align-items:center;gap:14px;padding:0 20px;background:var(--bg);
  border-bottom:1px solid var(--line);font-size:12px;color:var(--fg3)}
#reading-label{flex:0 0 calc(var(--sidebar) - 20px);font-weight:600;letter-spacing:.03em}
#reading-location{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--fg2)}
#reading-percent{flex:0 0 38px;text-align:right;font-variant-numeric:tabular-nums}
#reading-top{flex:0 0 88px;text-align:right;color:var(--fg2);white-space:nowrap}
#reading-top:hover{color:var(--accent)}
#reading-progress{position:absolute;left:var(--sidebar);right:0;bottom:-1px;height:2px;pointer-events:none}
#reading-fill{display:block;height:100%;background:var(--accent);transform:scaleX(0);transform-origin:left}
#side{position:sticky;top:var(--sticky-height);width:var(--sidebar);flex:0 0 var(--sidebar);
  height:calc(100vh - var(--sticky-height));overflow:auto;padding:0 12px 60px 18px;
  border-right:1px solid var(--line);background:var(--bg)}
.side-heading{position:sticky;top:0;z-index:1;display:flex;align-items:center;justify-content:space-between;
  margin:0 0 12px;padding:17px 10px 12px;background:var(--bg);border-bottom:1px solid var(--line);
  font-size:12px;font-weight:600;color:var(--fg2)}
#section-count{font-size:11px;font-weight:400;color:var(--fg3);font-variant-numeric:tabular-nums}
#side .grp{margin:22px 0 7px;padding-left:10px;font-size:11px;font-weight:600;letter-spacing:.06em;color:var(--fg3)}
#side a{display:block;padding:5px 10px;border-radius:6px;color:var(--fg2);font-size:13.5px;
  border-left:2px solid transparent}
#side a:hover{background:var(--accent-soft);text-decoration:none;color:var(--fg)}
#side a.lv2{padding-left:20px} #side a.lv3{padding-left:34px;font-size:13px;color:var(--fg3)}
#side a.on{color:var(--accent);border-left-color:var(--accent);background:var(--accent-soft)}
#side a.hide{display:none}

main{flex:1;min-width:0;padding:30px 44px 120px;max-width:1120px;outline:none}
h1,h2,h3,h4,h5,h6,#cover{scroll-margin-top:calc(var(--sticky-height) + 16px)}
main section>p,main section>ul,main section>ol{max-width:88ch}
h1{font-size:29px;margin:8px 0 18px;padding-bottom:12px;border-bottom:2px solid var(--accent)}
h2{font-size:22px;margin:44px 0 14px;padding-left:12px;border-left:4px solid var(--accent)}
h2 .emo{width:38px;height:38px;border-radius:50%;vertical-align:-11px;margin-right:11px;
  border:1px solid var(--line);background:var(--bg2)}
h3{font-size:17px;margin:30px 0 10px;color:var(--fg)}
h4{font-size:15px;margin:22px 0 8px;color:var(--fg2)}
p{margin:12px 0} ul,ol{margin:12px 0;padding-left:26px} li{margin:5px 0}

.tw{overflow:auto;margin:16px 0;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{border-bottom:1px solid var(--line);padding:9px 12px;text-align:left;vertical-align:top}
th{background:var(--bg2);font-weight:700;white-space:nowrap}
tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--accent-soft)}

blockquote{margin:16px 0;padding:12px 16px;border-radius:8px;border-left:4px solid var(--fg3);
  background:var(--bg2);color:var(--fg2)}
blockquote.warn{border-left-color:var(--warn);background:var(--warn-bg)}
blockquote.tip{border-left-color:var(--tip);background:var(--tip-bg)}

figure{margin:22px 0;text-align:center}
figure img{max-width:100%;border:1px solid var(--line);border-radius:10px;cursor:zoom-in;
  box-shadow:0 6px 24px rgba(0,0,0,.28);background:var(--bg2)}
figcaption{margin-top:8px;font-size:12.5px;color:var(--fg3)}

/* 看板娘立绘：浮动点缀，不占整行，也不加卡片边框 */
figure.mascot{margin:4px 0 10px;width:min(270px,32%)}
figure.mascot img{border:0;box-shadow:none;background:none;border-radius:0}
figure.mascot.right{float:right;margin-left:30px}
figure.mascot.left{float:left;margin-right:30px}
figure.mascot figcaption{font-size:12px}
h2,h3,hr{clear:both}
@media (max-width:900px){
  figure.mascot{float:none!important;width:62%;margin:14px auto}
}

#cover{display:block;margin:6px 0 34px;padding:0;overflow:hidden;
  border:1px solid var(--line);border-radius:14px;
  background-color:var(--bg2);background-image:linear-gradient(135deg,rgba(255,255,255,.025),rgba(0,0,0,.025))}
#cover img{display:block;width:100%;height:auto;max-height:400px;
  object-fit:cover;object-position:center 26%;border:0;border-radius:0;cursor:zoom-in}
#cover .t{padding:22px 30px 26px}
#cover h1{border:0;margin:0 0 10px;padding:0;font-size:31px}
#cover p{margin:6px 0;color:var(--fg2)}
#cover .said{margin-top:14px;padding:10px 14px;border-left:3px solid var(--accent);
  background:var(--accent-soft);border-radius:0 8px 8px 0;color:var(--fg)}
#avatar{width:32px;height:32px;border-radius:50%;flex:0 0 auto}
.guide-paths{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:0 0 46px}
.guide-path{position:relative;display:flex;flex-direction:column;gap:7px;padding:18px;
  border:1px solid var(--line);border-radius:12px;background:var(--bg2);color:var(--fg)}
.guide-path:hover{border-color:var(--accent);background:var(--accent-soft);text-decoration:none}
.guide-path .path-number{font:11px/1.5 Consolas,monospace;letter-spacing:.08em;color:var(--accent)}
.guide-path strong{font-size:14px;line-height:1.6;font-weight:600}
.guide-path .path-description{font-size:12px;line-height:1.8;color:var(--fg2)}
.guide-path .path-arrow{position:absolute;top:14px;right:17px;color:var(--fg3)}

#lb{position:fixed;inset:0;z-index:99;display:none;grid-template-rows:auto minmax(0,1fr) auto;
  background:rgba(0,0,0,.94);padding:12px;gap:10px;color:#e8eaed}
#lb.on{display:grid}
#lb-tools{display:flex;justify-content:center;gap:8px;flex-wrap:wrap}
#lb-tools .btn{background:#22262a;color:#e8eaed;border-color:#66717b}
#lb-stage{min-height:0;overflow:auto;display:grid;place-items:center;overscroll-behavior:contain}
#lb-stage img{display:block;max-width:100%;max-height:100%;border-radius:6px;cursor:zoom-in}
#lb.native #lb-stage{display:block}
#lb.native img{max-width:none;max-height:none;margin:0 auto;cursor:zoom-out}
#lb .hint{text-align:center;color:#b6bcc2;font-size:12px}

@media (max-width:900px){
  #burger{display:block}
  #side{position:fixed;left:0;top:var(--sticky-height);z-index:25;transform:translateX(-100%);
    transition:transform .18s var(--ease),background-color var(--theme-duration) var(--ease),
      color var(--theme-duration) var(--ease),border-color var(--theme-duration) var(--ease);
    box-shadow:0 0 40px rgba(0,0,0,.5);width:86vw}
  #side.open{transform:none}
  main{padding:18px 16px 100px}
  #q{width:40vw}
}
@media (max-width:900px){
  :root{--top-height:96px}
  #top{display:grid;grid-template-columns:auto minmax(0,1fr) auto auto;
    grid-template-rows:40px 32px;gap:8px;padding:8px 12px}
  #avatar{display:none}
  #burger{grid-column:1;grid-row:1}
  #top .brand{grid-column:2 / 4;grid-row:1;min-width:0;overflow:hidden;
    text-overflow:ellipsis;font-size:13px}
  #theme{grid-column:4;grid-row:1}
  #q{grid-column:1 / 3;grid-row:2;width:100%;min-width:0}
  #languages{grid-column:3 / 5;grid-row:2;justify-self:end}
  #reading{padding:0 14px;gap:10px}
  #reading-label{display:none}
  #reading-progress{left:0}
  #cover .t{padding:18px}
  #cover h1{font-size:26px;overflow-wrap:anywhere}
  .guide-paths{gap:9px}
  .guide-path{padding:14px}
}
@media (max-width:600px){
  .guide-paths{grid-template-columns:1fr;margin-bottom:32px}
  .guide-path{gap:3px;padding:14px 18px}
  .guide-path .path-number{position:absolute;top:16px;right:40px}
}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation:none!important;transition:none!important}
}
@media print{
  #top,#reading,#side,#lb,.skip-link{display:none!important}
  #wrap{animation:none!important;opacity:1!important}
  main{max-width:none;padding:0}
  body{background:#fff;color:#000}
  figure img{box-shadow:none}
}
"""

JS = Path(__file__).with_name("reader.js").read_text(encoding="utf-8")

SHELL = """<!DOCTYPE html>
<html lang="__LANG__" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>d3dxSkinManage-Plus __TITLE__</title>
<script>
(function(){try{
  var saved=localStorage.getItem('d3dx-tut-theme');
  if(saved==='light'||saved==='dark') document.documentElement.setAttribute('data-theme',saved);
}catch(e){}})();
(function(){
  var requested=new URL(location.href).searchParams.get('lang');
  if((requested==='zh-CN'||requested==='en-US')&&requested!==document.documentElement.lang)
    document.documentElement.classList.add('initial-language');
})();
</script>
<style>__CSS__</style>
<noscript><style>html.initial-language #wrap,html.initial-language #reading{visibility:visible}</style></noscript>
</head>
<body>
<a id="skip-link" class="skip-link" href="#main">__SKIP__</a>
<div id="top">
  <button class="btn" id="burger" aria-label="__CONTENTS__" aria-controls="side" aria-expanded="false">☰</button>
  <img id="avatar" src="__AVATAR__" data-source="images/plusa/00-头像.webp" alt="__MASCOT__">
  <div class="brand">d3dxSkinManage<span>-Plus</span> · <span id="brand-title">__TITLE__</span></div>
  <input id="q" placeholder="__FILTER__" aria-label="__FILTER__">
  <nav id="languages" aria-label="__LANGUAGE__" data-active-language="__LANG__">__LANGUAGES__</nav>
  <button class="btn" id="theme">__THEME__</button>
</div>
<div id="reading">
  <span id="reading-label">__READING__</span>
  <span id="reading-location">__START__</span>
  <span id="reading-percent">0%</span>
  <a id="reading-top" href="#cover">__TOP__</a>
  <div id="reading-progress" role="progressbar" aria-label="__PROGRESS__"
       aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><span id="reading-fill"></span></div>
</div>
<div id="wrap">
  <nav id="side" aria-label="__CONTENTS__">
    <div class="side-heading"><span id="side-label">__CONTENTS__</span><span id="section-count"></span></div>
    <div id="toc">__TOC__</div>
  </nav>
  <main id="main" tabindex="-1">__MAIN__</main>
</div>
<div id="lb" role="dialog" aria-modal="true" aria-hidden="true" aria-label="__VIEWER__">
  <div id="lb-tools">
    <button class="btn" id="lb-fit">__FIT__</button>
    <button class="btn" id="lb-native" aria-pressed="false">__ORIGINAL__</button>
    <button class="btn" id="lb-close">__CLOSE__</button>
  </div>
  <div id="lb-stage"><img alt=""></div>
  <div class="hint">__HINT__</div>
</div>
<script id="reader-data" type="application/json">__READER_DATA__</script>
<script>__JS__</script>
</body>
</html>
"""

COVER = """<header id="cover">
  <img src="__PORTRAIT__" data-source="__PORTRAIT_SOURCE__" alt="看板娘 普拉莎">
  <div class="t">
    <h1>d3dxSkinManage-Plus 使用教程</h1>
    <p>从第一个 Mod 开始，逐步整理自己的资源库。按图中编号操作，点击图片可放大查看。</p>
    <p>先读《5 分钟快速上手》，再按需要查阅八章《完整功能手册》。想扩展功能，继续阅读
       <a href="#插件开发指南">《插件开发指南》</a>。左侧目录可跳转和筛选，正文可用 Ctrl+F 搜索。</p>
    <div class="said">「柜子是空的。先添一个游戏吧。」 —— 普拉莎</div>
  </div>
</header>
"""

PLUGIN_COVER = """<header id="cover">
  <img src="__PORTRAIT__" data-source="__PORTRAIT_SOURCE__" alt="看板娘 普拉莎">
  <div class="t">
    <h1>d3dxSkinManage-Plus 插件开发指南</h1>
    <p>从一个可运行的插件开始，逐步接入 Mod 操作、命令、菜单、设置和自定义页面。</p>
    <p>包含完整示例、接口与权限说明、异步任务、调试和打包步骤。左侧目录可筛选，代码可直接选择复制。</p>
    <div class="said">「先把第一个小工具做好，再添上你需要的功能。」 —— 普拉莎</div>
  </div>
</header>
"""


EN_COVER = """<header id="cover">
  <img src="__PORTRAIT__" data-source="__PORTRAIT_SOURCE__" alt="Plusa, the project mascot">
  <div class="t">
    <h1>d3dxSkinManage-Plus User Guide</h1>
    <p>Start with your first Mod, then build and organize your library. Follow the numbered screenshots;
       click any image and choose Original size to see the details.</p>
    <p>Begin with the 5-minute quick start, then explore the eight-chapter manual as needed.
       To extend the application, read the <a href="#plugin-development-guide">Plugin development guide</a>.
       Use the contents to navigate or filter headings, and Ctrl+F to search the page.</p>
    <div class="said">“The shelves are empty. Let's add a game first.” — Plusa</div>
  </div>
</header>
"""

EN_PLUGIN_COVER = """<header id="cover">
  <img src="__PORTRAIT__" data-source="__PORTRAIT_SOURCE__" alt="Plusa, the project mascot">
  <div class="t">
    <h1>d3dxSkinManage-Plus Plugin Development Guide</h1>
    <p>Build a working plugin, then add Mod operations, commands, menus, settings and custom pages.</p>
    <p>Includes complete examples, APIs and permissions, asynchronous tasks, debugging and packaging.
       Filter the contents to find a topic; select and copy code directly from the examples.</p>
    <div class="said">“Make your first small tool work, then add the features you need.” — Plusa</div>
  </div>
</header>
"""


def render_edition(
    docs: list[tuple[str, str]],
    *,
    translated_docs: list[tuple[str, str]],
    developer: bool = False,
    language: str = "zh-CN",
) -> dict:
    """生成单种语言的正文、目录及阅读入口。"""
    translations = translation_ids(docs, translated_docs)
    used: set[str] = set()
    toc: list[tuple[int, str, str]] = []
    body_parts: list[str] = []
    groups: list[tuple[str, int]] = []
    for title, md in docs:
        groups.append((title, len(toc)))
        body_parts.append(f"<section>{render(md, toc, used, '', translations)}</section>")

    nav: list[str] = []
    bounds = [start for _, start in groups] + [len(toc)]
    for gi, (gtitle, _) in enumerate(groups):
        nav.append(f'<div class="grp">{html.escape(gtitle)}</div>')
        for level, text, slug in toc[bounds[gi] : bounds[gi + 1]]:
            nav.append(f'<a class="lv{level}" href="#{slug}">{html.escape(text)}</a>')
    # 封面优先用横幅；没有横幅就退回竖版立绘，避免缺图时整块封面变碎图
    banner = TUT / "images" / "plusa" / "封面横幅.webp"
    portrait = "images/plusa/封面横幅.webp" if banner.is_file() else "images/plusa/立绘.webp"
    english = language == "en-US"
    cover = (EN_PLUGIN_COVER if developer else EN_COVER) if english else (PLUGIN_COVER if developer else COVER)
    cover = cover.replace("__PORTRAIT__", data_uri(portrait)).replace("__PORTRAIT_SOURCE__", portrait)
    labels = {
        "LANG": language,
        "TITLE": ("Plugin Development Guide" if developer else "User Guide")
        if english
        else ("插件开发指南" if developer else "使用教程"),
        "MASCOT": "Plusa" if english else "普拉莎",
        "CONTENTS": "Contents" if english else "目录",
        "FILTER": "Filter contents" if english else "筛选目录",
        "LANGUAGE": "Language" if english else "语言",
        "THEME": "Dark / Light" if english else "深 / 浅",
        "SKIP": "Skip to content" if english else "跳转到正文",
        "READING": "READING GUIDE" if english else "阅读指南",
        "START": "Choose where to start" if english else "从这里开始",
        "TOP": "Back to top ↑" if english else "回到顶部 ↑",
        "PROGRESS": "Reading progress" if english else "阅读进度",
        "VIEWER": "Image viewer" if english else "图片查看器",
        "FIT": "Fit to window" if english else "适应窗口",
        "ORIGINAL": "Original size" if english else "原始尺寸",
        "CLOSE": "Close" if english else "关闭",
        "HINT": "Click the image to switch size; scroll to inspect details. Press Esc to close."
        if english
        else "点击图片切换尺寸，滚动查看细节；按 Esc 关闭。",
    }

    if developer:
        targets = [
            next(slug for level, text, slug in toc if level == 2 and text.startswith(f"{n}.")) for n in (1, 2, 8)
        ]
        paths = (
            [
                ("Build your first plugin", "A working example, from setup to loading."),
                ("Explore the API", "Find the interfaces and events you need."),
                ("Package and share", "Prepare your plugin for installation."),
            ]
            if english
            else [
                ("写出第一个插件", "从开发环境到运行示例，完成第一次加载。"),
                ("查阅接口", "按需找到操作任务、事件和扩展能力。"),
                ("打包与分发", "整理发布文件，让插件可以安装和更新。"),
            ]
        )
    else:
        targets = [toc[start][2] for _, start in groups]
        paths = (
            [
                ("5-minute quick start", "Get your first Mod up and running."),
                ("Complete user manual", "Find detailed steps for each feature."),
                ("Plugin development", "Build tools that fit your workflow."),
            ]
            if english
            else [
                ("5 分钟快速上手", "从添加游戏开始，启用你的第一个 Mod。"),
                ("完整功能手册", "按功能查阅操作步骤、设置和常见问题。"),
                ("插件开发指南", "从一个小工具开始，扩展自己的工作流。"),
            ]
        )
    cards = []
    for index, (target, (title, description)) in enumerate(zip(targets, paths, strict=True), 1):
        cards.append(
            f'<a class="guide-path" href="#{target}"><span class="path-number">0{index}</span>'
            f'<span class="path-arrow" aria-hidden="true">↗</span><strong>{html.escape(title)}</strong>'
            f'<span class="path-description">{html.escape(description)}</span></a>'
        )
    shortcuts = f'<nav class="guide-paths" aria-label="{labels["START"]}">' + "".join(cards) + "</nav>"
    filename = (
        (EN_OUT_PLUGIN_HTML if developer else EN_OUT_HTML) if english else (OUT_PLUGIN_HTML if developer else OUT_HTML)
    )
    return {
        "file": filename.name,
        "labels": labels,
        "mainHTML": embed_source_references(cover + shortcuts + "\n<hr>\n".join(body_parts), language=language),
        "tocHTML": "\n".join(nav),
    }
