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
