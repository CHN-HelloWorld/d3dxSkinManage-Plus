"""将指定的正方形图片转换为应用使用的多尺寸 ICO 图标。"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[2]
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def main() -> int:
    """校验正方形源图并生成多尺寸 ICO，小尺寸增加锐化以保持图标清晰。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="至少 256 像素的正方形图片")
    parser.add_argument("--output", type=Path, default=ROOT / "resources/app.ico", help="输出 ICO 路径")
    args = parser.parse_args()
    with Image.open(args.source) as image:
        if image.width != image.height or image.width < 256:
            parser.error("源图片必须为至少 256 像素的正方形")
        base = image.convert("RGBA")

    frames = []
    for size in SIZES:
        frame = base.resize((size, size), Image.Resampling.LANCZOS)
        if size <= 48:
            frame = frame.filter(ImageFilter.UnsharpMask(radius=0.6, percent=70, threshold=0))
        frames.append(frame)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    frames[-1].save(
        args.output,
        format="ICO",
        sizes=[(size, size) for size in SIZES],
        append_images=frames[:-1],
    )
    print(f"已生成 {args.output}，包含 {len(SIZES)} 个尺寸。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
