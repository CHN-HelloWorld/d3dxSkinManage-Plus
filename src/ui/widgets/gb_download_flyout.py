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

    def _on_retry(self) -> None:
        """重新登记下载文件的使用者后请求重试任务。"""
        self._retain_download_consumer()
        self._manager.retry(self._task.file.file_id)

    def _on_open_mod(self) -> None:
        """核验打开条件后转发站点资源编号和下载任务，条件失效时刷新按钮状态。"""
        if not self._can_open_mod(self._task):
            self._sync_with_state()
            return
        mod_id = getattr(self._task.profile, "mod_id", 0)
        if mod_id:
            self.open_mod_requested.emit(int(mod_id))
            self.open_file_requested.emit(self._task)

    def _on_show_in_explorer(self) -> None:
        """请求文件资源管理器定位该任务的归档路径。"""
        _open_in_explorer(self._task.archive_path)

    def _on_copy_link(self) -> None:
        """将非空下载地址复制到系统剪贴板。"""
        url = getattr(self._task.file, "download_url", "") or ""
        if url:
            QApplication.clipboard().setText(url)

    def _on_copy_name(self) -> None:
        """将任务文件名复制到系统剪贴板，缺少名称时使用空字符串。"""
        QApplication.clipboard().setText(self._task.file.file_name or "")

    def _on_delete_record(self) -> None:
        """以保留本地文件的方式请求删除任务记录，并通知列表刷新。"""
        # 仅删除任务记录，保留磁盘文件。
        self._manager.delete_task(self._task.file.file_id, delete_file=False)
        self.request_refresh.emit()

    def _on_delete_file_and_record(self) -> None:
        """请求管理器删除任务记录及对应本地文件，并通知列表刷新。"""
        # 删除记录并清理对应文件。
        self._manager.delete_task(self._task.file.file_id, delete_file=True)
        self.request_refresh.emit()

    # 右键菜单
    def _build_menu(self) -> QMenu:
        """按任务状态和完整或部分文件的存在情况构建可用操作菜单。"""
        menu = QMenu(self)
        state = self._task.state
        path = self._task.archive_path
        # 进行中的任务仅有部分下载文件，删除可用性不能只检查最终归档路径。
        part = path.with_suffix(path.suffix + ".part")
        has_product = path.exists()
        has_file = has_product or part.exists()

        if state in ("downloading", "preparing"):
            act = QAction(icons.get_icon("actions/pause"), tr("暂停"), menu)
            act.triggered.connect(self._on_pause)
            menu.addAction(act)
            act = QAction(icons.get_icon("actions/dismiss"), tr("取消"), menu)
            act.triggered.connect(self._on_cancel)
            menu.addAction(act)
        elif state == "queued":
            # 排队任务也允许取消。
            act = QAction(icons.get_icon("actions/dismiss"), tr("取消"), menu)
            act.triggered.connect(self._on_cancel)
            menu.addAction(act)
        elif state == "paused":
            act = QAction(icons.get_icon("actions/play"), tr("继续"), menu)
            act.triggered.connect(self._on_resume)
            menu.addAction(act)
            act = QAction(icons.get_icon("actions/dismiss"), tr("取消"), menu)
            act.triggered.connect(self._on_cancel)
            menu.addAction(act)
        elif state in ("failed", "canceled"):
            act = QAction(icons.get_icon("actions/refresh"), tr("重试"), menu)
            act.triggered.connect(self._on_retry)
            menu.addAction(act)
        elif state == "done":
            act = QAction(icons.get_icon("actions/open"), tr("打开（进入 Mod 类别）"), menu)
            act.triggered.connect(self._on_open_mod)
            act.setEnabled(self._can_open_mod(self._task))
            menu.addAction(act)

        menu.addSeparator()
        act = QAction(icons.get_icon("actions/folder-open"), tr("在文件资源管理器中显示"), menu)
        # 只有完成文件存在时才能在文件资源管理器中定位。
        act.setEnabled(has_product)
        act.triggered.connect(self._on_show_in_explorer)
        menu.addAction(act)

        menu.addSeparator()
        act = QAction(icons.get_icon("actions/copy"), tr("复制下载链接"), menu)
        act.setEnabled(bool(getattr(self._task.file, "download_url", "")))
        act.triggered.connect(self._on_copy_link)
        menu.addAction(act)
        act = QAction(icons.get_icon("actions/copy"), tr("复制文件名"), menu)
        act.triggered.connect(self._on_copy_name)
        menu.addAction(act)

        menu.addSeparator()
        act = QAction(icons.get_icon("actions/dismiss"), tr("从列表中删除"), menu)
        act.triggered.connect(self._on_delete_record)
        menu.addAction(act)
        act = QAction(icons.get_icon("actions/remove"), tr("删除（含本地文件）"), menu)
        act.setEnabled(has_file)
        act.triggered.connect(self._on_delete_file_and_record)
        menu.addAction(act)

        return menu

    def _on_context_menu(self, _pos) -> None:
        """在当前鼠标屏幕位置打开该记录的操作菜单。"""
        self._build_menu().exec(QCursor.pos())

    def _show_menu_at_button(self) -> None:
        """将记录操作菜单定位到更多按钮下方。"""
        self._build_menu().exec(self.menu_btn.mapToGlobal(self.menu_btn.rect().bottomLeft()))


