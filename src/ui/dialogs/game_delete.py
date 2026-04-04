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

    def changeEvent(self, event):
        """调色板或样式变化时检查主题，只有主题切换才重建样式和图标。"""
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange) and hasattr(self, "_theme"):
            current = theme.current_theme()
            if self._theme != current:
                self._theme = current
                self._retheme()
                icons.retheme_icons(self)
        super().changeEvent(event)


class GameDeleteDialog(_GameDataDialog):
    """展示永久删除的实际范围，不改变用户确认语义。"""

    def __init__(self, game_name: str, info: dict | None, parent: QWidget | None = None, *, scan=None):
        """展示已有统计或准备异步扫描；提供扫描函数时在结果产生前只允许取消。"""
        super().__init__(tr("删除游戏"), parent)
        self.info = info
        self._game_name = game_name
        self._scan = _GameDataScan(scan, self) if scan else None
        self._scan_started = False
        if self._scan is not None:
            manage_page_worker(self, self._scan)
            self._heading(
                tr("正在扫描「{game_name}」", game_name=game_name), tr("正在汇总将删除的数据，可随时取消扫描。")
            )
            self.body.addStretch()
            self._scan_label = _label(tr("准备扫描…"), "strong")
            self.body.addWidget(self._scan_label)
            self._scan_progress = QProgressBar()
            self._scan_progress.setRange(0, 0)
            self.body.addWidget(self._scan_progress)
            self._scan_details = _label("", "muted")
            self.body.addWidget(self._scan_details)
            self.body.addStretch()
            self._footer()
            self.cancel_button.setText(tr("取消扫描"))
            self._scan.progress.connect(self._scan_update)
            self._scan.finished.connect(self._scan_finished)
        else:
            self._show_summary(info or {})

    def showEvent(self, event):
        """首次显示后排队启动扫描，让窗口先完成显示，重复显示不重复扫描。"""
        super().showEvent(event)
        if self._scan is not None and not self._scan_started:
            self._scan_started = True
            call_later(0, self._scan, self._scan.start)

    def _scan_update(self, phase, done, total, size):
        """按目录扫描或容量统计阶段更新进度和数量，忽略已取消扫描的迟到信号。"""
        if self._scan is None or self._scan.cancelled.is_set():
            return
        self._scan_label.setText(tr(phase))
        if phase == "统计容量":
            self._scan_progress.setRange(0, max(1, total))
            self._scan_progress.setValue(done if total else 1)
            self._scan_details.setText(
                tr("已统计 {done:,} / {total:,} 个文件 · {value}", done=done, total=total, value=format_data_size(size))
            )
        else:
            self._scan_progress.setRange(0, 0)
            self._scan_details.setText(tr("已扫描 {total:,} 个目录 · 已发现 {done:,} 个文件", total=total, done=done))

    def _scan_finished(self):
        """释放已结束线程并替换扫描页面；取消时关闭，失败时仅提示，成功后才展示删除确认。"""
        worker, self._scan = self._scan, None
        worker.deleteLater()
        if worker.cancelled.is_set():
            super().reject()
            return
        while self.body.count():
            item = self.body.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())
        self._outer.removeWidget(self._footer_widget)
        self._footer_widget.hide()
        self._footer_widget.deleteLater()
        if worker.error is not None:
            self._heading(tr("无法完成扫描"), tr("请检查资源目录是否可访问，再重试删除。"))
            self.body.addWidget(_label(str(worker.error), "warning", selectable=True))
            self.body.addStretch()
            self._footer()
            return
        self.info = worker.result
        self._show_summary(self.info)

    @staticmethod
    def _clear_layout(layout):
        """递归移出布局项并延迟销毁其中的控件，用于切换扫描结果页面。"""
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
            elif item.layout():
                GameDeleteDialog._clear_layout(item.layout())

    def reject(self):
        """扫描期间发送取消并等待线程结束；没有扫描任务时直接拒绝对话框。"""
        if self._scan is not None:
            self._scan.cancelled.set()
            self.cancel_button.setEnabled(False)
            self.cancel_button.setText(tr("正在取消…"))
        else:
            super().reject()

    def closeEvent(self, event):
        """扫描仍被窗口持有时暂缓系统关闭，转入协作取消流程以保留线程生命周期。"""
        if self._scan is not None:
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)

    def _show_summary(self, info):
        """展示统计快照和永久删除范围，添加确认操作；实际删除由接受结果的调用方执行。"""
        game_name = self._game_name
        self._heading(
            tr("删除「{game_name}」", game_name=game_name), tr("此操作会永久删除以下数据，不进回收站，无法撤销。")
        )
        summary = self._panel()
        size_row = QHBoxLayout()
        size_row.addWidget(_label(tr("资源目录占用"), "strong"))
        size_row.addStretch()
        size_row.addWidget(_label(format_data_size(info.get("library_size", 0)), "number"))
        summary.addLayout(size_row)
        counts = QGridLayout()
        counts.setHorizontalSpacing(20)
        for column, (key, text) in enumerate(
            (
                ("mods", tr("已入库 Mod")),
                ("characters", tr("类别 / 角色")),
                ("presets", tr("预设")),
                ("deployed", tr("部署副本")),
            )
        ):
            counts.addWidget(_label(str(info.get(key, 0)), "number"), 0, column)
            counts.addWidget(_label(text, "muted"), 1, column)
            counts.setColumnStretch(column, 1)
        summary.addSpacing(4)
        summary.addLayout(counts)
        scope = self._panel()
        scope.addWidget(_label(tr("将删除的内容"), "section"))
        scope.addWidget(_label(tr("整个游戏资源目录（含 Mod 库）"), "strong"))
        scope.addWidget(_label(info.get("library_dir") or tr("磁盘上没有该游戏的资源目录。"), "muted", selectable=True))
        scope.addWidget(_label(tr("此目录内的全部文件和文件夹都会删除"), "muted"))
        scope.addSpacing(4)
        scope.addWidget(_label(tr("此游戏的Mod全部部署数据"), "strong"))
        scope.addSpacing(4)
        scope.addWidget(_label(tr("该游戏的配置、预设、按键 / 变体偏好和更新记录。"), "strong"))
        self.body.addWidget(
            _label(tr("需要保留的 Mod 请先「导出」；手动放入的文件请另行备份到上述删除范围之外。"), "warning")
        )
        self.body.addStretch()
        self._footer(destructive=True)


