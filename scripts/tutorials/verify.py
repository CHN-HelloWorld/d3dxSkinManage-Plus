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


def index_records(records: list[dict], context: str) -> dict[str, dict]:
    """按截图文件名建立索引，同时拒绝重复记录和路径形式的文件名。"""
    assert isinstance(records, list) and records, f"{context} 缺少截图记录。"
    by_file = {}
    for record in records:
        name = record.get("file", "")
        assert re.fullmatch(r"\d{2}-[^/\\]+\.webp", name), f"{context} 截图文件名无效：{name}"
        assert name not in by_file, f"{context} 截图记录重复：{name}"
        by_file[name] = record
    return by_file


def verify_box(box, size, context: str) -> None:
    """坐标必须是有面积且位于指定坐标系内的整数矩形。"""
    assert isinstance(box, (list, tuple)) and len(box) == 4 and all(type(value) is int for value in box), (
        f"{context} 矩形坐标无效：{box}"
    )
    x, y, width, height = box
    assert 0 <= x < x + width <= size[0] and 0 <= y < y + height <= size[1], (
        f"{context} 矩形越界或没有面积：{box}，边界 {size}"
    )


def annotation_box(mark: dict, capture: dict, messages: dict, context: str) -> list[int]:
    """由布局的逻辑矩形或真实控件独立计算应有的物理标注坐标。"""
    logical_size = (capture["logical_width"], capture["logical_height"])
    if "box" in mark:
        logical = mark["box"]
    else:
        target = mark["target"]
        if capture["language"] == "en-US":
            target = mark.get("target_en", messages.get(target, target))
        matches = {
            tuple(control["box"])
            for control in capture["controls"]
            if control["text"].strip() == target and (not mark.get("class") or control["class"] == mark["class"])
        }
        ordered = sorted(matches, key=lambda box: (box[1], box[0]))
        assert ordered and (len(ordered) == 1 or "occurrence" in mark), (
            f"{context} 无法唯一定位实际控件 {target!r}：{ordered}"
        )
        occurrence = mark.get("occurrence", 0)
        assert type(occurrence) is int and 0 <= occurrence < len(ordered), f"{context} 控件序号超出范围：{occurrence}"
        logical = ordered[occurrence]
    verify_box(logical, logical_size, f"{context} 逻辑坐标")
    return [round(value * capture["device_pixel_ratio"]) for value in logical]


