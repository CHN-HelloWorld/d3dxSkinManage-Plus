"""验证教程从当前源码生成，并以独立 HTML 纳入两种发行格式。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

DOCUMENT_NAMES = (
    "d3dxSkinManage-Plus 使用教程.html",
    "d3dxSkinManage-Plus User Guide.html",
    "d3dxSkinManage-Plus 插件开发指南.html",
    "d3dxSkinManage-Plus Plugin Development Guide.html",
)


def _write_files(directory: Path, files: dict[str, str]) -> None:
    """向隔离目录写入明确列出的文档或暂存文件。"""
    for relative, content in files.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
