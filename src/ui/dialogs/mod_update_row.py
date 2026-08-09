"""显示紧凑更新条目，包含图标、有界文字和可展开说明。"""

from __future__ import annotations

import math
import re
from datetime import datetime
from html.parser import HTMLParser

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTextBlockFormat,
    QTextCursor,
    QTextDocument,
    QTextOption,
)
from PySide6.QtWidgets import (
    QBoxLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from src.services.gamebanana.update_checker import UpdateInfo
from src.ui import icons
from src.ui.dialogs.mod_update_task_progress import (
    ACTIVE_PHASES,
    FILE_PHASES,
    PAUSABLE_PHASES,
    PHASE_ICONS,
    PHASE_LABELS,
    TaskProgressBar,
    TaskRingButton,
    finite_number,
    format_eta,
    progress_fraction,
    progress_phase,
)
from src.ui.dialogs.mod_updates_style import TEXT_LINE_HEIGHT, color, menu_stylesheet
from src.ui.widgets.tag_chip import TagFlowLayout
from src.utils.i18n import t as tr


def label(text: str = "", name: str = "") -> QLabel:
    """创建可收缩宽度的具名纯文本标签，供更新行和窗口局部样式使用。"""
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(name)
    widget.setMinimumWidth(0)
    return widget


def apply_icon(target, name: str, tone: str = "secondary") -> None:
    """为按钮或动作安装更新模块命名空间下的主题图标，并登记主题刷新信息。"""
    icons.apply_icon(target, f"mod-updates/{name}", f"mod_updates.{tone}")


def icon_label(name: str, size: int, tone: str = "secondary") -> QLabel:
    """创建固定尺寸的图标标签，使用更新模块的图标路径和语义色。"""
    widget = label()
    widget.setFixedSize(size, size)
    icons.apply_pixmap(widget, f"mod-updates/{name}", size, f"mod_updates.{tone}")
    return widget


def format_size(size: int) -> str:
    """把非负字节量转换为 B、KB、MB 或 GB 文本，负数按零处理。"""
    value = max(0, size)
    if value >= 1024**3:
        return f"{value / 1024**3:.1f} GB"
    if value >= 1024**2:
        return f"{value / 1024**2:.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value} B"


def _update_error_summary(error: str) -> str:
    """条目显示可读结果，详细诊断保留在提示中。"""
    if "NTSTATUS" in error:
        for operation in (tr("启动"), tr("暂停"), tr("继续")):
            if f"无法{operation}更新进程" in error:
                return tr("无法{operation}更新，请重试。", operation=operation)
    if "校验后端" in error:
        return tr("更新文件校验组件不可用，请重新安装程序后重试。")
    if error.startswith("更新压缩包校验失败"):
        if "文件不存在或无法读取" in error:
            return tr("无法读取更新文件，请重新下载后重试。")
        if "不支持的压缩格式" in error:
            return tr("不支持此更新文件的压缩格式。")
        return tr("更新文件校验失败，请重新下载后重试。")
    return error


def file_date(timestamp: int) -> str:
    """按本地时区显示文件时间戳，无有效正时间戳或超出平台范围时返回日期未知。"""
    if timestamp <= 0:
        return tr("日期未知")
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y.%m.%d")
    except (OSError, OverflowError, ValueError):
        return tr("日期未知")


class ElidedLabel(QLabel):
    """完整保留原文和提示，长名称不扩大对话框。"""

    def __init__(self, text: str = "", name: str = "", height: int = TEXT_LINE_HEIGHT):
        """保存完整纯文本和工具提示，固定行高并允许布局压缩宽度。"""
        super().__init__(text)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setObjectName(name)
        self.setFixedHeight(height)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setToolTip(text)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)

    def minimumSizeHint(self) -> QSize:
        """返回零最小宽度和固定行高，使长名称不反向扩大窗口。"""
        return QSize(0, self.height())

    def paintEvent(self, event) -> None:
        """按内容区宽度绘制尾部省略文本，保留控件原始完整文本。"""
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, self.contentsRect().width())
        painter.drawText(self.contentsRect(), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)


