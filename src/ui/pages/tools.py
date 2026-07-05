"""提供限定范围的维护工具和活动插件命令。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui import icons, settings_style, theme
from src.ui.smooth_scroll import SmoothScroller
from src.ui.widgets.tag_chip import TagFlowLayout
from src.utils.i18n import t


def _label(text="", role="body", name=""):
    """创建可换行的纯文本标签，并登记工具页样式角色与对象名称。"""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setProperty("toolsRole", role)
    label.setObjectName(name)
    return label


def _symbol(name, size=24, token="settings.secondary"):
    """创建固定尺寸的主题图标标签，使用指定颜色令牌绘制图标。"""
    label = _label()
    label.setFixedSize(size, size)
    icons.apply_pixmap(label, name, size, token)
    return label


def _button(text, name, callback, *, primary=False, icon=""):
    """创建工具操作按钮，绑定回调并按主次样式配置图标与尺寸。"""
    button = QPushButton(text)
    button.setObjectName(name)
    button.setProperty("toolsButton", "primary" if primary else "standard")
    button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    button.setMinimumHeight(36)
    button.clicked.connect(callback)
    if icon:
        icons.apply_icon(button, icon, "settings.on_accent" if primary else "settings.text")
        button.setIconSize(QSize(17, 17))
    return button


class ToolCard(QWidget):
    """紧凑透明操作行，窄页面将按钮排列到说明下方。"""

    def __init__(self, title, description, btn_text, *, icon, scope="", name="", parent=None):
        """组合工具图标、作用范围、说明和操作按钮，保留网格布局以适应窄屏换行。"""
        super().__init__(parent)
        self.setObjectName(name or "toolCard")
        self.setProperty("toolsPlain", True)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(20, 15, 20, 15)
        self._grid.setHorizontalSpacing(16)
        self._grid.setVerticalSpacing(10)
        self._grid.setColumnStretch(1, 1)
        self._grid.addWidget(_symbol(icon), 0, 0, Qt.AlignmentFlag.AlignVCenter)
        copy = QWidget()
        copy.setProperty("toolsPlain", True)
        text = QVBoxLayout(copy)
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(4)
        self._title_label = _label(title, "strong")
        if scope:
            heading = QHBoxLayout()
            heading.setSpacing(9)
            heading.addWidget(self._title_label)
            heading.addWidget(_label(scope, "scope"))
            heading.addStretch()
            text.addLayout(heading)
        else:
            text.addWidget(self._title_label)
        self._desc_label = _label(description, "secondary")
        text.addWidget(self._desc_label)
        self._grid.addWidget(copy, 0, 1)
        self.button = _button(btn_text, "", lambda: None)
        self.button.setMinimumWidth(88)
        self._grid.addWidget(self.button, 0, 2, Qt.AlignmentFlag.AlignRight)
        self._compact = False

    def resizeEvent(self, event):
        """仅在跨越宽度阈值时移动操作按钮，使窄卡片中的按钮换到说明下方。"""
        super().resizeEvent(event)
        compact = self.width() < 490
        if compact != self._compact:
            self._compact = compact
            self._grid.removeWidget(self.button)
            self._grid.addWidget(
                self.button,
                1 if compact else 0,
                1 if compact else 2,
                Qt.AlignmentFlag.AlignLeft if compact else Qt.AlignmentFlag.AlignRight,
            )
