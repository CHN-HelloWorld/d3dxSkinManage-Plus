"""生成不依赖个人资源的教程演示库；只写带专用标记的系统临时目录。"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

from demo_common import DEMO_VERSION, GAME_ID, REPO, isolate


def make_preview(destination: Path, title: str, color: str) -> None:
    """生成只有几何图案和说明文字的占位图，不使用任何游戏或角色素材。"""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (800, 600), "#16222e")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((28, 28, 772, 572), radius=28, fill=color)
    draw.rounded_rectangle((54, 430, 746, 548), radius=18, fill="#16222e")
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    title_font = ImageFont.truetype(str(font_path), 32)
    caption_font = ImageFont.truetype(str(font_path), 18)
    draw.rounded_rectangle((268, 106, 532, 316), radius=16, outline="#e0ebef", width=6)
    draw.ellipse((450, 136, 486, 172), fill="#e0ebef")
    draw.line(((287, 291), (354, 209), (411, 263), (455, 223), (513, 291)), fill="#e0ebef", width=7)
    draw.text((324, 352), "预览图占位", fill="#e0ebef", font=title_font)
    draw.text((80, 452), title, fill="white", font=title_font)
    draw.text((82, 503), "教程占位数据 · SAMPLE", fill="#b8c7d4", font=caption_font)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination)
