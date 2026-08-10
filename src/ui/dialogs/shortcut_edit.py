"""快捷键录制、冲突替换及恢复默认的模态对话框。"""

from __future__ import annotations

from html import escape

from PySide6.QtCore import QEvent, QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.core.shortcuts import validate_binding
from src.ui import anim, icons
from src.ui.dialog_chrome import decorate_dialog
from src.ui.shortcut_manager import ShortcutSettings, binding_from_event
from src.ui.widgets.shortcut_controls import KeyCaps, button_style, color
from src.utils.i18n import t as tr


def _label(text="", name="", *, wrap=False):
    """创建用于快捷键说明的标签，按需换行并采用不接受文本交互的尺寸策略。"""
    label = QLabel(text)
    label.setObjectName(name)
    label.setIndent(0)
    label.setWordWrap(wrap)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    return label


def _button(text: str, callback, *, primary=False, ghost=False):
    """创建并连接快捷键对话框按钮，记录主操作或透明按钮样式，关闭自动默认行为。"""
    button = QPushButton(text)
    button.setProperty("primary", primary)
    button.setProperty("ghost", ghost)
    button.setAutoDefault(False)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.clicked.connect(callback)
    return button


class _DialogContent(QWidget):
    """在录制区域外侧绘制焦点环，不占用内部布局空间。"""

    focus_target: QWidget | None = None

    def paintEvent(self, event):
        """在录制控件拥有焦点时，于其布局外侧绘制主题焦点环。"""
        super().paintEvent(event)
        target = self.focus_target
        if target is None or not target.hasFocus():
            return
        position = target.mapTo(self, QPoint())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(color("accent")), 2))
        painter.drawRoundedRect(
            QRectF(position.x() - 4, position.y() - 4, target.width() + 8, target.height() + 8), 12, 12
        )


class _PrototypeDialog(QDialog):
    """快捷键内容卡，共用应用的自定义标题栏与窗口边框。"""

    def __init__(self, parent: QWidget, title: str, subtitle: str):
        """以宿主顶层窗口为父对象创建固定宽度内容卡，并接入共享标题栏和边框。"""
        super().__init__(parent.window())
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setFixedWidth(470)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.card = QFrame()
        self.card.setObjectName("shortcutDialogCard")
        self.card.setFixedWidth(468)
        self.card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Maximum)
        layout.addWidget(self.card)
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)
        self.content = _DialogContent()
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(24, 24, 24, 24)
        self.body.setSpacing(0)
        self.title_label = _label(title, "shortcutDialogTitle")
        self.title_label.setMinimumHeight(31)
        self.body.addWidget(self.title_label)
        self.body.addSpacing(7)
        self.subtitle = _label(subtitle, "shortcutDialogSubtitle", wrap=True)
        self.subtitle.setMinimumHeight(20)
        self.body.addWidget(self.subtitle)
        card_layout.addWidget(self.content)
        self.footer = QFrame()
        self.footer.setObjectName("shortcutDialogFooter")
        self.buttons = QHBoxLayout(self.footer)
        self.buttons.setContentsMargins(24, 16, 24, 16)
        self.buttons.setSpacing(8)
        card_layout.addWidget(self.footer)
        self.setStyleSheet(
            button_style() + "QWidget { background: transparent; }"
            f"QLabel {{ background: transparent; border: none; color: {color('text.primary')}; font-size: 13px; }}"
            "#shortcutDialogCard { background: transparent; border: none; }"
            "#shortcutDialogTitle { font-size: 20px; font-weight: 600; }"
            f"#shortcutDialogSubtitle {{ color: {color('text.secondary')}; font-size: 12px; }}"
            "#shortcutDialogFooter { background: transparent; border: none;"
            f" border-top: 1px solid {color('stroke.card')}; border-bottom-left-radius: 11px;"
            " border-bottom-right-radius: 11px; }"
        )
        self.close_button = decorate_dialog(self).titlebar.close_button

    def keyPressEvent(self, event):
        """未按 Ctrl、Alt 或 Meta 修饰键时用回车激活聚焦按钮，忽略自动重复，其余按键交给 Qt。"""
        button = self.focusWidget()
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and not event.modifiers()
            & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)
            and isinstance(button, QAbstractButton)
        ):
            if not event.isAutoRepeat():
                button.click()
            event.accept()
            return
        super().keyPressEvent(event)
