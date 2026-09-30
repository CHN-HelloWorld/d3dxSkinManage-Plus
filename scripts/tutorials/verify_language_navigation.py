"""核验生成的四份教程能双向切换语言，且每个小节都指向对应译文。"""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

TUTORIAL = Path(__file__).resolve().parents[2] / "docs" / "tutorials"
DOCUMENT_PAIRS = (
    ("d3dxSkinManage-Plus 使用教程.html", "d3dxSkinManage-Plus User Guide.html"),
    ("d3dxSkinManage-Plus 插件开发指南.html", "d3dxSkinManage-Plus Plugin Development Guide.html"),
)


class LanguageNavigation(HTMLParser):
    """只读取实际 HTML 的导航和标题，不调用构建器的配对逻辑。"""

    def __init__(self) -> None:
        """初始化语言、锚点及标题的解析结果。"""
        super().__init__(convert_charrefs=True)
        self.language = None
        self.switches = []
        self.current = []
        self.headings = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        """读取页眉切换链接、页面语言及标题对应关系。"""
        attributes = dict(attrs)
        if tag == "html":
            self.language = attributes.get("lang")
        if attributes.get("id"):
            self.ids.add(attributes["id"])
        if tag == "a" and attributes.get("id") == "language-link":
            self.switches.append(attributes)
        if attributes.get("id") == "language-current":
            self.current.append(attributes)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") and attributes.get("id"):
            self.headings.append((tag, attributes["id"], attributes.get("data-translation-id")))


def verify_language_navigation(output_dir: Path | None = None) -> None:
    """逐页检查语言入口和所有小节的双向切换目标。"""
    output_dir = output_dir or TUTORIAL.parent.parent / "build/tutorials"
    total = 0
    for filenames in DOCUMENT_PAIRS:
        pages = []
        for filename in filenames:
            page = LanguageNavigation()
            page.feed((output_dir / filename).read_text(encoding="utf-8"))
            page.close()
            pages.append(page)
        assert [page.language for page in pages] == ["zh-CN", "en-US"], f"{filenames} 语言顺序错误。"
        for index, page in enumerate(pages):
            filename, alternate = filenames[index], pages[1 - index]
            assert len(page.switches) == 1, f"{filename} 缺少唯一的页眉语言切换链接。"
            switch = page.switches[0]
            target = urlsplit(switch.get("href", ""))
            assert not target.scheme and not target.netloc and not target.query, f"{filename} 语言切换应使用相对路径。"
            assert unquote(target.path) == filenames[1 - index], f"{filename} 未指向同类型的对应语言文档。"
            assert unquote(target.fragment) in alternate.ids, f"{filename} 默认语言切换目标不存在。"
            assert switch.get("target", "_self") == "_self", f"{filename} 语言切换不应另开标签页。"
            assert switch.get("hreflang") == switch.get("lang") == alternate.language, f"{filename} 目标语言标记错误。"
            assert len(page.current) == 1, f"{filename} 缺少当前语言标记。"
            assert page.current[0].get("lang") == page.language, f"{filename} 当前语言标记不符。"
            assert page.current[0].get("aria-current") == "page", f"{filename} 当前语言没有无障碍标记。"
            assert page.headings and len(page.headings) == len(alternate.headings), f"{filename} 中英文标题不完整。"
            for source, translated in zip(page.headings, alternate.headings, strict=True):
                assert source[0] == translated[0], f"{filename}/{source[1]} 对应标题层级错误。"
                assert source[2] == translated[1], f"{filename}/{source[1]} 未指向对应译文：{translated[1]}。"
                assert translated[2] == source[1], f"{filename}/{source[1]} 不能从译文返回。"
            total += len(page.headings)
    print(f"语言导航检查通过：四份 HTML、{total} 个小节切换目标，双向对应且保留原有锚点。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=TUTORIAL.parent.parent / "build/tutorials")
    verify_language_navigation(parser.parse_args().output_dir)
