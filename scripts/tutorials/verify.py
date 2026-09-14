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
