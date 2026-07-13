"""显示可按游戏筛选的下载任务，提供暂停、继续、取消及记录清理入口。

任务及文件操作交由下载管理器处理，打开资源通过信号交由页面导航。
浮层作为不接受焦点的工具窗口显示，通过应用事件过滤器处理 Esc 和外部点击。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QMetaMethod,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QCursor, QFontMetrics
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.services.gamebanana.downloader import GbDownloadManager, GbDownloadTask
from src.services.gamebanana.format import humanize_size
from src.ui import anim, icons, theme
from src.ui.widgets.ring_progress import RingProgressButton
from src.ui.win_effects import acrylic_surface_alpha
from src.utils.i18n import t as tr

# 固定面板宽度，为进度、文件名及操作按钮预留稳定空间。
_FLYOUT_WIDTH = 440


def _open_in_explorer(path: Path) -> None:
    """在系统资源管理器中选中文件（Windows: /select,；其他平台退化为打开父目录）。"""
    try:
        if not path.exists():
            return
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
    except Exception:
        pass


def _format_speed(bps: float) -> str:
    """字节/秒 → 「x.x MB/s」；非正速度返回空串。"""
    if not bps or bps <= 0:
        return ""
    return f"{humanize_size(int(bps))}/s"


def _format_eta(seconds: float) -> str:
    """剩余秒 → 「mm:ss」或「h:mm:ss」；未知(<0)返回空串。"""
    if seconds is None or seconds < 0:
        return ""
    s = int(seconds + 0.5)
    if s >= 3600:
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}"
    m, sec = divmod(s, 60)
    return f"{m:02d}:{sec:02d}"


class _DownloadRow(QFrame):
    """展示单条下载任务的进度及可用操作，向宿主转发资源定位和文件打开请求。"""

    open_mod_requested = Signal(int)
    open_file_requested = Signal(object)
    request_refresh = Signal()

    def __init__(self, manager: GbDownloadManager, task: GbDownloadTask, parent=None, *, can_open_mod=None) -> None:
        """绑定下载任务与管理器，创建可换行的文件信息及固定操作区，并接收任务状态变化。"""
        super().__init__(parent)
        self._manager = manager
        self._task = task
        self._can_open_mod = can_open_mod or (lambda task: task.archive_path.is_file())
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        # 行高随文件名换行后的高度变化。
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        # 首行排列文件名和操作按钮，下载状态独占第二行宽度。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(3)

        # 上行：文件名（左，可换行，占满剩余宽度）+ 右侧操作按钮组
        top = QHBoxLayout()
        top.setSpacing(8)
        outer.addLayout(top)

        # 长文件名按布局分配的宽度换行，高度随内容增长。
        # 最小宽度设为零并忽略宽度提示，避免文件名挤出操作按钮。
        self.name_label = QLabel(task.file.file_name or "")
        self.name_label.setStyleSheet("font-weight:600;")
        self.name_label.setWordWrap(True)
        self.name_label.setMinimumWidth(0)
        self.name_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Minimum,
        )
        # 悬停时显示完整文件名。
        self.name_label.setToolTip(task.file.file_name or "")
        # TextInteractionFlags 让用户可以选择/复制文件名文本
        self.name_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # 禁用文件名标签的默认文本菜单，让右键事件交给下载行统一处理。
        self.name_label.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        top.addWidget(self.name_label, 1)

        # 右侧按钮组：主按钮 + 取消（下载中/暂停时显示）+ ⋯
        # 主按钮在下载或暂停时显示进度环及暂停、继续图标，
        # 完成或失败时显示打开、重试图标；操作说明保留在工具提示中。
        self.primary_btn = RingProgressButton(size=30, line_width=3)
        top.addWidget(self.primary_btn)

        # 活动任务使用取消图标按钮，工具提示提供文字说明。
        self.cancel_btn = QToolButton()
        icons.apply_icon(self.cancel_btn, "actions/dismiss", "text.secondary")
        self.cancel_btn.setIconSize(QSize(16, 16))
        self.cancel_btn.setAutoRaise(True)
        self.cancel_btn.setToolTip(tr("取消"))
        self.cancel_btn.clicked.connect(self._on_cancel)
        top.addWidget(self.cancel_btn)

        self.menu_btn = QToolButton()
        icons.apply_icon(self.menu_btn, "actions/more")
        self.menu_btn.setIconSize(QSize(16, 16))
        self.menu_btn.setAutoRaise(True)
        self.menu_btn.clicked.connect(self._show_menu_at_button)
        top.addWidget(self.menu_btn)

        # 按钮顶端对齐，避免多行文件名改变其垂直位置。
        top.setAlignment(self.primary_btn, Qt.AlignmentFlag.AlignTop)
        top.setAlignment(self.cancel_btn, Qt.AlignmentFlag.AlignTop)
        top.setAlignment(self.menu_btn, Qt.AlignmentFlag.AlignTop)

        # 下载信息独占一行，不换行。
        self.sub_label = QLabel(self._compose_subtitle())
        self.sub_label.setStyleSheet(f"color:{theme.color('text.tertiary')}; font-size:9pt;")
        self.sub_label.setWordWrap(False)
        self.sub_label.setMinimumWidth(0)
        self.sub_label.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        outer.addWidget(self.sub_label)

        # 连接任务信号。
        task.progress.connect(self._on_progress)
        task.state_changed.connect(self._on_state_changed)

        # 按当前状态初始化按钮，包括恢复的已完成记录。
        self._sync_with_state()

    def resizeEvent(self, event):
        """根据可用宽度计算文件名换行后的行高。

        水平布局不会自动按 heightForWidth 调整标签高度，因此需显式更新最小高度，
        避免多行文本被裁切。
        """
        super().resizeEvent(event)
        self._adjust_name_label_height()

    def _adjust_name_label_height(self) -> None:
        """按文件名标签的当前宽度计算换行高度，并更新最小高度。"""
        try:
            avail = self.name_label.width()
        except RuntimeError:
            return
        if avail <= 0:
            return
        text = self._task.file.file_name or ""
        if not text:
            return
        fm = QFontMetrics(self.name_label.font())
        rect = fm.boundingRect(
            0,
            0,
            avail,
            10000,
            int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignLeft),
            text,
        )
        # 行高至少 1 行——避免 boundingRect 在极小宽度下返回 0
        h = max(fm.height(), rect.height())
        if self.name_label.minimumHeight() != h:
            self.name_label.setMinimumHeight(h)

    # 状态同步
    def retheme(self) -> None:
        """主题切换时更新次要文字颜色及环形按钮图标，其他图标由统一主题刷新处理。"""
        self.sub_label.setStyleSheet(f"color:{theme.color('text.tertiary')}; font-size:9pt;")
        try:
            self.primary_btn.retheme()
        except Exception:
            pass
