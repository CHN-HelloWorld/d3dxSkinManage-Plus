"""检查教程源文件、图片、引用及离线 HTML 的完整性，不启动应用。"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import io
import json
import os
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from demo_common import REPO, TUTORIAL
from PIL import Image, ImageChops
from verify_language_navigation import verify_language_navigation


def markdown_lines(value: str) -> list[str]:
    """只返回围栏代码以外的正文，避免把插件示例当作标题或图片。"""
    lines = []
    fenced = False
    for line in value.splitlines():
        if line.strip().startswith(chr(96) * 3):
            fenced = not fenced
        elif not fenced:
            lines.append(line)
    return lines


def read_markdown_at_root(path: Path) -> str:
    """以教程素材根目录比较各来源文档的链接，不依赖生成器的路径处理。"""

    def normalize(match: re.Match[str]) -> str:
        """保留外部地址与锚点，将本地路径换算为根目录下的对应位置。"""
        parsed = urlsplit(match[2].strip("<>"))
        if parsed.scheme or parsed.netloc or not parsed.path:
            return match[0]
        target = (path.parent / unquote(parsed.path)).resolve()
        relative = Path(os.path.relpath(target, TUTORIAL)).as_posix()
        if parsed.fragment:
            relative += "#" + parsed.fragment
        return match[1] + relative + match[3]

    lines, fenced = [], False
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        if line.strip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            line = re.sub(r"(!?\[[^\]]*\]\()([^)]+)(\))", normalize, line)
        lines.append(line)
    return "".join(lines)


def markdown_headings(value: str) -> list[tuple[int, str]]:
    """读取实际正文的标题层级和显示文字，不执行 HTML 构建器。"""
    headings = []
    for line in markdown_lines(value):
        match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if match:
            parts = re.split(r"(`[^`]+`)", match[2])
            for index, part in enumerate(parts):
                if part.startswith("`") and part.endswith("`"):
                    parts[index] = part[1:-1]
                else:
                    parts[index] = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", part).replace("*", "")
            headings.append((len(match[1]), " ".join("".join(parts).split())))
    return headings


def markdown_images(value: str) -> list[str]:
    """保留正文图片顺序和重复次数，以核对完整译文及 HTML 内容。"""
    prose = "\n".join(markdown_lines(value))
    return [unquote(target.strip("<>")).replace("\\", "/") for target in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", prose)]


def source_image_path(reference: str, context: str) -> Path:
    """data-source 必须指向教程目录内实际存在的原始 PNG。"""
    relative = PurePosixPath(unquote(reference).replace("\\", "/"))
    assert (
        relative.parts
        and relative.parts[0] == "images"
        and not relative.is_absolute()
        and ".." not in relative.parts
        and relative.suffix.lower() == ".webp"
    ), f"{context} 图片来源不是教程内的 PNG：{reference}"
    path = (TUTORIAL / Path(*relative.parts)).resolve()
    assert path.is_relative_to((TUTORIAL / "images").resolve()) and path.is_file(), (
        f"{context} 图片来源不存在或越出教程目录：{reference}"
    )
    return path


class TutorialHTML(HTMLParser):
    """收集文档标识、链接与图片，检查是否存在外部依赖。"""

    def __init__(self):
        """初始化解析结果容器。"""
        super().__init__(convert_charrefs=True)
        self.ids = []
        self.hrefs = []
        self.images = []
        self.figure_sources = []
        self.dependencies = []
        self.languages = []
        self.headings = []
        self.language_href = None
        self.empty_lightbox_images = 0
        self._ancestors = []
        self._heading = None

    def handle_starttag(self, tag, attrs):
        """记录导航与资源引用。"""
        attributes = dict(attrs)
        if tag == "html":
            self.languages.append(attributes.get("lang"))
        if attributes.get("id"):
            self.ids.append(attributes["id"])
        if tag == "a" and attributes.get("href"):
            self.hrefs.append(attributes["href"])
            if attributes.get("id") == "language-link":
                self.language_href = attributes["href"]
        if tag == "img":
            if not attributes.get("src") and any(item[1] == "lb-stage" for item in self._ancestors):
                self.empty_lightbox_images += 1
            else:
                self.images.append(attributes)
                if any(item[0] == "figure" for item in self._ancestors):
                    self.figure_sources.append(attributes.get("data-source"))
        if tag in {"script", "iframe", "video", "audio", "source"} and attributes.get("src"):
            self.dependencies.append(attributes["src"])
        if tag == "link" and attributes.get("href"):
            self.dependencies.append(attributes["href"])
        if attributes.get("srcset"):
            self.dependencies.append(attributes["srcset"])
        if re.fullmatch(r"h[1-6]", tag) and attributes.get("id"):
            self._heading = (int(tag[1]), [])
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self._ancestors.append((tag, attributes.get("id")))

    def handle_data(self, data):
        """收集正文标题的实际显示文字，忽略封面的无锚点标题。"""
        if self._heading is not None:
            self._heading[1].append(data)

    def handle_endtag(self, tag):
        """结束标题及祖先范围，识别唯一允许为空的灯箱图片。"""
        if self._heading is not None and tag == f"h{self._heading[0]}":
            self.headings.append((self._heading[0], " ".join("".join(self._heading[1]).split())))
            self._heading = None
        for index in range(len(self._ancestors) - 1, -1, -1):
            if self._ancestors[index][0] == tag:
                del self._ancestors[index:]
                break

    def handle_startendtag(self, tag, attrs):
        """自闭合标签使用相同的资源检查规则。"""
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)


def verify_html(path, *, language="zh-CN", markdown_texts=None, verified_images=None) -> TutorialHTML:
    """四份 HTML 均须独立可读，内嵌图片与来源尺寸及 RGBA 像素完全一致。"""
    document = path.read_text(encoding="utf-8")
    assert "\x00" not in document, f"{path.name} 含未还原的行内 Markdown 占位符。"
    assert "\ufffd" not in document, f"{path.name} 编码异常。"
    assert not re.search(
        r"__(?:CSS|JS|AVATAR|COVER|TOC|BODY|PORTRAIT|PORTRAIT_SOURCE|LANG|LANGUAGE|LANGUAGES|TITLE|MASCOT|CONTENTS|FILTER|THEME|VIEWER|FIT|ORIGINAL|CLOSE|HINT)__",
        document,
    ), f"{path.name} HTML 模板未完整替换。"
    for phrase in ("适用：最终版", "最终版功能教程", "核对日期", "按 2026-10-30", "本教程按 "):
        assert phrase not in document, f"{path.name} 残留内部维护措辞：{phrase}"
    parser = TutorialHTML()
    parser.feed(document)
    parser.close()
    assert parser.languages == [language], f"{path.name} HTML 语言错误：{parser.languages}，应为 {language}"
    repeated = [key for key, count in Counter(parser.ids).items() if count > 1]
    assert not repeated, f"{path.name} 标识重复：{repeated}"
    ids = set(parser.ids)
    plugin_anchor = "plugin-development-guide" if language == "en-US" else "插件开发指南"
    assert plugin_anchor in ids, f"{path.name} 未包含对应语言的插件指南锚点：{plugin_anchor}"
    for href in parser.hrefs:
        if href.startswith("#"):
            assert unquote(href[1:]) in ids, f"{path.name} 链接失效：{href}"
        elif href == parser.language_href:
            # 唯一允许的本地文档跳转；对应文件、语言和全部小节由专用检查器核验。
            assert (path.parent / unquote(href.split("#", 1)[0])).is_file(), f"{path.name} 对应语言文件不存在。"
        else:
            assert re.match(r"https?://|mailto:", href), f"离线 HTML 依赖本地文档：{href}"
    assert not parser.dependencies, f"存在外部脚本或媒体：{parser.dependencies}"
    assert parser.empty_lightbox_images == 1, f"{path.name} 应仅保留一张空的灯箱图片。"
    checked = verified_images if verified_images is not None else set()
    for attributes in parser.images:
        src = attributes.get("src", "")
        reference = attributes.get("data-source", "")
        assert src.startswith("data:image/") and ";base64," in src, f"{path.name} HTML 图片未内嵌。"
        assert reference, f"{path.name} 内嵌图片缺少 data-source：{attributes.get('alt', '')}"
        original = source_image_path(reference, path.name)
        payload = base64.b64decode(src.split(",", 1)[1], validate=True)
        identity = (reference, hashlib.sha256(payload).digest())
        if identity not in checked:
            with Image.open(io.BytesIO(payload)) as picture:
                picture.verify()
            with Image.open(io.BytesIO(payload)) as picture, Image.open(original) as source:
                assert picture.size == source.size, (
                    f"{path.name} 内嵌图片被缩放：{reference}，{picture.size} != {source.size}"
                )
                difference = ImageChops.difference(picture.convert("RGBA"), source.convert("RGBA"))
                assert difference.getbbox(alpha_only=False) is None, (
                    f"{path.name} 内嵌图片像素与原 PNG 不同：{reference}"
                )
            checked.add(identity)
        # 后续只需要来源与数量，不保留四份 HTML 的大段 base64 字符串。
        attributes.pop("src", None)
    if markdown_texts is not None:
        expected_headings = [heading for value in markdown_texts for heading in markdown_headings(value)]
        assert parser.headings == expected_headings, f"{path.name} 正文标题缺失、顺序错误或与 Markdown 不一致。"
        expected_images = Counter(image for value in markdown_texts for image in markdown_images(value))
        actual_images = Counter(parser.figure_sources)
        assert actual_images == expected_images, (
            f"{path.name} 正文配图不完整：缺少 {dict(expected_images - actual_images)}，"
            f"多出 {dict(actual_images - expected_images)}"
        )
    return parser


def read_build_constants() -> dict:
    """只解析构建模块的常量，避免导入时执行任何业务代码。"""
    root = ast.parse(Path(__file__).with_name("build.py").read_text(encoding="utf-8"))
    found = {}
    for node in root.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {
                    "MANUAL_HEAD",
                    "CHAPTER_ORDER",
                    "EN_MANUAL_HEAD",
                    "EN_CHAPTER_ORDER",
                }:
                    found[target.id] = ast.literal_eval(node.value)
    assert set(found) == {"MANUAL_HEAD", "CHAPTER_ORDER", "EN_MANUAL_HEAD", "EN_CHAPTER_ORDER"}, (
        "构建脚本缺少完整的中英文手册常量。"
    )
    return found


def verify_translation(chinese: str, english: str, context: str) -> None:
    """核对译文的章节、表格、代码块和配图完整性，允许示例数据保留中文。"""
    zh_headings = markdown_headings(chinese)
    en_headings = markdown_headings(english)
    assert [level for level, _ in zh_headings] == [level for level, _ in en_headings], (
        f"{context} 中英文标题数量或层级不一致。"
    )
    numbers = [
        [
            match[1]
            for level, title in headings
            if level > 1 and (match := re.match(r"^(\d+(?:\.\d+)*)(?=[\s.:：])", title))
        ]
        for headings in (zh_headings, en_headings)
    ]
    assert numbers[0] == numbers[1], f"{context} 英文章节编号缺失或顺序错误。"
    zh_rows = [line.count("|") for line in markdown_lines(chinese) if line.startswith("|")]
    en_rows = [line.count("|") for line in markdown_lines(english) if line.startswith("|")]
    assert zh_rows == en_rows, f"{context} 中英文表格行数或列数不一致。"
    fences = [
        [line.strip()[3:] for line in value.splitlines() if line.strip().startswith(chr(96) * 3)]
        for value in (chinese, english)
    ]
    assert fences[0] == fences[1], f"{context} 中英文代码块数量或类型不一致。"
    expected_images = [image.replace("images/guide/", "images/guide-en/") for image in markdown_images(chinese)]
    assert markdown_images(english) == expected_images, f"{context} 英文配图缺失、顺序错误或仍指向中文配图。"
