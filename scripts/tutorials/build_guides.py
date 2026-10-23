"""把真实截图和操作标注合成阅读用配图，保留原始截图。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

from demo_common import REPO, TUTORIAL
from PIL import Image, ImageDraw, ImageFont


def wrap(text: str, font, width: int) -> list[str]:
    """按实际字宽折行，避免中文说明越出图边。"""
    lines, line = [], ""
    tokens = re.findall(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*|\s+|.", text)
    for character in tokens:
        if line and font.getlength(line + character) > width and character not in "，。；：！？、）》」】":
            lines.append(line.rstrip())
            line = ""
        line += character if line else character.lstrip()
    if line:
        lines.append(line)
    return lines
