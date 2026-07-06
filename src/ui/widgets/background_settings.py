"""独立配置图片、视频和场景背景来源。"""

import json
from contextlib import ExitStack
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, QSize, Qt
from PySide6.QtGui import QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import (
    QBoxLayout,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
)

from src.ui import icons, theme
from src.ui.backgrounds.engine import EnginePathValidator
from src.ui.settings_style import color, stroke, surface
from src.ui.widgets.settings_components import ChoiceSegments, icon_label, plain_widget, style_button, text_label
from src.utils import fs
from src.utils.i18n import t as tr


class _AssetPathLabel(QLabel):
    """在有限宽度内省略路径中部，并通过提示保留完整素材路径。"""

    def set_path(self, path):
        """保存完整路径作为提示，并按当前标签宽度更新省略显示。"""
        self._path = path
        self.setToolTip(path)
        self._elide()

    def _elide(self):
        """按当前字体和宽度省略路径中部，尚未设置路径时显示空文本。"""
        self.setText(
            self.fontMetrics().elidedText(
                getattr(self, "_path", ""), Qt.TextElideMode.ElideMiddle, max(1, self.width())
            )
        )

    def resizeEvent(self, event):
        """在标签宽度变化后重新计算路径省略位置。"""
        super().resizeEvent(event)
        self._elide()


class _AssetThumbnail(QLabel):
    """占位图随主题变化，不替换实际预览图。"""

    def __init__(self):
        """建立居中的素材缩略图区域，初始使用紧凑占位尺寸。"""
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.clear()

    def clear(self):
        """清除素材位图并恢复占位图标所需的尺寸。"""
        super().clear()
        self.setFixedSize(32, 40)

    def setPixmap(self, pixmap):
        """切换到素材预览尺寸后显示给定缩略图。"""
        self.setFixedSize(88, 60)
        super().setPixmap(pixmap)

    def paintEvent(self, event):
        """有预览图时沿用标签绘制，否则使用当前主题的图片图标占位。"""
        if not self.pixmap().isNull():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.drawPixmap(0, (self.height() - 32) // 2, icons.pixmap("settings/image", 32, "settings.muted"))