class SummaryButton(QPushButton):
    """以可压缩宽度显示更新日志摘要，保留按钮交互及完整工具提示。"""

    def sizeHint(self) -> QSize:
        """返回固定文本行高及零建议宽度，由父布局分配摘要所需空间。"""
        return QSize(0, TEXT_LINE_HEIGHT)

    def minimumSizeHint(self) -> QSize:
        """允许摘要按钮横向收缩而保持一行高度。"""
        return QSize(0, TEXT_LINE_HEIGHT)

    def paintEvent(self, event) -> None:
        """在按钮原生样式中绘制尾部省略的摘要，并预留图标空间。"""
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, max(0, self.width() - 20))
        painter = QStylePainter(self)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)


class _ReleaseText(HTMLParser):
    """把发布说明 HTML 提取为适合只读展示的纯文本，忽略脚本和样式内容。"""

    def __init__(self) -> None:
        """启用字符实体转换，初始化文本片段及脚本、样式跳过层数。"""
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipped = 0

    def handle_starttag(self, tag, attrs):
        """进入脚本或样式时暂停收集文本，其他块级标签插入换行，列表项附加项目符号。"""
        if tag in {"script", "style"}:
            self.skipped += 1
        elif not self.skipped:
            if tag == "li":
                self.parts.append("\n• ")
            elif tag in {"p", "div", "br", "h1", "h2", "h3", "h4", "tr"}:
                self.parts.append("\n")

    def handle_endtag(self, tag):
        """离开脚本或样式区域时恢复收集，其余块级结束标签补充换行。"""
        if tag in {"script", "style"}:
            self.skipped = max(0, self.skipped - 1)
        elif not self.skipped and tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        """仅在非脚本和非样式区域收集文本内容。"""
        if not self.skipped:
            self.parts.append(data)


def release_text(html: str) -> str:
    """解析发布说明并合并连续空白行，返回去除首尾空白的纯文本。"""
    parser = _ReleaseText()
    parser.feed(html or "")
    return re.sub(r"\n[ \t\n]*\n", "\n", "".join(parser.parts)).strip()


