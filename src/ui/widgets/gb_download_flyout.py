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

    def _compose_subtitle(self) -> str:
        """格式化速度、已下载和总大小以及剩余时间或任务状态。

        总大小未知时省略大小比值；剩余时间使用分秒或时分秒格式。
        """
        d, t = self._task.last_progress
        state = self._task.state
        sizes = f"{humanize_size(d)}/{humanize_size(t)}" if t > 0 else ""

        if state == "downloading":
            spd = _format_speed(getattr(self._task, "speed_bps", 0.0)) or "0 B/s"
            eta = _format_eta(getattr(self._task, "eta_seconds", -1.0))
            tail = tr("剩余 {eta}", eta=eta) if eta else tr("下载中")
            return f"{spd} - {sizes}, {tail}" if sizes else f"{spd}, {tail}"
        if state == "paused":
            # 暂停时显示零速度及暂停状态。
            return tr("0 B/s - {sizes}, 已暂停", sizes=sizes) if sizes else tr("0 B/s, 已暂停")
        if state == "queued":
            return tr("排队中")
        if state == "preparing":
            # 等待响应头时总长度未知，省略无效的大小比值。
            return tr("准备中")
        if state == "done":
            try:
                size = self._task.archive_path.stat().st_size if self._task.archive_path.exists() else 0
            except OSError:
                size = 0
            return tr("已完成 · {value}", value=humanize_size(size)) if size else tr("已完成")
        if state == "failed":
            return tr("失败")
        if state == "canceled":
            return tr("已取消")
        return state

    def _sync_with_state(self) -> None:
        """按任务状态重绑定主操作按钮、进度模式和取消入口，避免累积重复的点击连接。"""
        state = self._task.state
        d, t = self._task.last_progress
        pct = int(d * 100 / t) if t > 0 else 0
        self.sub_label.setText(self._compose_subtitle())

        # 主按钮（环形进度 + 图标）：先断开旧 clicked，避免状态切换后多次连接叠加。
        if self.primary_btn.isSignalConnected(QMetaMethod.fromSignal(self.primary_btn.clicked)):
            self.primary_btn.clicked.disconnect()

        # 状态变化时同步不确定进度模式，避免离开准备状态后仍持续旋转。
        self.primary_btn.set_indeterminate(state == "preparing")

        # 取消按钮仅在「活跃」态显示（preparing/downloading/paused/queued）。已结束的
        # 任务（done/failed/canceled）不再有取消语义——主按钮才是「打开/重试」。
        self.cancel_btn.setVisible(state in ("preparing", "downloading", "paused", "queued"))

        if state == "downloading":
            self.primary_btn.set_icon("actions/pause", "text.primary")
            self.primary_btn.set_ring_color("accent")
            self.primary_btn.set_progress(pct)
            self.primary_btn.setEnabled(True)
            self.primary_btn.setToolTip(tr("暂停"))
            self.primary_btn.clicked.connect(self._on_pause)
        elif state == "preparing":
            # 准备期间显示不确定进度，仍允许暂停和取消。
            self.primary_btn.set_icon("actions/pause", "text.primary")
            self.primary_btn.set_ring_color("accent")
            self.primary_btn.setEnabled(True)
            self.primary_btn.setToolTip(tr("暂停"))
            self.primary_btn.clicked.connect(self._on_pause)
        elif state == "queued":
            # 排队时禁用主按钮，保留取消操作。
            self.primary_btn.set_icon("actions/pause", "text.tertiary")
            self.primary_btn.set_progress(-1)
            self.primary_btn.setEnabled(False)
            self.primary_btn.setToolTip(tr("排队中"))
        elif state == "paused":
            self.primary_btn.set_icon("actions/play", "text.primary")
            self.primary_btn.set_ring_color("text.tertiary")  # 暂停态进度环淡化
            self.primary_btn.set_progress(pct)
            self.primary_btn.setEnabled(True)
            self.primary_btn.setToolTip(tr("继续"))
            self.primary_btn.clicked.connect(self._on_resume)
        elif state == "done":
            self.primary_btn.set_icon("actions/open", "text.primary")
            self.primary_btn.set_progress(-1)
            self.primary_btn.setEnabled(self._can_open_mod(self._task))
            self.primary_btn.setToolTip(tr("打开（进入 Mod 类别）"))
            self.primary_btn.clicked.connect(self._on_open_mod)
        else:  # failed / canceled
            self.primary_btn.set_icon("actions/refresh", "text.primary")
            self.primary_btn.set_progress(-1)
            self.primary_btn.setEnabled(True)
            self.primary_btn.setToolTip(tr("重试"))
            self.primary_btn.clicked.connect(self._on_retry)

    # 信号回调
    def _on_progress(self, d: int, t: int) -> None:
        """更新活动下载的百分比与信息文案，记录行已销毁时忽略迟到信号。"""
        try:
            if t > 0 and self._task.state in ("downloading", "paused"):
                self.primary_btn.set_progress(int(d * 100 / t))
            self.sub_label.setText(self._compose_subtitle())
        except RuntimeError:
            pass

    def _on_state_changed(self, _state: str) -> None:
        """按最新任务状态刷新行操作，容忍任务信号到达时控件已销毁。"""
        try:
            self._sync_with_state()
        except RuntimeError:
            pass

    # 操作槽
    def _on_pause(self) -> None:
        """向下载管理器请求暂停该文件任务。"""
        self._manager.pause(self._task.file.file_id)

    def _retain_download_consumer(self) -> None:
        """以下载界面所有者登记文件占用，供继续或重试任务时保留共享下载产物。"""
        file = self._task.file
        self._manager.retain(
            file.file_id,
            file.md5_checksum,
            owner=f"download:{file.file_id}",
            path=self._task.archive_path,
        )

    def _on_resume(self) -> None:
        """重新登记下载文件的使用者后请求继续任务。"""
        self._retain_download_consumer()
        self._manager.resume(self._task.file.file_id)

    def _on_cancel(self) -> None:
        """向下载管理器请求取消该文件任务。"""
        self._manager.cancel(self._task.file.file_id)
