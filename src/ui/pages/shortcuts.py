"""根据当前命令表展示和配置快捷键。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui import icons, theme
from src.ui import settings_style as fluent
from src.ui.dialogs.shortcut_edit import ShortcutEditDialog
from src.ui.shortcut_manager import ShortcutSettings
from src.ui.smooth_scroll import SmoothScroller
from src.ui.widgets.settings_components import icon_label, plain_widget, settings_note
from src.ui.widgets.shortcut_controls import BindingButton, KeyCaps, ShortcutButton, ShortcutToast, button_style, color
from src.utils.i18n import t as tr


def _icon(name, token="text.secondary", size=16):
    """创建不截获鼠标事件的主题图标标签，使点击交由所在操作控件处理。"""
    label = QLabel()
    label.setFixedSize(size, size)
    label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    icons.apply_pixmap(label, name, size, token)
    return label


class _ShortcutRow(QWidget):
    """展示单条命令的用途、当前绑定、自定义状态和恢复默认入口。"""

    def __init__(self, command, binding, *, last=False):
        """构建命令说明与绑定操作列，按当前绑定是否偏离默认值显示恢复入口和自定义标记。"""
        super().__init__()
        self._last = last
        self.setObjectName("shortcutRow")
        self.setMinimumHeight(65)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(14)
        info = QVBoxLayout()
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(2)
        info.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        title_line = QHBoxLayout()
        title_line.setSpacing(7)
        title = QLabel(command.display_title)
        title.setObjectName("shortcutRowTitle")
        title.setMinimumHeight(21)
        title_line.addWidget(title)
        self.custom = QLabel(tr("自定义"))
        self.custom.setObjectName("shortcutCustom")
        self.custom.setFixedHeight(22)
        self.custom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_line.addWidget(self.custom)
        title_line.addStretch()
        info.addLayout(title_line)
        description = QLabel(command.display_description)
        description.setObjectName("shortcutDescription")
        description.setMinimumHeight(20)
        description.setWordWrap(True)
        description.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        info.addWidget(description)
        layout.addLayout(info, 1)
        binding_cell = QWidget()
        self._binding_cell = binding_cell
        binding_cell.setFixedWidth(170)
        cell = QHBoxLayout(binding_cell)
        cell.setContentsMargins(0, 0, 0, 0)
        self.button = BindingButton(command.display_title, binding, self)
        self.button.set_column_width(170)
        self.button.focus_changed.connect(self.update)
        cell.addWidget(self.button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(binding_cell)
        self.restore_button = ShortcutButton(tr("还原"))
        self.restore_button.setObjectName("shortcutRestore")
        self.restore_button.setAccessibleName(tr("还原{title}的快捷键", title=command.display_title))
        self.restore_button.setFixedWidth(54)
        self.restore_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.restore_button.setEnabled(binding != command.default)
        layout.addWidget(self.restore_button)
        self.custom.setVisible(binding != command.default)

    def set_compact(self, compact, narrow=False):
        """按紧凑与窄屏状态调整行高、内边距及快捷键列宽，保持各行对齐。"""
        inset = 14 if narrow else 20
        width = 155 if narrow else 170
        self.setMinimumHeight(60 if compact else 65)
        self.layout().setContentsMargins(inset, 8 if compact else 10, inset, 8 if compact else 10)
        self.layout().setSpacing(10 if narrow else 14)
        self._binding_cell.setFixedWidth(width)
        self.button.set_column_width(width)

    def paintEvent(self, event):
        """绘制悬停或绑定按钮聚焦背景及分隔线，末行保留分组面板的底部圆角。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.underMouse() or self.button.hasFocus():
            painter.setPen(Qt.PenStyle.NoPen)
            fill = QColor(fluent.color("soft"))
            if theme.is_background_active():
                fill = QColor(theme.color("text.primary"))
                fill.setAlphaF(0.04)
            painter.setBrush(fill)
            if self._last:
                path = QPainterPath()
                path.addRoundedRect(0, -8, self.width(), self.height() + 8, 7, 7)
                painter.drawPath(path)
            else:
                painter.drawRect(self.rect())
        painter.fillRect(20, 0, max(0, self.width() - 40), 1, fluent.stroke_color())

    def enterEvent(self, event):
        """鼠标进入时请求重绘悬停背景，再交由基类处理进入事件。"""
        self.update()
        super().enterEvent(event)
