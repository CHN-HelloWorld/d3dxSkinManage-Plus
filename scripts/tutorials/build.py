"""把 Markdown 教程编译成**单文件 HTML**（图片 base64 内嵌，双击即开）。

每份文档的图片、样式和脚本均内嵌，可以独立离线阅读。
每份文件内置对应译文，语言切换保留当前页面和阅读位置。

同时负责把 `chapters/zh/` 和 `chapters/en/` 中的章节拼成完整手册。

用法::

    .venv/Scripts/python.exe scripts/tutorials/build.py

依赖：只用标准库 + 一个极小的自写 Markdown 子集渲染器（避免为教程引入新依赖）。
支持：ATX 标题、段落、无序/有序列表、表格、围栏代码块、引用块、图片、行内
``**粗体**`` / ``*斜体*`` / ``` `代码` ``` / 链接。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import mimetypes
import os
import re
import sys
from functools import cache
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

TUT = Path(__file__).resolve().parents[2] / "docs" / "tutorials"
IMAGES = TUT / "images"
DRAFT = TUT / "chapters"
DEFAULT_OUTPUT = TUT.parent.parent / "build" / "tutorials"
QUICK = TUT / "zh/5分钟快速上手.md"
MANUAL = TUT / "zh/完整功能手册.md"
OUT_HTML = TUT / "d3dxSkinManage-Plus 使用教程.html"
PLUGIN_GUIDE = TUT / "zh/插件开发指南.md"
OUT_PLUGIN_HTML = TUT / "d3dxSkinManage-Plus 插件开发指南.html"
EN_QUICK = TUT / "en/Quickstart.en.md"
EN_MANUAL = TUT / "en/Manual.en.md"
EN_PLUGIN_GUIDE = TUT / "en/PluginGuide.en.md"
EN_OUT_HTML = TUT / "d3dxSkinManage-Plus User Guide.html"
EN_OUT_PLUGIN_HTML = TUT / "d3dxSkinManage-Plus Plugin Development Guide.html"

EN_CHAPTER_ORDER = [
    "ch1-interface-and-navigation.md",
    "ch2-games-and-setup.md",
    "ch3-categories-and-mods.md",
    "ch4-presets-deployment-and-keys.md",
    "ch5-online-mods-and-updates.md",
    "ch6-settings.md",
    "ch7-tools-plugins-and-about.md",
    "ch8-data-backup-and-troubleshooting.md",
]

CHAPTER_ORDER = [
    "ch1-界面与导航.md",
    "ch2-游戏与首次配置.md",
    "ch3-类别总览与Mod管理.md",
    "ch4-预设部署与按键.md",
    "ch5-在线Mod与更新.md",
    "ch6-设置详解.md",
    "ch7-工具箱插件关于.md",
    "ch8-数据备份与排障.md",
]

MANUAL_HEAD = """# 完整功能手册

只想先装好一个 Mod，请从《5 分钟快速上手》开始；
本手册按功能提供详细步骤、设置说明和数据维护方法。

界面入口以简体中文标注。所有软件截图中的游戏、分类、Mod 与预览图均为占位数据，
不包含任何具体游戏的资源；正文外的普拉莎插画为本项目原创看板娘。

快捷键表列出默认绑定，自定义后的操作以「设置 → 快捷键」为准。
配图中的编号对应图下的操作说明，可点击图片放大查看。希望扩展程序功能时，继续阅读《插件开发指南》。
"""

EN_MANUAL_HEAD = """# Complete user manual

To get your first Mod working, begin with the 5-minute quick start.
This manual provides detailed steps, settings and data-maintenance guidance by feature.

Screenshots show the English interface with fictional games, categories, Mods and geometric preview placeholders.
Some sample library names, such as 外观 (Appearance), remain in Chinese because they are user-entered data.
Plusa is the project's original mascot.

The shortcut table lists default bindings. If you customize them, follow Settings → Shortcuts instead.
Numbers in the screenshots match the explanations below them.
Click an image, then choose Original size to inspect its details.
For application extensions, continue to the Plugin development guide.
"""

#: 章标题里出现哪个关键字 → 用哪张普拉莎表情当小图标（看板娘轻量点缀）
CHAPTER_EMOTE = {
    "第 1 步": "01-自信微笑",
    "第 2 步": "10-眨眼竖拇指",
    "第 3 步": "09-思考",
    "第 4 步": "01-自信微笑",
    "第 5 步": "08-星星眼",
    "第 6 步": "10-眨眼竖拇指",
    "开始之前": "09-思考",
    "没生效": "03-无语滴汗",
    "接下来看什么": "06-坏笑",
    "第 1 章": "01-自信微笑",
    "第 2 章": "10-眨眼竖拇指",
    "第 3 章": "08-星星眼",
    "第 4 章": "09-思考",
    "第 5 章": "08-星星眼",
    "第 6 章": "06-坏笑",
    "第 7 章": "11-害羞",
    "第 8 章": "03-无语滴汗",
    "附录 A": "10-眨眼竖拇指",
    "附录 B": "02-惊讶",
}

CHAPTER_EMOTE.update({f"Chapter {i}": CHAPTER_EMOTE[f"第 {i} 章"] for i in range(1, 9)})
CHAPTER_EMOTE.update({f"Step {i}": CHAPTER_EMOTE[f"第 {i} 步"] for i in range(1, 7)})
CHAPTER_EMOTE.update({"Appendix A": "10-眨眼竖拇指", "Appendix B": "02-惊讶"})
CHAPTER_EMOTE.update({"Before you start": "09-思考", "No effect?": "03-无语滴汗", "What to read next": "06-坏笑"})


def emote_for(text: str) -> str | None:
    """按章节标题选择用于导航提示的插画。"""
    for key, name in CHAPTER_EMOTE.items():
        if key in text:
            path = TUT / "images" / "plusa" / "face" / f"{name}.webp"
            return f"images/plusa/face/{name}.webp" if path.is_file() else None
    return None