class WrappingLabel(QLabel):
    """提供可控行距的纯文本标签，支持不含分隔符的长文件名。"""

    def __init__(self, text: str, name: str = "updateLogText") -> None:
        """为纯文本创建控件持有的 QTextDocument，固定行距并允许无分隔符长文本换行。"""
        super().__init__(text)
        self.setObjectName(name)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._document = QTextDocument(self)
        self._document.setDocumentMargin(0)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self._document.setDefaultTextOption(option)
        self._document.setPlainText(text)
        cursor = QTextCursor(self._document)
        cursor.select(QTextCursor.SelectionType.Document)
        block = QTextBlockFormat()
        block.setLineHeight(TEXT_LINE_HEIGHT, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        cursor.mergeBlockFormat(block)

    def _measure(self, width: int) -> int:
        """按当前字体和指定宽度重新排版内部文档，返回向上取整并留有余量的高度。"""
        self._document.setDefaultFont(self.font())
        self._document.setTextWidth(max(1, width))
        return math.ceil(self._document.size().height()) + 2

    def heightForWidth(self, width: int) -> int:
        """将指定布局宽度转换为文档实际所需高度，供父布局按宽度分配空间。"""
        return self._measure(width)

    def sizeHint(self) -> QSize:
        """以二百四十像素宽度计算默认建议尺寸。"""
        return QSize(240, self._measure(240))

    def minimumSizeHint(self) -> QSize:
        """不为长文本设置额外最小宽高，允许父布局按可用空间重新排版。"""
        return QSize(0, 0)

    def paintEvent(self, event) -> None:
        """按实际宽度排版内部文档并使用控件调色板绘制纯文本。"""
        self._measure(self.width())
        painter = QPainter(self)
        context = self._document.documentLayout().PaintContext()
        context.palette = self.palette()
        self._document.documentLayout().draw(painter, context)


class UpdateThumbnail(QLabel):
    """持有本地预览 QPixmap 并按更新行尺寸裁切，缺图时展示占位说明和图标。"""

    def __init__(self, info: UpdateInfo):
        """创建固定尺寸的预览控件，加载更新信息中的本地图片路径。"""
        super().__init__()
        self.setObjectName("updateThumb")
        self.setFixedSize(60, 66)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
        self.setContentsMargins(1, 0, 1, 8)
        self.setAccessibleName(tr("Mod 外观预览"))
        self._path = None
        self._source = QPixmap()
        self.set_source(info.local_preview)

    def set_source(self, path: str) -> None:
        """路径变化时在界面线程加载源 QPixmap 并重新渲染，同一路径复用已加载图像。"""
        path = (path or "").strip()
        if path == self._path:
            return
        self._path = path
        self._source = QPixmap(path) if path else QPixmap()
        self._render_preview()

    def _render_preview(self) -> None:
        """按设备像素比填充裁切圆角预览，纵向偏向图片上部；空源显示占位文字。"""
        if self._source.isNull():
            self.setContentsMargins(1, 0, 1, 4 if self.width() < 50 else 8)
            self.setText(tr("暂无预览"))
            self.setAccessibleName(tr("暂无 Mod 预览图"))
        else:
            self.setContentsMargins(0, 0, 0, 0)
            ratio = self.devicePixelRatioF()
            size = QSize(round(self.width() * ratio), round(self.height() * ratio))
            scaled = self._source.scaled(
                size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation
            )
            x = max(0, (scaled.width() - size.width()) // 2)
            y = max(0, round((scaled.height() - size.height()) * 0.21))
            pm = QPixmap(size)
            pm.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pm)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(pm.rect()), 7 * ratio, 7 * ratio)
            painter.setClipPath(clip)
            painter.drawPixmap(0, 0, scaled.copy(x, y, size.width(), size.height()))
            painter.end()
            pm.setDevicePixelRatio(ratio)
            self.setPixmap(pm)
        self.update()

    def resizeEvent(self, event) -> None:
        """控件尺寸变化后从保留的源图重新缩放裁切，避免反复缩放已裁切结果。"""
        super().resizeEvent(event)
        if hasattr(self, "_source"):
            self._render_preview()

    def paintEvent(self, event) -> None:
        """先绘制圆角底色和边框，再绘制图片或缺图文本，空源额外显示主题图片图标。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(color("subtle")))
        painter.setPen(QPen(QColor(color("line")), 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)
        painter.end()
        super().paintEvent(event)
        if self._source.isNull():
            painter = QPainter(self)
            size = 18 if self.width() < 50 else 23
            pm = icons.pixmap("mod-updates/image", size, "mod_updates.muted")
            painter.drawPixmap((self.width() - size) // 2, 8 if self.width() < 50 else 14, pm)


class UpdateRow(QWidget):
    """展示单个 Mod 的更新信息、日志和任务快照，通过所属对话框信号请求实际操作。"""

    def __init__(self, info: UpdateInfo, dialog):
        """创建版本、缩略图、摘要及任务控件，连接所属窗口的操作信号并初始化为可更新状态。"""
        super().__init__(dialog)
        self.setObjectName("updateRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setProperty("modId", info.local_mod_id)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.info = info
        self.dialog = dialog
        self.state = "ready"
        self.phase = "ready"
        self.task_snapshot: dict = {}
        self._compact = False
        self._short = False
        self._logs_built = False
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(4, 14, 4, 14)
        self._root.setSpacing(12)
        self._main = QWidget()
        self._grid = QGridLayout(self._main)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(13)
        self._grid.setVerticalSpacing(0)
        self._grid.setColumnStretch(1, 1)
        self.thumb = UpdateThumbnail(info)
        self._grid.addWidget(self.thumb, 0, 0, Qt.AlignmentFlag.AlignVCenter)
        copy = QWidget()
        copy.setMinimumWidth(0)
        copy_layout = QVBoxLayout(copy)
        copy_layout.setContentsMargins(0, 0, 0, 0)
        copy_layout.setSpacing(0)
        self.name_label = ElidedLabel(name="updateName")
        self.name_label.setProperty("modId", info.local_mod_id)
        copy_layout.addWidget(self.name_label)
        copy_layout.addSpacing(5)
        self._version = QWidget()
        version_layout = TagFlowLayout(self._version, spacing=7)
        self.old_version = ElidedLabel(name="updateOldVersion", height=TEXT_LINE_HEIGHT + 4)
        self.old_version.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.old_version.setMaximumWidth(140)
        version_layout.addWidget(self.old_version)
        arrow = icon_label("arrow", 13, "muted")
        arrow.setFixedHeight(TEXT_LINE_HEIGHT + 4)
        arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        version_layout.addWidget(arrow)
        self.new_version = ElidedLabel(name="updateNewVersion", height=TEXT_LINE_HEIGHT + 4)
        self.new_version.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.new_version.setContentsMargins(7, 2, 7, 2)
        self.new_version.setMaximumWidth(150)
        version_layout.addWidget(self.new_version)
        divider_slot = QWidget()
        divider_slot.setFixedSize(5, TEXT_LINE_HEIGHT + 4)
        divider = QWidget(divider_slot)
        divider.setObjectName("updateVersionDivider")
        divider.setGeometry(2, 6, 1, 10)
        version_layout.addWidget(divider_slot)
        self.file_size = label(name="updateFileSize")
        self.file_size.setFixedHeight(TEXT_LINE_HEIGHT + 4)
        version_layout.addWidget(self.file_size)
        self.date_hint = label(tr("按文件日期"), "updateDateHint")
        self.date_hint.setFixedHeight(TEXT_LINE_HEIGHT + 4)
        version_layout.addWidget(self.date_hint)
        self._version.setMinimumHeight(TEXT_LINE_HEIGHT + 4)
        copy_layout.addWidget(self._version)
        copy_layout.addSpacing(5)
        self.summary = SummaryButton()
        self.summary.setObjectName("updateSummary")
        self.summary.setProperty("modId", info.local_mod_id)
        self.summary.setCheckable(True)
        self.summary.setAutoDefault(False)
        self.summary.setMinimumWidth(0)
        self.summary.setFixedHeight(TEXT_LINE_HEIGHT)
        self.summary.setIconSize(QSize(12, 12))
        apply_icon(self.summary, "chevron", "muted")
        self.summary.toggled.connect(self._toggle_logs)
        copy_layout.addWidget(self.summary)
        self._grid.addWidget(copy, 0, 1)
        self._actions = QWidget()
        actions_layout = QHBoxLayout(self._actions)
        actions_layout.setContentsMargins(7, 0, 0, 0)
        actions_layout.setSpacing(6)
        self.update_button = QPushButton(tr("更新"))
        self.update_button.setObjectName("updateSingle")
        self.update_button.setProperty("modId", info.local_mod_id)
        self.update_button.setFixedSize(76, 30)
        self.update_button.setIconSize(QSize(14, 14))
        self.update_button.setAutoDefault(False)
        self.update_button.clicked.connect(lambda: dialog.update_requested.emit(self.info.local_mod_id))
        actions_layout.addWidget(self.update_button)
        self._task_controls = QWidget()
        task_controls = QHBoxLayout(self._task_controls)
        task_controls.setContentsMargins(0, 0, 0, 0)
        task_controls.setSpacing(3)
        self.pause_button = TaskRingButton()
        self.pause_button.setObjectName("updateTaskPause")
        self.pause_button.setProperty("modId", info.local_mod_id)
        self.pause_button.clicked.connect(self._pause_or_resume)
        task_controls.addWidget(self.pause_button)
        self.stop_button = QPushButton()
        self.stop_button.setObjectName("updateTaskStop")
        self.stop_button.setProperty("modId", info.local_mod_id)
        self.stop_button.setProperty("iconOnly", True)
        self.stop_button.setFixedSize(28, 28)
        self.stop_button.setIconSize(QSize(15, 15))
        self.stop_button.setAutoDefault(False)
        self.stop_button.setCursor(Qt.CursorShape.PointingHandCursor)
        icons.apply_icon(self.stop_button, "actions/stop", "mod_updates.secondary")
        self.stop_button.clicked.connect(lambda: dialog.stop_update_requested.emit(self.info.local_mod_id))
        task_controls.addWidget(self.stop_button)
        actions_layout.addWidget(self._task_controls)
        self._task_controls.hide()
        self.more = QPushButton()
        self.more.setObjectName("updateMore")
        self.more.setProperty("modId", info.local_mod_id)
        self.more.setProperty("iconOnly", True)
        self.more.setFixedSize(30, 30)
        self.more.setAutoDefault(False)
        self.more.setIconSize(QSize(18, 18))
        self.more.setToolTip(tr("更多操作"))
        apply_icon(self.more, "more")
        self._build_menu()
        actions_layout.addWidget(self.more)
        self._grid.addWidget(self._actions, 0, 2)
        self._root.addWidget(self._main)
        self._build_task_progress()
        self._log_margin = QWidget()
        log_margin = QHBoxLayout(self._log_margin)
        log_margin.setContentsMargins(73, 0, 36, 0)
        self._log = QWidget()
        self._log.setObjectName("updateLog")
        self._log_layout = QVBoxLayout(self._log)
        self._log_layout.setContentsMargins(15, 1, 0, 6)
        self._log_layout.setSpacing(10)
        log_margin.addWidget(self._log)
        self._root.addWidget(self._log_margin)
        self._log_margin.hide()
        self.set_info(info)
        self.set_state("ready")

    def _build_task_progress(self) -> None:
        """构建默认隐藏的任务阶段、真实进度、下载指标及错误详情区域。"""
        self._task_margin = QWidget()
        margin = QHBoxLayout(self._task_margin)
        margin.setContentsMargins(73, 0, 36, 0)
        self._task = QWidget()
        self._task.setObjectName("updateTaskProgress")
        self._task.setMinimumWidth(0)
        content = QVBoxLayout(self._task)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(4)
        self.task_file = ElidedLabel(name="updateTaskFile")
        content.addWidget(self.task_file)
        line = QHBoxLayout()
        line.setSpacing(5)
        self.task_icon = label()
        self.task_icon.setFixedSize(14, 14)
        line.addWidget(self.task_icon)
        self.task_phase = ElidedLabel(name="updateTaskPhase")
        line.addWidget(self.task_phase, 1)
        self.task_percent = label(name="updateTaskPercent")
        self.task_percent.setFixedHeight(TEXT_LINE_HEIGHT)
        line.addWidget(self.task_percent)
        content.addLayout(line)
        self.progress = TaskProgressBar()
        self.progress.setObjectName("updateTaskTrack")
        content.addWidget(self.progress)
        self.task_metrics = ElidedLabel(name="updateTaskMetrics")
        content.addWidget(self.task_metrics)
        self.task_detail = ElidedLabel(name="updateTaskDetail")
        content.addWidget(self.task_detail)
        margin.addWidget(self._task)
        self._root.addWidget(self._task_margin)
        self._task_margin.hide()

    def _build_menu(self) -> None:
        """创建由更多按钮持有的来源和忽略菜单，触发时使用当前更新信息的 Mod 标识。"""
        menu = QMenu(self.more)
        menu.setObjectName("modUpdatesMenu")
        menu.setStyleSheet(menu_stylesheet())
        menu.setMinimumWidth(205)
        entries = [
            (tr("在 GameBanana 查看"), "updateOpenSource", "external", self.dialog.open_gb_requested),
            (tr("忽略此版本"), "updateIgnoreVersion", "skip", self.dialog.ignore_version_requested),
            (tr("忽略此 Mod"), "updateIgnoreMod", "bell-off", self.dialog.ignore_mod_requested),
        ]
        for index, (text, name, svg, signal) in enumerate(entries):
            if index == 1:
                menu.addSeparator()
            action = menu.addAction(text)
            action.setObjectName(name)
            apply_icon(action, svg)
            action.triggered.connect(lambda _=False, s=signal: s.emit(self.info.local_mod_id))
        hint = QWidgetAction(menu)
        hint.setObjectName("updateMenuHint")
        hint.setEnabled(False)
        hint.setDefaultWidget(label(tr("忽略 Mod 后，将不再提示其后续更新。")))
        menu.addAction(hint)
        self.more.setMenu(menu)

    def set_info(self, info: UpdateInfo) -> None:
        """替换更新信息并同步名称、版本、预览和摘要；日志变化时作废惰性内容并按展开状态重建。"""
        changed_logs = self.info.changelog != info.changelog
        self.info = info
        name = info.local_name or str(info.gb_mod_id)
        self.name_label.setText(name)
        self.name_label.setToolTip(name)
        self.more.setAccessibleName(tr("{name}的更多操作", name=name))
        self.update_button.setAccessibleName(tr("更新{name}", name=name))
        self.stop_button.setAccessibleName(tr("终止更新{name}", name=name))
        self.stop_button.setToolTip(tr("终止此项更新…"))
        current, latest = info.current_version, info.latest_version
        dated = not current and not latest
        self.old_version.setText(file_date(info.current_file_ts) if dated else current or tr("版本未知"))
        self.new_version.setText(file_date(info.latest_file.date_added_ts) if dated else latest or tr("新文件"))
        self.old_version.setToolTip(tr("当前：{value}", value=self.old_version.text()))
        self.new_version.setToolTip(tr("新版：{value}", value=self.new_version.text()))
        self.date_hint.setVisible(dated)
        self.file_size.setText(format_size(info.latest_file.size_bytes))
        titles = [entry.title for entry in info.changelog if entry.title][:3]
        summary = "；".join(titles) or tr("作者暂未提供更新日志")
        self.summary.setText(summary)
        self.summary.setToolTip(summary)
        self.thumb.set_source(info.local_preview)
        if changed_logs and self._logs_built:
            while self._log_layout.count():
                item = self._log_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self._logs_built = False
            if self.summary.isChecked():
                self._build_logs()
        if self.task_snapshot:
            self._render_task()

    def _build_logs(self) -> None:
        """首次展开时构建纯文本发布说明、版本标签及来源链接，后续展开复用已有控件。"""
        if self._logs_built:
            return
        self._logs_built = True
        for entry in self.info.changelog:
            block = QWidget()
            layout = QVBoxLayout(block)
            layout.setContentsMargins(0, 8, 0, 6)
            layout.setSpacing(7)
            header = QHBoxLayout()
            header.setContentsMargins(0, 0, 0, 0)
            header.setSpacing(8)
            header.addWidget(ElidedLabel(entry.title or tr("更新说明"), "updateLogTitle"), 1)
            header.addWidget(label(file_date(entry.date_added_ts), "updateLogDate"))
            layout.addLayout(header)
            tags = [str(value) for value in [entry.version, *entry.tags] if value]
            if tags:
                tag_row = QWidget()
                tag_row.setObjectName("updateLogTags")
                tag_layout = TagFlowLayout(tag_row, spacing=5)
                for text in tags:
                    chip = ElidedLabel(text, "updateLogTag", TEXT_LINE_HEIGHT + 4)
                    chip.setContentsMargins(6, 1, 6, 1)
                    chip.setMaximumWidth(160)
                    chip.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
                    tag_layout.addWidget(chip)
                layout.addWidget(tag_row)
            text = release_text(entry.description_html) or entry.title or tr("作者暂未提供详细说明。")
            layout.addWidget(WrappingLabel(text))
            self._log_layout.addWidget(block)
        if not self.info.changelog:
            self._log_layout.addWidget(WrappingLabel(tr("作者暂未提供更新日志。")))
        source = QPushButton(tr("查看 GameBanana"))
        source.setObjectName("updateSourceLink")
        source.setAutoDefault(False)
        source.setFixedSize(154, 24)
        source.setIconSize(QSize(14, 14))
        source.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        apply_icon(source, "external", "accent")
        source.clicked.connect(lambda: self.dialog.open_gb_requested.emit(self.info.local_mod_id))
        self._log_layout.addWidget(source)

    def _toggle_logs(self, checked: bool) -> None:
        """按需构建日志后切换可见性和箭头图标，并通知父布局行高发生变化。"""
        if checked:
            self._build_logs()
        self._log_margin.setVisible(checked)
        apply_icon(self.summary, "chevron-down" if checked else "chevron", "muted")
        self.updateGeometry()

    def set_state(self, state: str, error: str = "") -> None:
        """将 ready 状态重置为空任务，或把 updating、done、failed 转成对应任务快照；未知非空闲状态抛出 KeyError。"""
        if state != "ready":
            self.set_task_snapshot(
                {
                    **self.task_snapshot,
                    "phase": {"updating": "preparing", "done": "done", "failed": "failed"}[state],
                    "error": error,
                }
            )
            return
        self.phase = self.state = "ready"
        self.task_snapshot = {}
        self._task_margin.hide()
        self._set_state_appearance()

    def set_task_snapshot(self, snapshot: dict) -> None:
        """复制任务快照并从阶段派生行状态，然后展示进度区、刷新操作及任务反馈。"""
        self.task_snapshot = dict(snapshot)
        self.phase = str(snapshot.get("phase", "preparing"))
        self.state = (
            "done"
            if self.phase == "done"
            else "failed"
            if self.phase in {"failed", "canceled", "deferred"}
            else "updating"
        )
        self._task_margin.show()
        self._set_state_appearance()
        self._render_task()

    def _set_state_appearance(self) -> None:
        """同步完成样式、重试文字及活动控制区，停止非活动任务动画并按需重新应用样式。"""
        state = self.state
        changed = self.update_button.property("state") != state
        self.update_button.setProperty("state", state)
        self.setProperty("complete", state == "done")
        self.new_version.setProperty("complete", state == "done")
        text = {"ready": tr("更新"), "updating": tr("更新中"), "done": tr("已更新"), "failed": tr("重试")}[state]
        self.update_button.setText(tr("继续") if self.phase == "deferred" else text)
        error = str(self.task_snapshot.get("error") or "")
        self.update_button.setToolTip(tr("详细信息：\n{error}", error=error) if error else "")
        if state == "done":
            apply_icon(self.update_button, "check", "green")
            status_icon = self.update_button.icon()
            status_icon.addPixmap(status_icon.pixmap(32, 32), QIcon.Mode.Disabled)
            self.update_button.setIcon(status_icon)
        else:
            self.update_button.setProperty("_themed_icon_name", None)
            self.update_button.setIcon(QIcon())
        active = self.phase in ACTIVE_PHASES
        self._task_controls.setVisible(active)
        self.update_button.setVisible(not active)
        self.more.setVisible(not active)
        self.refresh_actions()
        if not active:
            self.pause_button.set_indeterminate(False)
        if changed:
            for widget in (self, self.update_button, self.new_version):
                widget.style().unpolish(widget)
                widget.style().polish(widget)
                widget.update()

    def _pause_or_resume(self) -> None:
        """暂停阶段请求恢复，允许暂停的活动阶段请求暂停，其他阶段不发送操作。"""
        if self.phase == "paused":
            self.dialog.resume_update_requested.emit(self.info.local_mod_id)
        elif self.phase in PAUSABLE_PHASES:
            self.dialog.pause_update_requested.emit(self.info.local_mod_id)

    def _render_task(self) -> None:
        """按任务实际阶段或恢复阶段解释进度，分别展示下载字节、速率、剩余时间或文件处理数量。

        同步环形按钮和细进度条的可见性及动画，完成时展示结果，错误摘要与完整诊断分别放在正文和提示中。
        """
        snapshot, phase = self.task_snapshot, self.phase
        basis = progress_phase(snapshot)
        fraction = progress_fraction(snapshot)
        paused = phase == "paused"
        tone = (
            "green"
            if phase == "done"
            else "warning"
            if phase == "failed"
            else "secondary"
            if phase in {"queued", "paused", "canceled", "stopping", "deferred"}
            else "accent"
        )
        phase_text = tr(PHASE_LABELS.get(phase, phase))
        if paused and basis in PHASE_LABELS:
            phase_text += f" · {tr(PHASE_LABELS[basis])}"
        self.task_phase.setText(phase_text)
        self.task_phase.setToolTip(phase_text)
        self.task_phase.setStyleSheet(f"color: {color(tone)};")
        complete = phase == "done"
        icon_size = (21 if self._short else 25) if complete else 14
        self.task_icon.setFixedSize(icon_size, icon_size)
        task_icon = "mod-updates/check-circle" if complete else PHASE_ICONS.get(phase, "mod-updates/info")
        icons.apply_pixmap(self.task_icon, task_icon, icon_size, f"mod_updates.{tone}")
        self._task.setMinimumHeight((42 if self._short else 59) if complete else 0)
        self.task_percent.setText(f"{math.floor(fraction * 100)}%" if fraction is not None and phase != "done" else "")
        self.progress.set_measurement(fraction, running=phase in PAUSABLE_PHASES | {"stopping"}, tone=tone)
        self.progress.setAccessibleName(
            tr("{local_name} · {phase_text}进度", local_name=self.info.local_name, phase_text=phase_text)
        )
        self.progress.setVisible(phase != "done" and (phase in ACTIVE_PHASES or fraction is not None))
        self.pause_button.set_icon("actions/play" if paused else "actions/pause", f"mod_updates.{tone}")
        self.pause_button.set_ring_color(f"mod_updates.{tone}")
        self.pause_button.set_progress(fraction * 100 if fraction is not None else -1)
        self.pause_button.set_indeterminate(fraction is None and phase in PAUSABLE_PHASES)
        action = tr("继续") if paused else tr("暂停")
        self.pause_button.setAccessibleName(
            tr("{action}更新{local_name}", action=action, local_name=self.info.local_name)
        )
        self.pause_button.setToolTip(tr("{action}此项更新", action=action))
        metrics = []
        if basis == "downloading" and phase != "done":
            downloaded = max(0, int(finite_number(snapshot.get("downloaded_bytes"))))
            total = max(0, int(finite_number(snapshot.get("total_bytes"))))
            metrics.append(
                f"{format_size(downloaded)} / {format_size(total)}"
                if total
                else tr("已下载 {value} · 总大小未知", value=format_size(downloaded))
            )
            if phase == "downloading":
                speed = max(0, int(finite_number(snapshot.get("speed_bps"))))
                if speed:
                    metrics.append(f"{format_size(speed)}/s")
                eta = format_eta(snapshot.get("eta_seconds"))
                if eta:
                    metrics.append(eta)
        elif basis in FILE_PHASES:
            completed = finite_number(snapshot.get("completed_files"), -1)
            total = finite_number(snapshot.get("total_files"), -1)
            if completed >= 0 and total > 0:
                metrics.append(tr("已处理 {value} / {value_2} 个文件", value=int(completed), value_2=int(total)))
        file_name = str(snapshot.get("file_name") or "")
        error = str(snapshot.get("error") or "")
        detail = (
            tr("新版已安装 · 即将移出列表")
            if complete
            else _update_error_summary(error)
            if error
            else str(snapshot.get("detail") or "")
        )
        for widget, text in (
            (self.task_file, file_name),
            (self.task_metrics, " · ".join(metrics)),
            (self.task_detail, detail),
        ):
            widget.setText(text)
            widget.setToolTip(text)
            widget.setVisible(bool(text))
        if error and not complete:
            self.task_detail.setToolTip(tr("详细信息：\n{error}", error=error))
        self.task_file.setVisible(bool(file_name) and phase not in {"queued", "done"} and not self._short)
        detail_tone = "warning" if phase in {"failed", "deferred"} else "secondary"
        self.task_detail.setStyleSheet(f"color: {color(detail_tone)};")
        self._task.setToolTip(
            "\n".join(text for text in (phase_text, file_name, " · ".join(metrics), error or detail) if text)
        )

    def retheme(self) -> None:
        """刷新弹出菜单、行状态及任务进度颜色，并重绘环形按钮和进度轨道。"""
        self.more.menu().setStyleSheet(menu_stylesheet())
        self._set_state_appearance()
        if self.task_snapshot:
            self._render_task()
        self.pause_button.retheme()
        self.progress.update()

    def refresh_actions(self) -> None:
        """结合宿主操作门禁、检查状态和任务阶段启用更新、暂停、恢复及停止按钮。"""
        self.update_button.setEnabled(
            self.dialog.actions_enabled and not self.dialog._checking and self.state not in {"updating", "done"}
        )
        self.more.setEnabled(self.state != "updating")
        self.pause_button.setEnabled(self.dialog.actions_enabled and self.phase in PAUSABLE_PHASES | {"paused"})
        self.stop_button.setEnabled(self.dialog.actions_enabled and self.phase in PAUSABLE_PHASES | {"paused"})

    def set_compact(self, compact: bool, *, short: bool = False) -> None:
        """按紧凑和低高度模式调整边距、按钮排列及详情密度，保留当前任务状态并重新计算布局。"""
        if (self._compact, self._short) == (compact, short):
            return
        self._compact, self._short = compact, short
        self._root.setContentsMargins(2 if compact else 4, 9 if short else 14, 2 if compact else 4, 9 if short else 14)
        self._root.setSpacing(6 if short else 9 if compact else 12)
        self._grid.setHorizontalSpacing(8 if compact else 13)
        self._actions.layout().setDirection(
            QBoxLayout.Direction.TopToBottom if compact else QBoxLayout.Direction.LeftToRight
        )
        self._actions.layout().setContentsMargins(0 if compact else 7, 0, 0, 0)
        self._actions.layout().setSpacing(1 if compact else 6)
        self.update_button.setFixedSize(76, 27 if compact else 30)
        self.more.setFixedSize(26 if compact else 30, 24 if compact else 30)
        self._actions.layout().setAlignment(self.more, Qt.AlignmentFlag.AlignHCenter)
        self._version.layout().setSpacing(4 if compact else 7)
        self._log_margin.layout().setContentsMargins(68 if compact else 73, 0, 0 if compact else 36, 0)
        self._task_margin.layout().setContentsMargins(68 if compact else 73, 0, 0 if compact else 36, 0)
        self._task.layout().setSpacing(3 if short else 4)
        self.setStyleSheet(
            """
            QPushButton#updateSingle { min-height: 25px; max-height: 25px; }
            QPushButton#updateMore { min-height: 22px; max-height: 22px; }
        """
            if compact
            else ""
        )
        self.date_hint.setVisible(not compact and not self.info.current_version and not self.info.latest_version)
        if self.task_snapshot:
            self._render_task()
        self.updateGeometry()
