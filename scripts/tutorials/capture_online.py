"""用中性占位记录填充真实在线页面，仅用于教程截图。"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


class PlaceholderImages:
    """从演示目录读取占位图片，避免连接任何在线服务。"""

    def __init__(self, workspace: Path):
        """固定允许读取的演示根目录。"""
        self.workspace = workspace.resolve()

    def load(self, url, target, *, size=None, **kwargs):
        """通过实际图片控件显示本地占位图。"""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPixmap

        source = Path(url).resolve()
        if not source.is_relative_to(self.workspace):
            raise ValueError("占位图片越出演示工作区。")
        pixmap = QPixmap(str(source))
        if size is not None and not size.isEmpty():
            pixmap = pixmap.scaled(size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        target.setPixmap(pixmap)
