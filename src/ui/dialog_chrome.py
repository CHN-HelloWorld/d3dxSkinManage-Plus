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

    def _set_title(self, title):
        """同步窗口标题与悬浮提示，空标题回退应用名并更新关闭按钮无障碍名称。"""
        self.title_label.setText(title or APP_DISPLAY_NAME)
        self.title_label.setToolTip(title)
        self.close_button.setAccessibleName(tr("关闭{value}", value=title or APP_DISPLAY_NAME))

    def _set_icon(self, icon):
        """按当前屏幕缩放将窗口图标绘制到标题栏的固定逻辑尺寸。"""
        self.icon_label.setPixmap(icon.pixmap(QSize(16, 16), self.devicePixelRatioF()))

    def retheme(self):
        """根据对话框材质重建标题栏样式，并刷新关闭图标。"""
        fill = "transparent" if self.window().property("_dialog_acrylic") else _color("surface")
        self.setStyleSheet(f"""
            QWidget#dialogTitleBar {{ background: {fill}; border: 0; }}
            QLabel {{ color: {_color("secondary")}; background: transparent;
                     font-size: 11px; border: 0; padding: 0; margin: 0; }}
            QPushButton {{ background: transparent; border: 0; border-radius: 0;
                          padding: 0; margin: 0; min-width: 42px; max-width: 42px;
                          min-height: 30px; max-height: 30px; }}
            QPushButton:hover {{ background: #c42b1c; }}
            QPushButton:focus {{ background: transparent; border: 0; outline: 0; }}
            QPushButton:hover:focus {{ background: #c42b1c; }}
            QPushButton:disabled {{ background: transparent; }}
        """)
        self.close_button.refresh_icon()

    def mousePressEvent(self, event):
        """保存拖动基准，Win10 原生指针使用合并移动，其他情况保留系统移动。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._end_drag()
            self._drag_origin = event.globalPosition().toPoint()
            self._window_origin = self.window().pos()
            from src.ui import win_effects

            self._coalesced_move = (
                event.spontaneous()
                and QApplication.platformName() == "windows"
                and win_effects.needs_popup_paint_before_blur()
            )
            if self._coalesced_move:
                from src.ui.native_resize import begin_native_resize

                self._native_move = begin_native_resize(self.window())
            else:
                # 系统移动可能接受请求却未接管，继续送达标题栏的事件仍须移动窗口。
                handle = self.window().windowHandle()
                if handle is not None:
                    handle.startSystemMove()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """合并 Win10 系统指针事件，避免窗口依次追赶已经过期的位置。"""
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if self._coalesced_move:
                self._pending_move = event.globalPosition().toPoint()
                self._pending_native_move = event.spontaneous()
                if not self._move_timer.isActive():
                    self._move_timer.start(0)
            else:
                self.window().move(self._window_origin + event.globalPosition().toPoint() - self._drag_origin)
            event.accept()
        else:
            self._end_drag()
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """提交释放位置并取消排队更新，避免松开后仍继续移动。"""
        if event.button() == Qt.MouseButton.LeftButton and self._coalesced_move:
            self._move_timer.stop()
            self._pending_move = event.globalPosition().toPoint()
            self._pending_native_move = False
            self._apply_pending_move(finish=True)
        self._end_drag()
        super().mouseReleaseEvent(event)

    def _apply_pending_move(self, *, finish: bool = False) -> None:
        """以最新光标计算目标位置，复用原生几何基准并保留窗口尺寸与层级。"""
        position = self._pending_move
        if position is not None and self._pending_native_move:
            position = QCursor.pos()
        self._pending_move = None
        self._pending_native_move = False
        if self._drag_origin is None or position is None:
            return
        window = self.window()
        target = self._window_origin + position - self._drag_origin
        if not finish and self._native_move is not None:
            geometry = QRect(window.geometry())
            geometry.moveTopLeft(target)
            if self._native_move.apply(window, geometry):
                return
            from src.ui.native_resize import begin_native_resize

            window.move(target)
            self._native_move = begin_native_resize(window)
            return
        window.move(target)

    def hideEvent(self, event):
        """标题栏隐藏时终止拖动，防止位置基准残留。"""
        self._end_drag()
        super().hideEvent(event)
