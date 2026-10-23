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


@lru_cache(maxsize=1)
def english_messages() -> dict[str, str]:
    """使用程序实际语言包定位英文控件，不改变截图中的用户数据。"""
    return json.loads((REPO / "config/languages/en-US/strings.json").read_text(encoding="utf-8"))["messages"]


def target_box(mark: dict, record: dict) -> list[int]:
    """优先按控件文本定位；自绘控件使用明确像素范围。"""
    if "box" in mark:
        return mark["box"]
    targets = {mark["target"]}
    if record.get("language") == "en-US":
        targets.add(mark.get("target_en", english_messages().get(mark["target"], mark["target"])))
    candidates = {
        tuple(control["box"])
        for control in record.get("controls", [])
        if control["text"].strip() in targets and (not mark.get("class") or control["class"] == mark["class"])
    }
    ordered = sorted(candidates, key=lambda box: (box[1], box[0]))
    if not ordered or (len(ordered) != 1 and "occurrence" not in mark):
        raise ValueError(f"{record['file']}：无法唯一定位 {mark['target']!r}，候选 {ordered}")
    return list(ordered[mark.get("occurrence", 0)])