class GameDeleteReportDialog(_GameDataDialog):
    """列出全部清理失败项，路径可读且可复制。"""

    def __init__(self, game_name: str, stats: dict, parent: QWidget | None = None):
        """根据清理统计列出已完成操作和全部失败路径，提供只读复制及关闭操作。"""
        super().__init__(tr("游戏数据清理结果"), parent)
        errors = stats.get("errors") or []
        self._heading(
            tr("「{game_name}」已移除", game_name=game_name),
            tr("有 {count} 项内容未能清理，可能正被其他程序占用。", count=len(errors)),
        )
        summary = self._panel()
        summary.addWidget(_label(tr("已完成"), "strong"))
        summary.addWidget(
            _label(
                tr(
                    "移除游戏配置 · 清理 {value} 份部署副本 · 删除 {value_2} 个预设",
                    value=stats.get("deployed_removed", 0),
                    value_2=stats.get("presets", 0),
                )
            )
        )
        self.body.addWidget(_label(tr("待清理内容"), "strong"))
        paths = QPlainTextEdit("\n\n".join(str(error) for error in errors))
        paths.setReadOnly(True)
        paths.setAccessibleName(tr("未能清理的完整路径"))
        paths.setMinimumHeight(180)
        self.body.addWidget(paths, 1)
        self.body.addWidget(_label(tr("请关闭占用这些文件的程序，再根据以上路径手动清理。"), "muted"))
        self._footer()
