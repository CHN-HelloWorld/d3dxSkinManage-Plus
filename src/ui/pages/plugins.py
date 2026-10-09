"""插件管理页面，展示状态、目录、权限和错误，提供启停、重载及声明式设置。

插件变更通过管理器监听器刷新界面。
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import shiboken6
from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import (
    QBoxLayout,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.plugin_sdk.errors import PluginError
from src.ui import anim, icons, settings_style, theme
from src.ui.plugin_settings import PluginSettingsForm
from src.ui.smooth_scroll import SmoothScroller
from src.ui.widgets.tag_chip import TagFlowLayout
from src.ui.widgets.toggle_switch import ToggleSwitch
from src.utils.i18n import N_
from src.utils.i18n import t as tr
from src.utils.paths import get_plugins_dir

if TYPE_CHECKING:
    from src.plugins.runtime.manager import PluginManager
    from src.plugins.runtime.registry import PluginRecord


_STATE_TEXT = {
    "active": (N_("运行中"), "success"),
    "loaded": (N_("已加载"), "text.secondary"),
    "disabled": (N_("已停用"), "text.tertiary"),
    "discovered": (N_("待激活"), "text.tertiary"),
    "error": (N_("出错"), "danger"),
    "ready": (N_("待命"), "text.secondary"),
    "activating": (N_("正在激活"), "text.secondary"),
    "blocked": (N_("依赖受阻"), "warning"),
}

_PERMISSION_LABELS = {
    "mods:read": N_("读取 Mod"),
    "mods:write": N_("修改 Mod"),
    "mods:manage": N_("管理 Mod"),
    "storage": N_("存储设置"),
    "ui:tools-page": N_("工具箱入口"),
    "ui:menus": N_("右键菜单"),
    "ui:settings": N_("设置控件"),
    "ui:views": N_("插件视图"),
    "ui:shortcuts": N_("命令快捷键"),
    "network": N_("网络访问"),
    "fs:read": N_("读取文件"),
    "fs:write": N_("写入文件"),
    "background": N_("后台运行"),
    "process": N_("启动进程"),
    "native": N_("加载原生代码"),
}


def _permission_label(permission: str) -> str:
    """将已知权限标识转换为本地化名称，未知权限使用统一说明。"""
    return tr(_PERMISSION_LABELS.get(permission, N_("未识别权限")))


def _action_button(text: str, icon: str) -> QPushButton:
    """创建带主题图标的插件操作按钮，并设置可点击的光标。"""
    button = QPushButton(text)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    icons.apply_icon(button, icon)
    button.setIconSize(QSize(16, 16))
    return button


def _plain_widget() -> QWidget:
    """创建插件页使用的透明内容容器，避免继承面板背景和边框。"""
    widget = QWidget()
    widget.setObjectName("pluginPlain")
    widget.setStyleSheet("QWidget#pluginPlain { background: transparent; border: none; }")
    return widget


class _PluginEntry(QPushButton):
    """可选择的插件列表行，活动插件内容由详情面板持有。"""

    def __init__(self, record, parent=None):
        """创建可选中的插件列表项，登记插件身份，并使图标和名称的点击由整行接收。"""
        super().__init__(parent)
        self.setObjectName("pluginEntry")
        self.setProperty("pluginId", record.id)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(11)
        self._icon = QLabel()
        self._icon.setObjectName("pluginAvatar")
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon.setFixedSize(28, 28)
        self._icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self._name = QLabel()
        self._name.setTextFormat(Qt.TextFormat.PlainText)
        self._name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._name.installEventFilter(self)
        layout.addWidget(self._name, 1)
        self.update_record(record)

    def update_record(self, record):
        """替换列表项记录，同步无障碍名称、提示及主题样式，再按可用宽度省略名称。"""
        self._record = record
        self.setAccessibleName(record.name)
        self.setToolTip(f"{record.name}\n{record.description}")
        self.retheme()
        self._elide()

    def retheme(self):
        """刷新插件列表项的选中边框、悬停背景、名称颜色和主题图标。"""
        c = settings_style.color
        self.setStyleSheet(f"""
            QPushButton#pluginEntry {{ background: transparent; border: 1px solid transparent;
                border-radius: 7px; padding: 0; text-align: left; min-height: 56px; }}
            QPushButton#pluginEntry:hover {{ background: {theme.rgba("text.primary", 0.06)}; }}
            QPushButton#pluginEntry:checked {{ background: {settings_style.surface(over="nav")};
                border: 1px solid {settings_style.stroke()}; border-left: 3px solid {c("accent")}; }}
            QLabel#pluginAvatar {{ background: transparent; border: none; }}
        """)
        icons.apply_pixmap(self._icon, "navigation/plugins", 23, "settings.accent")
        self._name.setStyleSheet(f"background: transparent; color: {c('text')}; font-size: 14px; font-weight: 600;")

    def _elide(self):
        """在字体样式生效后按名称标签的实际宽度生成尾部省略文本。"""
        self._name.ensurePolished()
        self._name.setText(
            self._name.fontMetrics().elidedText(
                self._record.name, Qt.TextElideMode.ElideRight, max(0, self._name.contentsRect().width())
            )
        )
