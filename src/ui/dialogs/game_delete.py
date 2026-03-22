"""游戏删除范围和清理结果的可读展示。"""

from __future__ import annotations

from threading import Event

from PySide6.QtCore import QEvent, QSize, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.ui import icons, theme
from src.ui.acrylic import apply_dialog_acrylic
from src.ui.dialog_chrome import center_dialog
from src.ui.worker_lifecycle import manage_page_worker
from src.utils.i18n import t as tr
from src.utils.logger import logger
from src.utils.qt_timers import call_later


class _GameDataScan(QThread):
    """在后台只读统计游戏数据，使用信号报告进度并保留取消或错误结果。"""

    progress = Signal(str, int, int, object)

    def __init__(self, work, parent):
        """保存可接收取消和进度回调的扫描函数，创建由父窗口持有的工作线程。"""
        super().__init__(parent)
        self.work = work
        self.cancelled = Event()
        self.result = None
        self.error = None

    def run(self):
        """注入协作取消检查执行扫描，取消单独标记，一般异常记录日志并留给界面处理。"""
        from src.core.game_data_summary import GameDataScanCancelled

        try:
            self.result = self.work(cancelled=self.cancelled.is_set, progress=self.progress.emit)
        except GameDataScanCancelled:
            self.cancelled.set()
        except Exception as error:
            self.error = error
            logger.exception("删除游戏前的数据扫描失败")

    def stop(self):
        """设置取消事件供后台扫描响应，不在界面线程等待扫描结束。"""
        self.cancelled.set()


def format_data_size(size: int) -> str:
    """按每级 1024 换算字节量并返回带单位文本，负数按零处理。"""
    amount = max(0, size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024


def _label(text: str, role: str = "", *, selectable: bool = False) -> QLabel:
    """创建带删除提示样式角色的自动换行纯文本标签，可允许鼠标选择复制。"""
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    widget.setProperty("deleteRole", role)
    if selectable:
        widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


class _GameDataDialog(QDialog):
    """提供游戏删除范围和结果提示共用的滚动内容区、主题样式及底部操作。"""

    def __init__(self, title: str, parent: QWidget | None):
        """创建模态窗口和可滚动内容布局，并应用当前主题样式。"""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setObjectName("gameDataDialog")
        self.setModal(True)
        self.setMinimumSize(580, 440)
        self.resize(700, 640)
        self._theme = theme.current_theme()
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self.body = QVBoxLayout(content)
        self.body.setContentsMargins(24, 20, 24, 20)
        self.body.setSpacing(16)
        scroll.setWidget(content)
        self._outer.addWidget(scroll, 1)
        self._retheme()

    def _heading(self, title: str, description: str):
        """向内容区添加警示图标、标题和说明文本。"""
        row = QHBoxLayout()
        symbol = QLabel()
        icons.apply_pixmap(symbol, "status/warning", 30, "danger")
        row.addWidget(symbol, 0, Qt.AlignmentFlag.AlignTop)
        row.addSpacing(6)
        text = QVBoxLayout()
        text.addWidget(_label(title, "title"))
        text.addWidget(_label(description, "muted"))
        row.addLayout(text, 1)
        self.body.addLayout(row)

    def _panel(self) -> QVBoxLayout:
        """在内容区添加带背景的分组面板，并返回其布局供调用方填充。"""
        panel = QFrame()
        panel.setProperty("deleteRole", "panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        self.body.addWidget(panel)
        return layout

    def _footer(self, *, destructive: bool = False):
        """添加默认取消或关闭操作及可选永久删除操作，约束屏幕尺寸并居中窗口。"""
        footer = QFrame()
        self._footer_widget = footer
        footer.setProperty("deleteRole", "footer")
        row = QHBoxLayout(footer)
        row.setContentsMargins(24, 14, 24, 14)
        row.setSpacing(10)
        row.addStretch()
        cancel = QPushButton(tr("取消") if destructive else tr("关闭"))
        self.cancel_button = cancel
        cancel.setMinimumHeight(34)
        cancel.setDefault(True)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        if destructive:
            delete = QPushButton(tr("永久删除"))
            delete.setObjectName("confirmGameDelete")
            delete.setProperty("btnColor", "danger")
            delete.setAutoDefault(False)
            delete.setMinimumHeight(34)
            delete.clicked.connect(self.accept)
            row.addWidget(delete)
        self._outer.addWidget(footer)
        apply_dialog_acrylic(self, tint_alpha=0.9)
        self._outer.activate()
        parent = self.parentWidget()
        screen = parent.window().screen() if parent is not None else self.screen()
        if screen is not None:
            available = screen.availableGeometry().size() - QSize(32, 32)
            self.setMinimumSize(self.minimumSize().boundedTo(available))
            self.resize(self.size().boundedTo(available))
        center_dialog(self)
        cancel.setFocus()

    def _retheme(self):
        """按当前主题重新生成游戏数据窗口的局部控件样式。"""
        c = theme.color
        self.setStyleSheet(f"""
            QDialog#gameDataDialog, #gameDataDialog QWidget {{ background: transparent; }}
            #gameDataDialog QLabel {{ color: {c("text.primary")}; font-size: 13px; }}
            #gameDataDialog QLabel[deleteRole="title"] {{ font-size: 22px; font-weight: 600; }}
            #gameDataDialog QLabel[deleteRole="muted"] {{ color: {c("text.secondary")}; }}
            #gameDataDialog QLabel[deleteRole="section"] {{ font-size: 16px; font-weight: 600; }}
            #gameDataDialog QLabel[deleteRole="strong"] {{ font-weight: 600; }}
            #gameDataDialog QLabel[deleteRole="number"] {{ font-size: 24px; font-weight: 600; }}
            #gameDataDialog QLabel[deleteRole="warning"] {{ color: {c("danger")}; }}
            #gameDataDialog QFrame[deleteRole="panel"] {{ background: {c("bg.card")};
                border: 1px solid {c("stroke.card")}; border-radius: 8px; }}
            #gameDataDialog QFrame[deleteRole="footer"] {{ background: {c("bg.subtle")};
                border-top: 1px solid {c("stroke.card")}; }}
            #gameDataDialog QScrollArea {{ border: none; }}
            #gameDataDialog QPlainTextEdit {{ background: {c("bg.card")}; color: {c("text.primary")};
                border: 1px solid {c("stroke.card")}; border-radius: 6px; padding: 10px; }}
            #gameDataDialog QPushButton {{ background: {c("control.bg")}; color: {c("text.primary")};
                border: 1px solid {c("stroke.control")}; border-radius: 5px; padding: 4px 16px; }}
            #gameDataDialog QPushButton:hover {{ background: {c("bg.hover")}; }}
            #gameDataDialog QPushButton:focus {{ border-color: {c("accent")}; }}
            #gameDataDialog QPushButton[btnColor="danger"] {{ background: #c42b1c;
                border-color: #c42b1c; color: white; }}
            #gameDataDialog QPushButton[btnColor="danger"]:hover {{ background: #a92316; }}
            #gameDataDialog QPushButton[btnColor="danger"]:pressed {{ background: #8f1e14; }}
        """)
