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
