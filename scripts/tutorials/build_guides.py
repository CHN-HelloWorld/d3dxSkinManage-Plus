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


def build_one(spec: dict, record: dict, destination: Path) -> dict:
    """绘制框选、编号及独立说明区，不覆盖原始控件文字。"""
    language = record.get("language", "zh-CN")
    source = TUTORIAL / "images" / ("final-en" if language == "en-US" else "final") / record["file"]
    with Image.open(source) as original:
        screen = original.convert("RGB")
    if screen.size != (record["width"], record["height"]):
        raise ValueError(f"截图尺寸改变，请先重新采集控件位置：{source.name}")
    scale = record.get("device_pixel_ratio", 1)
    logical_width = record.get("logical_width", screen.width)
    logical_height = record.get("logical_height", screen.height)
    if screen.size != (round(logical_width * scale), round(logical_height * scale)):
        raise ValueError(f"截图物理尺寸与渲染倍率不一致：{source.name}")
    marks = [{**mark, "box": [round(value * scale) for value in target_box(mark, record)]} for mark in spec["marks"]]
    for mark in marks:
        x, y, width, height = mark["box"]
        if min(x, y) < 0 or min(width, height) <= 0 or x + width > screen.width or y + height > screen.height:
            raise ValueError(f"标注越出截图：{source.name} {mark}")

    font_size = round((25 if logical_width >= 1000 else 18) * scale)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", font_size)
    bold = ImageFont.truetype("C:/Windows/Fonts/msyhbd.ttc", font_size)
    pad = round(24 * scale)
    width = max(screen.width + pad * 2, round(560 * scale))
    origin_x = (width - screen.width) // 2
    radius = round((18 if logical_width >= 1000 else 14) * scale)
    line_height = font_size + round(12 * scale)
    titles = wrap(spec["title"], bold, width - pad * 2)
    header = max(round(96 * scale), round(16 * scale) + len(titles) * line_height + radius * 2 + round(8 * scale))
    explanations = [wrap(mark["label"], font, width - pad * 2 - radius * 2 - round(20 * scale)) for mark in marks]
    footer_height = sum(len(lines) * line_height + round(14 * scale) for lines in explanations) + pad * 2
    canvas = Image.new("RGB", (width, header + screen.height + footer_height), "#111e2c")
    draw = ImageDraw.Draw(canvas)
    for index, title in enumerate(titles):
        draw.text((pad, round(16 * scale) + index * line_height), title, font=bold, fill="#eaf6ff")
    canvas.paste(screen, (origin_x, header))

    for number, mark in enumerate(marks, 1):
        x, y, box_width, box_height = mark["box"]
        left, top = origin_x + x, header + y
        draw.rounded_rectangle(
            (left - 3 * scale, top - 3 * scale, left + box_width + 3 * scale, top + box_height + 3 * scale),
            radius=round(5 * scale),
            outline="#56cdff",
            width=round(3 * scale),
        )
        # 编号放在目标左侧的外边缘，保留目标内部的完整文字。
        badge = mark.get("badge", "above" if y < 80 * scale else "left")
        if badge == "above":
            center = (left + radius + 3 * scale, top - radius - 8 * scale)
        elif badge == "below":
            center = (left + radius + 3 * scale, top + box_height + radius + 8 * scale)
        else:
            center = (max(radius + 3 * scale, left - radius - 6 * scale), top + min(box_height / 2, radius + 3 * scale))
        draw.ellipse(
            (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius),
            fill="#157cb2",
            outline="#d1f1ff",
            width=round(2 * scale),
        )
        draw.text(center, str(number), font=bold, fill="white", anchor="mm")

    cursor = header + screen.height + pad
    draw.line((pad, cursor - 12 * scale, width - pad, cursor - 12 * scale), fill="#345064", width=round(scale))
    for number, lines in enumerate(explanations, 1):
        center = (pad + radius, cursor + line_height / 2)
        draw.ellipse(
            (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius),
            fill="#157cb2",
        )
        draw.text(center, str(number), font=bold, fill="white", anchor="mm")
        for line in lines:
            draw.text((pad + radius * 2 + 20 * scale, cursor), line, font=font, fill="#eaf2fa")
            cursor += line_height
        cursor += round(14 * scale)
    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(destination, "WEBP", lossless=True, exact=True, method=4)
    return {
        "file": source.name,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "image_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "source_size": list(screen.size),
        "size": list(canvas.size),
        "title": spec["title"],
        "marks": marks,
        "source": source.relative_to(TUTORIAL).as_posix(),
        "language": language,
        "device_pixel_ratio": scale,
        "screen_origin": [origin_x, header],
    }


def main() -> None:
    """根据标注清单构建全部配图，保存可核验的来源和坐标。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=("zh-CN", "en-US", "all"), default="all")
    args = parser.parse_args()
    for language in ("zh-CN", "en-US") if args.language == "all" else (args.language,):
        suffix = "-en" if language == "en-US" else ""
        layouts = "guide_layouts.en.json" if language == "en-US" else "guide_layouts.json"
        specs = json.loads((Path(__file__).parent / layouts).read_text(encoding="utf-8"))
        captures = json.loads((TUTORIAL / f"images/final{suffix}/captures.json").read_text(encoding="utf-8"))
        records = {record["file"]: record for record in captures["screenshots"]}
        output = TUTORIAL / f"images/guide{suffix}"
        results = [build_one(spec, records[spec["file"]], output / spec["file"]) for spec in specs]
        (output / "annotations.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"已生成 {len(results)} 张操作配图、{sum(len(result['marks']) for result in results)} 处编号说明：{output}"
        )


if __name__ == "__main__":
    main()
