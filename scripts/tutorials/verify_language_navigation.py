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
