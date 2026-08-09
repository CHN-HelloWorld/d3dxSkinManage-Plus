"""提供 Mod 元数据与标签草稿编辑，支持事务保存、放弃确认及窄窗口布局。"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QPoint, QSignalBlocker, QSize, Qt
from PySide6.QtGui import QAction, QTextOption
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from src.models.mod import AgeRating, Mod
from src.models.tag import TagCommitResult, TagDraft
from src.ui import acrylic, icons, theme
from src.ui.widgets.tag_editor import plain_label
from src.ui.widgets.tag_picker import TagPicker
from src.utils.i18n import t as tr
from src.utils.logger import logger


class _FormScrollArea(QScrollArea):
    """沿嵌套滚动区域显示目标控件。"""

    def ensureWidgetVisible(self, child, xmargin=50, ymargin=50) -> None:
        """依次请求嵌套滚动区显示目标控件，再调整外层滚动位置，避免验证错误字段仍被内层遮挡。"""
        ancestor = child.parentWidget()
        while ancestor is not None and ancestor is not self:
            if isinstance(ancestor, QScrollArea):
                ancestor.ensureWidgetVisible(child, xmargin, ymargin)
            ancestor = ancestor.parentWidget()
        super().ensureWidgetVisible(child, xmargin, ymargin)


class _SideScrollArea(QScrollArea):
    """保持侧栏父对象稳定，窄屏时交给外层表单滚动。"""

    def __init__(self) -> None:
        """初始化侧栏滚动区，默认使用横向双栏布局的独立滚动模式。"""
        super().__init__()
        self._stacked = False

    def set_stacked(self, stacked: bool) -> None:
        """切换侧栏宽度约束和滚动策略，纵向堆叠时由外层表单承担滚动。"""
        self._stacked = stacked
        self.setMinimumWidth(0 if stacked else 258)
        self.setMaximumWidth(16777215 if stacked else 258)
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff if stacked else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.updateGeometry()

    def minimumSizeHint(self) -> QSize:
        """在堆叠模式返回容纳内容最小高度的尺寸，避免外层布局压缩侧栏字段。"""
        hint = super().minimumSizeHint()
        if self._stacked and self.widget() is not None:
            hint.setHeight(self.widget().minimumSizeHint().height() + self.frameWidth() * 2)
        return hint

    def sizeHint(self) -> QSize:
        """堆叠模式按内容和边框计算建议尺寸，其他情况沿用滚动区默认建议。"""
        if self._stacked and self.widget() is not None:
            return self.widget().sizeHint() + QSize(self.frameWidth() * 2, self.frameWidth() * 2)
        return super().sizeHint()

    def event(self, event) -> bool:
        """完成默认事件处理后，在堆叠模式的布局请求中更新几何建议以同步内容高度。"""
        result = super().event(event)
        if event.type() == QEvent.Type.LayoutRequest and getattr(self, "_stacked", False):
            # 详情变化会更新内容最小高度，窄屏模式由外层滚动区承载。
            self.updateGeometry()
        return result


class _WrappingPathEdit(QTextEdit):
    """元数据按可用宽度换行并滚动，不扩大窗口。"""

    def __init__(self, parent: QWidget | None = None, *, height: int = 88) -> None:
        """创建固定高度、任意位置换行的只读纯文本区，长路径不扩大窗口宽度。"""
        super().__init__(parent)
        self.setObjectName("mod-edit-readonly")
        self.setReadOnly(True)
        self.setAcceptRichText(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.setTabChangesFocus(True)
        self.document().setDocumentMargin(4)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(height)
