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
