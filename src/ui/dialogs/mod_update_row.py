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
