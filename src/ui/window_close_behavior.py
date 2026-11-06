"""根据关闭设置管理系统托盘入口，真正退出时仍由主窗口处理业务清理。"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon, QWidget

from src.core.config import Config
from src.utils.constants import APP_DISPLAY_NAME
from src.utils.i18n import t as tr


class WindowCloseBehavior(QObject):
    """延迟创建托盘图标，保留主窗口恢复入口和显式退出操作。"""

    def __init__(self, window: QWidget, config: Config) -> None:
        """绑定主窗口和即时配置，默认不拦截正常退出。"""
        super().__init__(window)
        self.window = window
        self.config = config
        self.tray: QSystemTrayIcon | None = None
        self._menu: QMenu | None = None
        self._exit_requested = False
        QApplication.instance().aboutToQuit.connect(self.dispose)

    def handle_close(self, event: QCloseEvent) -> bool:
        """托盘可用且未请求退出时隐藏窗口；否则保留原有关闭流程。"""
        if (
            self._exit_requested
            or self.window.property("applicationExitRequested")
            or self.config.get("ui.close_behavior", "exit") != "tray"
        ):
            return False
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False
        if self.tray is None:
            icon = self.window.windowIcon()
            if icon.isNull():
                icon = QApplication.windowIcon()
            if icon.isNull():
                icon = self.window.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
            self.tray = QSystemTrayIcon(icon, self)
            self.tray.setToolTip(APP_DISPLAY_NAME)
            self._menu = QMenu(self.window)
            self._menu.addAction(tr("显示主窗口"), self.restore)
            self._menu.addSeparator()
            self._menu.addAction(tr("退出程序"), self.request_exit)
            self.tray.setContextMenu(self._menu)
            self.tray.activated.connect(self._on_activated)
        self.tray.show()
        event.ignore()
        self.window.hide()
        return True

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """单击或双击托盘图标时恢复主窗口，右键保留系统菜单。"""
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.restore()

    def restore(self) -> None:
        """保持最大化状态恢复窗口，并重新激活前台焦点。"""
        self._exit_requested = False
        if self.window.isMinimized():
            if self.window.windowState() & Qt.WindowState.WindowMaximized:
                self.window.showMaximized()
            else:
                self.window.showNormal()
        else:
            self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def request_exit(self) -> None:
        """绕过隐藏行为发起正常关闭，仍保留未保存内容和后台任务确认。"""
        self.restore()
        self._exit_requested = True
        self.window.close()

    def cancel_exit(self) -> None:
        """正常关闭被取消后恢复配置指定的关闭行为。"""
        self._exit_requested = False

    def dispose(self) -> None:
        """程序真正退出时移除托盘图标，避免留下失效入口。"""
        if self.tray is not None:
            self.tray.hide()