def verify_guides(language: str, layouts: dict[str, dict], reader_text: str, messages: dict) -> dict[str, dict]:
    """核对每种语言的真实 2× 截图、完整标注记录、来源哈希和坐标缩放。"""
    suffix = "-en" if language == "en-US" else ""
    original_dir = TUTORIAL / f"images/final{suffix}"
    guide_dir = TUTORIAL / f"images/guide{suffix}"
    manifest = json.loads((original_dir / "captures.json").read_text(encoding="utf-8"))
    assert manifest.get("language") == language and manifest.get("theme") == "dark", (
        f"{original_dir.name} 截图清单的语言或主题错误。"
    )
    captures = index_records(manifest["screenshots"], f"{language} 原始截图")
    assert captures.keys() == layouts.keys(), f"{language} 截图清单未覆盖全部 33 个布局场景。"
    for name, capture in captures.items():
        context = f"{language}/{name}"
        assert capture.get("language") == language, f"{context} 截图语言不符。"
        assert capture.get("device_pixel_ratio") == 2, f"{context} 截图不是原生 2× 渲染。"
        geometry = [capture.get(key) for key in ("width", "height", "logical_width", "logical_height")]
        assert all(type(value) is int and value > 0 for value in geometry), f"{context} 截图尺寸字段缺失或无效。"
        width, height, logical_width, logical_height = geometry
        assert (width, height) == (logical_width * 2, logical_height * 2), f"{context} 物理尺寸与逻辑尺寸及倍率不一致。"
        with Image.open(original_dir / name) as picture:
            assert picture.size == (width, height), f"{context} 截图尺寸与记录不符。"
            picture.verify()
        assert capture.get("controls"), f"{context} 缺少真实控件坐标。"
        for index, control in enumerate(capture["controls"], 1):
            assert control.get("class") and isinstance(control.get("text"), str), (
                f"{context} 控件 {index} 缺少类型或文字。"
            )
            verify_box(control["box"], (logical_width, logical_height), f"{context} 控件 {index}")
        visible_text = {control["text"].strip() for control in capture["controls"]}
        for required in capture.get("required_text", []):
            expected = messages.get(required, required) if language == "en-US" else required
            assert required in visible_text or expected in visible_text, f"{context} 缺少必要控件：{expected}"

    guides = index_records(
        json.loads((guide_dir / "annotations.json").read_text(encoding="utf-8")),
        f"{language} 操作配图",
    )
    assert guides.keys() == layouts.keys(), f"{language} 操作配图未覆盖全部 33 个布局场景。"
    prefix = f"images/guide{suffix}/"
    used_images = [image for image in markdown_images(reader_text) if image.startswith(prefix)]
    assert used_images, f"{language} 正文没有引用对应语言的操作配图。"
    for relative in used_images:
        assert relative.removeprefix(prefix) in guides, f"{language} 缺少配图来源记录：{relative}"
    for name, record in guides.items():
        context = f"{language}/{name}"
        capture = captures[name]
        spec = layouts[name]
        original = original_dir / name
        annotated = guide_dir / name
        assert record.get("source") == original.relative_to(TUTORIAL).as_posix(), f"{context} 标注来源路径错误。"
        assert record.get("language") == language and record.get("device_pixel_ratio") == 2, (
            f"{context} 标注语言或倍率错误。"
        )
        assert record.get("source_size") == [capture["width"], capture["height"]], (
            f"{context} 标注记录使用了错误的原图物理尺寸。"
        )
        assert hashlib.sha256(original.read_bytes()).hexdigest() == record["source_sha256"], (
            f"{context} 原图已变化，标注需要重建。"
        )
        assert hashlib.sha256(annotated.read_bytes()).hexdigest() == record["image_sha256"], (
            f"{context} 标注图与记录不同步。"
        )
        with Image.open(annotated) as picture:
            canvas_size = picture.size
            assert record.get("size") == list(canvas_size), f"{context} 标注图尺寸与记录不符。"
            picture.verify()
        origin = record.get("screen_origin")
        assert isinstance(origin, list) and len(origin) == 2 and all(type(value) is int for value in origin), (
            f"{context} 缺少有效的原图放置坐标。"
        )
        verify_box([*origin, *record["source_size"]], canvas_size, f"{context} 原图放置范围")
        assert origin[0] == (canvas_size[0] - capture["width"]) // 2, f"{context} 原图未按原尺寸居中放置。"
        assert origin[1] > 0 and origin[1] + capture["height"] < canvas_size[1], f"{context} 缺少标题或说明区。"
        assert record.get("title") == spec["title"] and spec["title"].strip(), f"{context} 配图标题与布局不一致。"
        assert record.get("marks") and len(record["marks"]) == len(spec["marks"]), f"{context} 编号说明缺失或数量错误。"
        for number, (mark, expected) in enumerate(zip(record["marks"], spec["marks"]), 1):
            mark_context = f"{context} 标注 {number}"
            assert expected.get("label", "").strip(), f"{mark_context} 缺少操作说明。"
            assert all(mark.get(key) == value for key, value in expected.items() if key != "box"), (
                f"{mark_context} 说明或目标与当前布局不一致。"
            )
            assert mark.get("box") == annotation_box(expected, capture, messages, mark_context), (
                f"{mark_context} 未按控件逻辑坐标和 2× 倍率生成。"
            )
            verify_box(mark["box"], record["source_size"], f"{mark_context} 物理坐标")
    assert (original_dir / "19-关于.webp").read_bytes() != (original_dir / "18-插件.webp").read_bytes(), (
        f"{language} 关于页配图仍与插件页重复。"
    )
    return guides
