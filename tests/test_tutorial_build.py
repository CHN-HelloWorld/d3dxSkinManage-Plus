"""验证离线教程的源码引用及分章链接在生成后仍可访问。"""

import base64
import importlib.util
import io
from html.parser import HTMLParser
from pathlib import Path

import pytest
from PIL import Image


@pytest.fixture
def tutorial_builder(monkeypatch):
    """独立加载教程生成器，避免与程序构建模块混淆。"""
    source = Path(__file__).resolve().parents[1] / "scripts/tutorials/build.py"
    monkeypatch.syspath_prepend(str(source.parent))
    spec = importlib.util.spec_from_file_location("tutorial_builder", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_links_remain_readable_without_a_source_checkout(tutorial_builder, tmp_path, monkeypatch):
    tutorial = tmp_path / "docs/tutorials"
    tutorial.mkdir(parents=True)
    source = tmp_path / "plugins/sample/entry.py"
    source.parent.mkdir(parents=True)
    source.write_text('print("<example>")\n', encoding="utf-8")
    monkeypatch.setattr(tutorial_builder, "TUT", tutorial)

    rendered = tutorial_builder.embed_source_references(
        '<p><a href="../../plugins/sample/entry.py" target="_blank">Example</a></p>'
        '<p><a href="../../plugins/sample/entry.py">Again</a></p>',
        language="en-US",
    )

    assert 'href="../../' not in rendered
    assert rendered.count('<details class="source-file"') == 1
    assert rendered.count('href="#source-') == 2
    assert 'print("&lt;example&gt;")' in rendered
    assert "plugins/sample/entry.py" in rendered


def test_source_reference_cannot_embed_a_file_outside_the_repository(tutorial_builder, tmp_path, monkeypatch):
    tutorial = tmp_path / "project/docs/tutorials"
    tutorial.mkdir(parents=True)
    (tmp_path / "private.md").write_text("private", encoding="utf-8")
    monkeypatch.setattr(tutorial_builder, "TUT", tutorial)

    with pytest.raises(ValueError):
        tutorial_builder.embed_source_references('<a href="../../../private.md">Outside</a>', language="en-US")


@pytest.mark.parametrize("language,guide", [("zh-CN", "../en/PluginGuide.en.md"), ("en-US", "PluginGuide.en.md")])
def test_manual_rebases_chapter_images_and_links(tutorial_builder, tmp_path, monkeypatch, language, guide):
    chapters = tmp_path / "chapters"
    localized = chapters / ("en" if language == "en-US" else "zh")
    localized.mkdir(parents=True)
    (localized / "sample.md").write_text(
        "## Sample\n\n![Example](../../images/sample.png)\n\n[Guide](../../en/PluginGuide.en.md)\n", encoding="utf-8"
    )
    monkeypatch.setattr(tutorial_builder, "DRAFT", chapters)
    monkeypatch.setattr(tutorial_builder, "CHAPTER_ORDER", ["sample.md"])
    monkeypatch.setattr(tutorial_builder, "EN_CHAPTER_ORDER", ["sample.md"])
    (tmp_path / "zh").mkdir()
    (tmp_path / "en").mkdir()
    monkeypatch.setattr(tutorial_builder, "MANUAL", tmp_path / "zh/manual.md")
    monkeypatch.setattr(tutorial_builder, "EN_MANUAL", tmp_path / "en/manual.en.md")
    monkeypatch.setattr(tutorial_builder, "TUT", tmp_path)

    result = tutorial_builder.assemble_manual(language)

    assert "![Example](../images/sample.png)" in result
    assert f"[Guide]({guide})" in result


def test_linked_thumbnail_rebases_both_images_without_touching_code(tutorial_builder, tmp_path, monkeypatch):
    tutorial = tmp_path / "docs/tutorials"
    monkeypatch.setattr(tutorial_builder, "TUT", tutorial)
    source = tutorial / "chapters/en/chapter.md"
    linked = "[![Thinking](../../images/plusa/face/thinking.webp)](../../images/plusa/thinking.webp)"

    result = tutorial_builder.rebase_markdown_links(
        f'> {linked} A useful tip.\n\n<img src="../../images/plusa/pose.webp" width="280" alt="Plusa">'
        f"\n\n```md\n{linked}\n```\n",
        source,
    )

    assert result == (
        "> [![Thinking](images/plusa/face/thinking.webp)](images/plusa/thinking.webp) A useful tip.\n\n"
        '<img src="images/plusa/pose.webp" width="280" alt="Plusa">\n\n'
        f"```md\n{linked}\n```\n"
    )


class _RenderedPictures(HTMLParser):
    """读取浏览器实际收到的图片和预览链接。"""

    def __init__(self):
        super().__init__()
        self.images = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.images.append(dict(attrs))
        elif tag == "a":
            self.links.append(dict(attrs))


def test_callout_embeds_thumbnail_and_full_size_image_losslessly(tutorial_builder, tmp_path, monkeypatch):
    thumbnail = tmp_path / "images/plusa/face/thinking.webp"
    original = tmp_path / "images/plusa/thinking.webp"
    thumbnail.parent.mkdir(parents=True)
    Image.new("RGBA", (4, 3), (17, 53, 91, 127)).save(thumbnail, lossless=True, exact=True)
    Image.new("RGBA", (8, 6), (91, 53, 17, 0)).save(original, lossless=True, exact=True)
    monkeypatch.setattr(tutorial_builder, "TUT", tmp_path)
    markdown = (
        '## Plugin setup\n\n> [![Thinking & "checking"](images/plusa/face/thinking.webp)]'
        "(images/plusa/thinking.webp) **Plusa:** Check the selected game.\n"
    )

    rendered = tutorial_builder.render(markdown, [], set(), "")
    parsed = _RenderedPictures()
    parsed.feed(tutorial_builder.embed_source_references(rendered, language="en-US"))

    assert len(parsed.images) == len(parsed.links) == 1
    assert parsed.images[0]["alt"] == 'Thinking & "checking"'
    assert parsed.images[0]["data-source"] == "images/plusa/face/thinking.webp"
    assert parsed.links[0]["data-image-source"] == "images/plusa/thinking.webp"
    for url, expected_size, expected_pixels in (
        (parsed.images[0]["src"], (4, 3), bytes((17, 53, 91, 127)) * 12),
        (parsed.links[0]["href"], (8, 6), bytes((91, 53, 17, 0)) * 48),
    ):
        assert url.startswith("data:image/webp;base64,")
        with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as decoded:
            assert decoded.size == expected_size
            assert decoded.convert("RGBA").tobytes() == expected_pixels


def test_inline_image_example_stays_code(tutorial_builder):
    rendered = tutorial_builder.inline("`![Example](images/missing.webp)`", None)

    assert rendered == "<code>![Example](images/missing.webp)</code>"


def test_sized_markdown_illustration_keeps_original_pixels(tutorial_builder, tmp_path, monkeypatch):
    image = tmp_path / "images/plusa/pose.webp"
    image.parent.mkdir(parents=True)
    Image.new("RGBA", (320, 480), (17, 53, 91, 127)).save(image, lossless=True, exact=True)
    monkeypatch.setattr(tutorial_builder, "TUT", tmp_path)

    rendered = tutorial_builder.render(
        '<img src="images/plusa/pose.webp" width="280" alt="Plusa &amp; setup">\n\n*Setup guidance.*\n',
        [],
        set(),
        "",
    )
    parsed = _RenderedPictures()
    parsed.feed(rendered)

    assert len(parsed.images) == 1
    assert parsed.images[0]["width"] == "280"
    assert parsed.images[0]["alt"] == "Plusa & setup"
    assert "<figcaption>Setup guidance.</figcaption>" in rendered
    payload = base64.b64decode(parsed.images[0]["src"].split(",", 1)[1])
    with Image.open(io.BytesIO(payload)) as decoded:
        assert decoded.size == (320, 480)
        assert decoded.convert("RGBA").tobytes() == bytes((17, 53, 91, 127)) * (320 * 480)


def test_qt_capture_preserves_pixels_in_webp(tmp_path, monkeypatch):
    from PySide6.QtGui import QColor, QImage

    tools = Path(__file__).resolve().parents[1] / "scripts/tutorials"
    monkeypatch.syspath_prepend(str(tools))
    source = tools / "image_assets.py"
    assert source.is_file(), "The screenshot writer must explicitly select lossless WebP."
    spec = importlib.util.spec_from_file_location("tutorial_image_assets", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    picture = QImage(4, 3, QImage.Format.Format_RGBA8888)
    picture.fill(QColor(17, 53, 91, 127))
    destination = tmp_path / "capture.webp"

    module.save_qt_capture(picture, destination)

    with Image.open(destination) as decoded:
        assert decoded.format == "WEBP"
        assert decoded.size == (4, 3)
        assert decoded.convert("RGBA").tobytes() == bytes((17, 53, 91, 127)) * 12
