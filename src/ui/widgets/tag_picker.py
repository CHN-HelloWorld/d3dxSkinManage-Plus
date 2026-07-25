"""标签输入及定位浮层，编辑结果仅写入草稿。"""

from __future__ import annotations

import copy
import re

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QMenu,
    QPushButton,
    QScrollArea,
    QScrollBar,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.models.tag import (
    MAX_TAGS,
    TagDefinition,
    TagDraft,
    default_tag_color,
    normalize_tag_color,
    normalize_tag_name,
    tag_key,
    validate_tag_name,
)
from src.ui import icons, theme
from src.ui.widgets.tag_chip import TagChip, TagFlowLayout
from src.ui.widgets.tag_editor import (
    CompositionLineEdit,
    TagEditorCard,
    TagRemovalDialog,
    plain_label,
)
from src.utils.i18n import t as tr

_FILTER_EVENT_TYPES = frozenset(
    (
        QEvent.Type.Wheel,
        QEvent.Type.KeyPress,
        QEvent.Type.FocusIn,
        QEvent.Type.MouseButtonPress,
        QEvent.Type.Resize,
        QEvent.Type.Move,
        QEvent.Type.Hide,
    )
)


class _TagInput(CompositionLineEdit):
    """粘贴时保留换行分隔符，包括右键菜单入口。"""

    def paste(self):
        """将剪贴板换行转换为标签分隔符后插入，保留批量粘贴的名称边界。"""
        raw = QApplication.clipboard().text()
        self.insert(re.sub(r"\r\n|[\r\n]", "，", raw))

    def keyPressEvent(self, event) -> None:
        """在未组词时将粘贴快捷键路由到自定义粘贴入口，其余按键交给输入法感知控件。"""
        if event.matches(QKeySequence.StandardKey.Paste) and not self.composing:
            self.paste()
            event.accept()
            return
        super().keyPressEvent(event)

    def createStandardContextMenu(self):
        """构造编辑菜单并按当前状态启停动作，使右键粘贴也保留标签分隔符。"""
        # Qt 内置粘贴可能绕过 Python 重写，需统一入口以保留换行边界。
        menu = QMenu(self)
        for title, callback, enabled, name in (
            (tr("撤销"), self.undo, self.isUndoAvailable(), ""),
            (tr("重做"), self.redo, self.isRedoAvailable(), ""),
            (tr("剪切"), self.cut, self.hasSelectedText(), ""),
            (tr("复制"), self.copy, self.hasSelectedText(), ""),
            (tr("粘贴"), self.paste, bool(QApplication.clipboard().text()), "tag-paste"),
            (tr("全选"), self.selectAll, bool(self.text()), ""),
        ):
            action = menu.addAction(title)
            action.setEnabled(enabled)
            action.setObjectName(name)
            action.triggered.connect(callback)
        return menu

    def contextMenuEvent(self, event) -> None:
        """在鼠标位置执行编辑菜单，并在菜单关闭后安排释放。"""
        menu = self.createStandardContextMenu()
        menu.exec(event.globalPos())
        menu.deleteLater()


class _TagPopup(QFrame):
    """在透明边框上一次绘制圆角表面及描边。"""

    def paintEvent(self, event) -> None:
        """直接覆盖绘制圆角背景与描边，避免透明表面重复叠加。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        painter.setPen(QPen(QColor(theme.color("stroke.control")), 1))
        painter.setBrush(QColor(theme.color("bg.layer")))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)


class _CandidateRow(QFrame):
    """显示可添加的标签候选及编辑、删除入口，已选标签保留状态提示。"""

    def __init__(self, tag, *, selected=False, prefix="", compact=False, parent=None):
        """按已选、新建和紧凑模式布置候选标签及操作按钮。"""
        super().__init__(parent)
        self.setObjectName("tag-candidate-row")
        self.setFixedHeight(40)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(7, 0, 4, 0)
        layout.setSpacing(3)
        if prefix:
            layout.addWidget(plain_label("+ " + prefix))
        self.chip = TagChip(tag, height=28, font_size=int(theme.font("size_secondary")))
        self.chip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.chip.setEnabled(not selected)
        self.chip.setAccessibleDescription(tr("当前 Mod 已选择") if selected else tr("添加到当前 Mod"))
        layout.addWidget(self.chip)
        if selected:
            check = plain_label()
            icons.apply_pixmap(check, "meta/check", 13, "accent")
            check.setAccessibleName(tr("当前 Mod 已选择"))
            layout.addWidget(check)
        layout.addStretch(1)
        self.edit_button = self.remove_button = None
        if not prefix:
            self.edit_button = self._action(
                tr("编辑"), "actions/edit", tr("编辑标签 {name}", name=tag.name), compact=compact
            )
            self.remove_button = self._action(
                tr("移除"),
                "actions/remove",
                tr("删除标签 {name} 及当前游戏内的所有引用", name=tag.name),
                compact=compact,
            )
            self.remove_button.setObjectName("tag-remove-action")
            layout.addWidget(self.edit_button)
            layout.addWidget(self.remove_button)