class GbDownloadFlyout(QFrame):
    """管理下载记录浮层的筛选、显隐和操作转发，实际任务生命周期由下载管理器负责。"""

    mod_open_requested = Signal(int)
    file_open_requested = Signal(object)

    def __init__(self, manager: GbDownloadManager, parent=None) -> None:
        """创建保持主窗口焦点的下载浮层，连接任务队列变化并建立按游戏范围清理的操作入口。"""
        super().__init__(parent)
        self._manager = manager
        self.can_open_mod = None
        # 游戏编号为空时显示全部任务，否则按所属游戏精确筛选。
        self._game_id_filter: str | None = None
        # 收起标记防止动画重入，持有动画对象以避免提前回收。
        self._closing = False
        self._show_anim: QParallelAnimationGroup | None = None
        self._hide_anim: QParallelAnimationGroup | None = None
        self.setObjectName("downloadFlyout")
        # 顶层窗口仅提供透明画布，边框由内部框架绘制。
        self.setFrameShape(QFrame.Shape.NoFrame)
        # 固定浮层宽度，防止长文件名扩张窗口并使按钮超出可视范围。
        self.setFixedWidth(_FLYOUT_WIDTH)
        self.setMinimumHeight(320)
        self.setMaximumHeight(560)
        # 窗口标志分别用于独立显示、隐藏标题栏和保持主窗口激活状态。
        # WindowDoesNotAcceptFocus 避免自动显示下载浮窗时转移激活及键盘焦点，
        # 以便主窗口继续接收下一次点击。
        # 外部点击由全局过滤器处理，保留锚点按钮本次点击的切换行为。
        self.setWindowFlags(
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        # 浮窗不接受键盘焦点，Esc 由全局事件过滤器处理。
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # 透明顶层窗口的 QSS 边框不能可靠绘制，背景、边框和圆角统一交给内部框架。
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        self._inner = QFrame(self)
        self._inner.setObjectName("downloadFlyoutInner")
        self._apply_inner_style()
        shell.addWidget(self._inner)

        outer = QVBoxLayout(self._inner)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel(tr("下载管理"))
        title.setStyleSheet("font-weight:bold; font-size:11pt;")
        header.addWidget(title, 1)
        self.btn_clear_done = QPushButton(tr("清除已完成"))
        # 清理操作使用当前游戏编号，保持作用范围与列表筛选一致。
        self.btn_clear_done.clicked.connect(lambda: self._manager.clear_completed(game_id=self._game_id_filter))
        header.addWidget(self.btn_clear_done)
        self.btn_clear_all = QPushButton(tr("全部清除"))
        self.btn_clear_all.clicked.connect(
            lambda: self._manager.clear_all(
                delete_files=False,
                game_id=self._game_id_filter,
            )
        )
        header.addWidget(self.btn_clear_all)
        outer.addLayout(header)

        # 滚动区和行容器按行控件的 sizeHint 计算布局。
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        # 禁用横向滚动并省略长文件名，保证操作按钮始终位于可见区域。
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._rows_container = QWidget()
        self._rows_container.setStyleSheet("background: transparent;")
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(4)
        self._rows_layout.addStretch(1)
        self.scroll.setWidget(self._rows_container)
        outer.addWidget(self.scroll, 1)

        self.empty_label = QLabel(tr("（暂无下载记录）"))
        self.empty_label.setStyleSheet(f"color:{theme.color('text.tertiary')}; padding:24px;")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.empty_label)

        self._row_widgets: dict[int, _DownloadRow] = {}

        manager.queue_changed.connect(self._refresh)
        manager.task_added.connect(lambda _t: self._refresh())
        manager.task_removed.connect(lambda _fid: self._refresh())
        self._refresh()

        # 点击浮窗和锚点之外的区域时由全局过滤器收起，
        # 保留目标控件自身的点击事件。
        self._anchor_widget: QWidget | None = None
        # 全局监听由 showEvent/hideEvent 管理，隐藏时不进入 Python 回调。

    def show_anchored_to(self, anchor: QWidget) -> None:
        """设置外部点击检测排除的锚点控件，使按钮能够直接切换浮窗。"""
        self._anchor_widget = anchor

    def _ensure_acrylic(self) -> None:
        """窗口显示后，为透明顶层窗口施加亚克力模糊背景和圆角。"""
        try:
            from src.ui import win_effects

            win_effects.apply_rounded_corners(self)
            win_effects.apply_acrylic_blurbehind(self, dark=(theme.current_theme() == "dark"))
        except Exception:
            pass

    def showEvent(self, event):
        """显示前后同步任务行状态，注册外部点击过滤并应用窗口材质。"""
        super().showEvent(event)
        for row in self._row_widgets.values():
            row._sync_with_state()
        QApplication.instance().installEventFilter(self)
        self._ensure_acrylic()

    def hideEvent(self, event):
        """隐藏时解除应用级事件过滤，停止处理浮层以外的交互。"""
        QApplication.instance().removeEventFilter(self)
        super().hideEvent(event)

    # 显示和收起动画。
    def animate_show(self, x: int, y: int) -> None:
        """通过淡入和位移动画显示浮窗；已显示时仅更新位置及层级。

        显示过程不激活窗口，保持主窗口的焦点与鼠标交互连续。
        """
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        # 取消可能在途的关闭动画（关闭途中又被唤起 → 直接转为显示）。
        if self._hide_anim is not None:
            try:
                self._hide_anim.stop()
            except Exception:
                pass
            self._hide_anim = None
        self._closing = False
        if self.isVisible():
            self.move(x, y)
            self.setWindowOpacity(1.0)
            self.raise_()
            return
        if not anim.is_enabled():
            self.move(x, y)
            self.setWindowOpacity(1.0)
            self.show()
            self.raise_()
            return
        start = QPoint(x, y - 12)
        self.move(start)
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        grp = QParallelAnimationGroup(self)
        pa = QPropertyAnimation(self, b"pos", grp)
        pa.setStartValue(start)
        pa.setEndValue(QPoint(x, y))
        pa.setDuration(190)
        pa.setEasingCurve(QEasingCurve.Type.OutCubic)
        oa = QPropertyAnimation(self, b"windowOpacity", grp)
        oa.setStartValue(0.0)
        oa.setEndValue(1.0)
        oa.setDuration(190)
        oa.setEasingCurve(QEasingCurve.Type.OutCubic)
        grp.addAnimation(pa)
        grp.addAnimation(oa)
        self._show_anim = grp
        grp.start()
