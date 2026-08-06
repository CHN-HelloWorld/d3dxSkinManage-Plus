"""应用统一的对话框边框。

保留原对话框布局和关闭处理，标题栏空间独立于 Qt 内部布局。
"""

from __future__ import annotations

import shiboken6
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QCursor, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui import icons, theme
from src.utils.constants import APP_DISPLAY_NAME
from src.utils.i18n import t as tr
from src.utils.paths import get_resources_dir


def _color(name: str) -> str:
    """读取对话框共享色板中的指定语义颜色。"""
    return theme.color(f"mod_updates.{name}")


def center_dialog(dialog: QDialog) -> None:
    """相对所属应用窗口居中，并保持边框位于屏幕内。"""
    parent = dialog.parentWidget()
    owner = parent.window() if parent is not None else None
    screen = owner.screen() if owner is not None else dialog.screen()
    if screen is None:
        return
    available = screen.availableGeometry()
    frame = dialog.frameGeometry()
    frame.moveCenter(owner.frameGeometry().center() if owner is not None else available.center())
    frame.moveLeft(max(available.left(), min(frame.left(), available.right() - frame.width() + 1)))
    frame.moveTop(max(available.top(), min(frame.top(), available.bottom() - frame.height() + 1)))
    dialog.move(frame.topLeft())


class _CloseButton(QPushButton):
    """随悬停与主题状态切换图标颜色的标题栏关闭按钮。"""

    def refresh_icon(self, hovered=False):
        """按悬停状态设置白色或主题次要文字色的关闭图标。"""
        self.setIcon(
            QIcon(icons.colored_pixmap_from("mod-updates/close", 30, "#ffffff" if hovered else _color("secondary")))
        )

    def showEvent(self, event):
        # 点击关闭可能不触发离开事件，复用标题栏前清理悬停状态。
        """显示时刷新关闭图标，使重新打开的对话框跟随当前主题。"""
        self.refresh_icon()
        super().showEvent(event)

    def enterEvent(self, event):
        """鼠标进入时将关闭图标切为高对比颜色。"""
        self.refresh_icon(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        """鼠标离开时恢复关闭图标的主题颜色。"""
        self.refresh_icon()
        super().leaveEvent(event)


class DialogTitleBar(QWidget):
    """与对话框材质一致的紧凑标题栏。"""

    def __init__(self, window: QDialog):
        """创建图标、标题和关闭按钮，并绑定所属窗口标题、图标及关闭动作。"""
        super().__init__(window)
        self.setObjectName("dialogTitleBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setFixedHeight(31)
        self._drag_origin = None
        self._window_origin = QPoint()
        self._coalesced_move = False
        self._pending_move = None
        self._pending_native_move = False
        self._native_move = None
        self._move_timer = QTimer(self)
        self._move_timer.setSingleShot(True)
        self._move_timer.timeout.connect(self._apply_pending_move)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 0, 0)
        layout.setSpacing(7)
        self.icon_label = QLabel()
        self.icon_label.setObjectName("dialogWindowIcon")
        self.icon_label.setFixedSize(16, 16)
        self.title_label = QLabel()
        self.title_label.setObjectName("dialogWindowTitle")
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        for label in (self.icon_label, self.title_label):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(label, 1 if label is self.title_label else 0)
        self.close_button = _CloseButton()
        self.close_button.setObjectName("dialogClose")
        self.close_button.setAutoDefault(False)
        self.close_button.setFixedSize(42, 30)
        self.close_button.setIconSize(QSize(15, 15))
        self.close_button.setToolTip(tr("关闭"))
        # 调用关闭以保留任务窗口拒绝关闭的能力，并让消息框处理自己的取消按钮。
        self.close_button.clicked.connect(window.close)
        layout.addWidget(self.close_button)
        window.windowTitleChanged.connect(self._set_title)
        window.windowIconChanged.connect(self._set_icon)
        self._set_title(window.windowTitle())
        icon = window.windowIcon()
        if icon.isNull():
            icon = QIcon(str(get_resources_dir() / "app.ico"))
        window.setWindowIcon(icon)
        self._set_icon(icon)
        self.retheme()
