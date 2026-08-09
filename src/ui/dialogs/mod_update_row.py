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
