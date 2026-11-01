"""将 Qt 截图写为保留原始像素的教程资源。"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice


def save_qt_capture(picture, destination: Path) -> None:
    """通过内存 PNG 读取 Qt 像素，明确使用无损 WebP 编码。"""
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not picture.save(buffer, "PNG"):
        raise RuntimeError(f"截图编码失败：{destination.name}")
    with Image.open(io.BytesIO(bytes(buffer.data()))) as original:
        original.save(destination, "WEBP", lossless=True, exact=True, method=4)
