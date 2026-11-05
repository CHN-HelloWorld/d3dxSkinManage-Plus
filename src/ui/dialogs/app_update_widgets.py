"""应用更新的共用界面与明确关闭语义。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui import acrylic, icons, theme
from src.utils.i18n import t as tr


def update_label(text: str, role: str = "body", *, wrap: bool = False) -> QLabel:
    """创建带样式角色的纯文本标签，由接收它的布局接管所有权。"""
    label = QLabel(text)
    label.setProperty("updateRole", role)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    return label


def update_button(text: str, *, primary: bool = False) -> QPushButton:
    """创建主操作或次操作按钮，关闭自动默认行为，仅主操作响应默认确认。"""
    button = QPushButton(text)
    button.setProperty("updateButton", "primary" if primary else "secondary")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setAutoDefault(False)
    if primary:
        button.setDefault(True)
    return button


def update_header(title: str, subtitle: str, icon: str, token: str = "accent") -> QWidget:
    """组合主题图标、标题及可选副标题，返回供更新对话框布局接管的容器。"""
    header = QWidget()
    row = QHBoxLayout(header)
    row.setContentsMargins(26, 22, 26, 20)
    row.setSpacing(16)
    symbol = QLabel()
    symbol.setObjectName("updateSymbol")
    symbol.setFixedSize(48, 48)
    symbol.setAlignment(Qt.AlignmentFlag.AlignCenter)
    icons.apply_pixmap(symbol, icon, 26, token)
    row.addWidget(symbol, 0, Qt.AlignmentFlag.AlignTop)
    copy = QVBoxLayout()
    copy.setSpacing(5)
    copy.addWidget(update_label(title, "title", wrap=True))
    if subtitle:
        copy.addWidget(update_label(subtitle, "secondary", wrap=True))
    row.addLayout(copy, 1)
    return header


def update_footer() -> tuple[QFrame, QHBoxLayout]:
    """返回底栏容器及其水平布局，供调用方添加操作按钮。"""
    footer = QFrame()
    footer.setObjectName("updateFooter")
    row = QHBoxLayout(footer)
    row.setContentsMargins(26, 16, 26, 18)
    row.setSpacing(10)
    return footer, row


class UpdateDialog(QDialog):
    """更新界面使用局部样式，共享应用标题栏。"""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        """初始化使用局部主题样式的更新窗口，并提供无边距内容布局。"""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setObjectName("appUpdateDialog")
        self.setStyleSheet(self._stylesheet())
        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(0, 0, 0, 0)
        self.content.setSpacing(0)

    def prepare(self, *, animate: bool = True) -> None:
        """在内容构建后安装共享标题栏及亚克力效果，可禁用入场动画。"""
        acrylic.apply_dialog_acrylic(self, tint_alpha=0.88, animate=animate)

    @staticmethod
    def _stylesheet() -> str:
        """生成限定在更新窗口及专用控件上的当前主题样式。"""
        color = theme.color
        return f"""
            QDialog#appUpdateDialog {{ color: {color("text.primary")}; }}
            QDialog#appUpdateDialog QLabel {{ background: transparent; border: 0; padding: 0;
                color: {color("text.primary")}; font-size: 13px; }}
            QDialog#appUpdateDialog QLabel[updateRole="title"] {{ font-size: 21px; font-weight: 600; }}
            QDialog#appUpdateDialog QLabel[updateRole="heading"] {{ font-size: 17px; font-weight: 600; }}
            QDialog#appUpdateDialog QLabel[updateRole="secondary"] {{ color: {color("text.secondary")}; }}
            QDialog#appUpdateDialog QLabel[updateRole="caption"] {{ font-size: 11px; color: {color("text.tertiary")}; }}
            QDialog#appUpdateDialog QLabel[updateRole="badge"] {{ font-size: 10px; color: {color("accent")};
                background: {color("accent.subtle")}; padding: 3px 6px; border-radius: 4px; }}
            QDialog#appUpdateDialog QLabel[updateRole="warning"] {{ color: {color("warning")}; font-size: 12px; }}
            QDialog#appUpdateDialog QLabel#updateSymbol {{ background: {color("accent.subtle")}; border-radius: 12px; }}
            QFrame#updateFooter {{ background: {color("bg.subtle")}; border: 0;
                border-top: 1px solid {color("stroke.divider")}; }}
            QFrame#releasePane {{ background: {color("bg.card")}; border: 1px solid {color("stroke.divider")};
                border-radius: 10px; }}
            QListWidget#releaseList {{ background: transparent; border: 0; outline: 0; padding: 0; }}
            QWidget#releaseRow {{ background: transparent; border: 0; }}
            QListWidget#releaseList::item {{ border: 1px solid transparent; border-radius: 8px; margin-bottom: 5px; }}
            QListWidget#releaseList::item:hover {{ background: {color("bg.hover")}; }}
            QListWidget#releaseList::item:selected {{ background: {color("accent.subtle")};
                border-color: {color("stroke.control")}; }}
            QTextBrowser#releaseNotes {{ background: transparent; color: {color("text.primary")};
                border: 0; padding: 0; font-size: 13px; selection-background-color: {color("accent")}; }}
            QPushButton[updateButton] {{ min-height: 20px; min-width: 68px; padding: 7px 16px;
                border: 1px solid {color("stroke.control")}; border-radius: 6px;
                background: {color("bg.card")}; color: {color("text.primary")}; font-size: 12px; }}
            QPushButton[updateButton]:hover {{ background: {color("bg.hover")}; }}
            QPushButton[updateButton]:pressed {{ background: {color("bg.pressed")}; }}
            QPushButton[updateButton]:focus {{ border-color: {color("accent")}; }}
            QPushButton[updateButton="primary"] {{ background: {color("accent")};
                color: {color("text.on_accent")}; border-color: {color("accent")}; font-weight: 600; }}
            QPushButton[updateButton="primary"]:hover {{ background: {color("accent.hover")}; }}
            QProgressBar#updateProgress {{ background: {color("bg.pressed")}; border: 0;
                border-radius: 3px; min-height: 6px; max-height: 6px; }}
            QProgressBar#updateProgress::chunk {{ background: {color("accent")}; border-radius: 3px; }}
        """


class UpdateProgressDialog(UpdateDialog):
    """任务可完成、取消或在窗口隐藏后继续执行。"""

    canceled = Signal()

    def __init__(self, title: str, text: str, parent: QWidget | None = None, *, cancellable: bool = True) -> None:
        """构建下载或安装准备进度；仅可取消模式提供取消按钮和下载速度。"""
        super().__init__(title, parent)
        self._cancellable = cancellable
        self._cancel_emitted = False
        self._finishing = False
        self.setMinimumWidth(520)
        subtitle = (
            tr("下载完成后将校验更新包并准备安装。") if cancellable else tr("安装准备完成后，程序将关闭并自动更新。")
        )
        self.content.addWidget(
            update_header(title, subtitle, "actions/download-manager" if cancellable else "settings/box")
        )
        body = QVBoxLayout()
        body.setContentsMargins(26, 2, 26, 24)
        body.setSpacing(14)
        status = QHBoxLayout()
        status.setSpacing(14)
        self._status = update_label(text, wrap=True)
        self._status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._percent = update_label("0%", "secondary")
        status.addWidget(self._status, 1)
        status.addWidget(self._percent, 0, Qt.AlignmentFlag.AlignTop)
        body.addLayout(status)
        self._bar = QProgressBar()
        self._bar.setObjectName("updateProgress")
        self._bar.setRange(0, 100 if cancellable else 0)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        body.addWidget(self._bar)
        self._speed = update_label(tr("下载速度：等待数据…"), "caption")
        self._speed.setObjectName("downloadSpeed")
        self._speed.setVisible(cancellable)
        body.addWidget(self._speed)
        self._source = update_label("", "caption", wrap=True)
        self._source.setObjectName("downloadSource")
        self._source.setVisible(False)
        body.addWidget(self._source)
        self.content.addLayout(body)
        footer, row = update_footer()
        row.addWidget(
            update_label(
                tr("取消后将清理已下载数据。") if cancellable else tr("关闭窗口后，安装准备仍会继续。"),
                "caption",
                wrap=True,
            ),
            1,
        )
        if cancellable:
            button = update_button(tr("取消下载"))
            button.setObjectName("cancelDownload")
            button.clicked.connect(self.cancel)
            row.addWidget(button)
        self.content.addWidget(footer)
        self._refresh_percent()
        self.prepare(animate=False)

    def setLabelText(self, text: str) -> None:
        """在界面线程替换当前任务的状态说明。"""
        self._status.setText(text)

    def setMaximum(self, maximum: int) -> None:
        """设置从零开始的进度范围；上限为零时显示不确定进度。"""
        self._bar.setRange(0, maximum)
        self._refresh_percent()

    def setValue(self, value: int) -> None:
        """更新进度值并同步百分比，数值范围由进度条约束。"""
        self._bar.setValue(value)
        self._refresh_percent()

    def setDownloadSpeed(self, bytes_per_second: float | None) -> None:
        """将每秒字节数显示为二进制单位速率；空值表示等待数据，负值按零处理。"""
        if bytes_per_second is None:
            self._speed.setText(tr("下载速度：等待数据…"))
            return
        speed = max(0.0, bytes_per_second)
        for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
            if speed < 1024 or unit == "GB/s":
                value = f"{speed:.0f} {unit}" if unit == "B/s" else f"{speed:.1f} {unit}"
                self._speed.setText(tr("下载速度：{speed}", speed=value))
                return
            speed /= 1024

    def setDownloadSource(self, source: str) -> None:
        """独立显示下载源和代理状态，不随字节进度说明刷新而丢失。"""
        self._source.setText(tr("当前线路：{source}", source=source))
        self._source.setVisible(self._cancellable and bool(source))

    def _refresh_percent(self) -> None:
        """根据当前进度和上限刷新百分比，无确定上限时显示进行中。"""
        maximum = self._bar.maximum()
        self._percent.setText(f"{max(0, self._bar.value()) * 100 // maximum}%" if maximum else tr("进行中"))

    def _report_cancel(self) -> None:
        """对尚未完成的可取消任务至多发送一次取消信号，由管理器停止实际工作。"""
        if self._cancellable and not self._finishing and not self._cancel_emitted:
            self._cancel_emitted = True
            self.canceled.emit()

    def cancel(self) -> None:
        """经拒绝路径关闭窗口，使按钮与窗口关闭采用相同取消语义。"""
        self.reject()

    def reject(self) -> None:
        """按任务的可取消状态报告取消，再结束对话框；安装准备可在窗口隐藏后继续。"""
        self._report_cancel()
        super().reject()

    def closeEvent(self, event) -> None:
        """处理标题栏关闭并报告允许的取消请求，完成路径不发送取消信号。"""
        self._report_cancel()
        super().closeEvent(event)

    def finish(self) -> None:
        """任务完成后关闭窗口，不报告为用户取消。"""
        self._finishing = True
        self.close()


class UpdateNoticeDialog(UpdateDialog):
    """为更新管理器各结束路径提供统一提示窗口。"""

    def __init__(self, title: str, text: str, parent: QWidget | None = None, *, error: bool = False) -> None:
        """构建可选择复制的更新结果说明，以错误状态选择图标和颜色。"""
        super().__init__(title, parent)
        self.setMinimumWidth(500)
        self.setMaximumWidth(640)
        self.content.addWidget(
            update_header(title, "", "status/error" if error else "status/info", "danger" if error else "accent")
        )
        body = QVBoxLayout()
        body.setContentsMargins(26, 0, 26, 24)
        label = update_label(text, "secondary", wrap=True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(label)
        self.content.addLayout(body)
        footer, row = update_footer()
        row.addStretch()
        button = update_button(tr("知道了"), primary=True)
        button.setObjectName("acknowledge")
        button.clicked.connect(self.accept)
        row.addWidget(button)
        self.content.addWidget(footer)
        self.prepare()
