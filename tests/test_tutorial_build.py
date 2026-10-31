"""验证离线教程的源码引用及分章链接在生成后仍可访问。"""

import importlib.util
from pathlib import Path

import pytest


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
