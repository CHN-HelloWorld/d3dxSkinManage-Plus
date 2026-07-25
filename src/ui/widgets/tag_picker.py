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

    @staticmethod
    def _action(text, icon, accessible_name, *, compact=False):
        """创建带图标和无障碍名称的候选操作按钮，紧凑模式只显示图标。"""
        button = QPushButton("" if compact else text)
        button.setAutoDefault(False)
        button.setFixedHeight(27)
        button.setIconSize(QSize(13, 13))
        icons.apply_icon(button, icon, "text.secondary")
        button.setAccessibleName(accessible_name)
        button.setToolTip(accessible_name)
        if compact:
            button.setFixedWidth(25)
        return button


class TagPicker(QWidget):
    """全部修改只作用于草稿，控件不访问持久化存储。"""

    changed = Signal()
    save_requested = Signal()

    def __init__(
        self,
        draft: TagDraft,
        parent=None,
        *,
        selected_height_limit: int | None = None,
        inline_editor=False,
        show_hints=True,
    ):
        """绑定标签草稿并建立输入、候选浮层和编辑控件，初始化校验状态及延后布局定时器。"""
        super().__init__(parent)
        self._draft = draft
        self._ready = False
        self._updating = False
        self._inline_editor = inline_editor
        self._show_hints = show_hints
        self._forwarding_wheel = False
        self._popup_boundary = None
        self._next_focus_widget = None
        self._active_tag_id = None
        self._armed_tag_id = None
        self._active_choice = -1
        self._choices = []
        self._rows = {}
        self._candidates_dirty = False
        self._candidate_reset_pending = False
        self._editor_anchor = None
        self._name_values = {}
        self._name_errors = {}
        self._hex_values = {}
        self._hex_errors = {}
        self._new_color = None
        self.removal_dialog = None
        self._selected_height_limit = selected_height_limit
        self._selected_scroll = None
        self._selected_content = None
        self._add_button = None
        self._selection_resize_timer = QTimer(self)
        self._selection_resize_timer.setSingleShot(True)
        self._selection_resize_timer.timeout.connect(self._update_selected_height)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._field = QFrame(self)
        self._field.setObjectName("tag-control")
        if selected_height_limit is None:
            self._flow = TagFlowLayout(self._field, margin=6, spacing=5)
        else:
            # 输入框独立于标签滚动区，避免已选标签挤出搜索、校验和操作按钮。
            field_layout = QVBoxLayout(self._field)
            field_layout.setContentsMargins(6, 6, 6, 4)
            field_layout.setSpacing(3)
            self._selected_scroll = QScrollArea(self._field)
            self._selected_scroll.setObjectName("tag-selected-scroll")
            self._selected_scroll.setFrameShape(QFrame.Shape.NoFrame)
            self._selected_scroll.setWidgetResizable(True)
            self._selected_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._selected_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self._selected_scroll.viewport().setAutoFillBackground(False)
            self._selected_content = QWidget()
            self._selected_content.setMinimumWidth(0)
            self._flow = TagFlowLayout(self._selected_content, margin=0, spacing=5)
            self._selected_scroll.setWidget(self._selected_content)
            field_layout.addWidget(self._selected_scroll)
        self.input = _TagInput(self._field)
        self.input.setObjectName("tag-input")
        self.input.setPlaceholderText(tr("搜索或添加标签…"))
        self.input.setAccessibleName(tr("搜索或添加标签"))
        self.input.setMinimumWidth(110)
        self.input.setFixedHeight(26)
        self.input.setStyleSheet("QLineEdit { border: 0; background: transparent; padding: 0 3px; }")
        if self._selected_scroll is None:
            self._flow.addWidget(self.input)
        else:
            input_row = QHBoxLayout()
            input_row.setSpacing(3)
            input_row.addWidget(self.input, 1)
            self._add_button = QToolButton()
            self._add_button.setObjectName("tag-add")
            self._add_button.setAccessibleName(tr("添加输入的标签"))
            self._add_button.setToolTip(tr("添加输入的标签"))
            self._add_button.setFixedSize(25, 25)
            self._add_button.setIconSize(QSize(14, 14))
            self._add_button.setStyleSheet(
                "QToolButton { border: 0; background: transparent; padding: 0; border-radius: 4px; }"
                f"QToolButton:hover {{ background: {theme.color('bg.hover')}; }}"
            )
            icons.apply_icon(self._add_button, "actions/add", "accent")
            self._add_button.clicked.connect(self._add_from_button)
            input_row.addWidget(self._add_button)
            field_layout.addLayout(input_row)
        layout.addWidget(self._field)
        help_row = QHBoxLayout()
        self._input_hint = plain_label(
            tr("Enter 添加 · 支持批量粘贴")
            if selected_height_limit is not None
            else tr("Enter 添加 · 支持逗号和换行粘贴")
        )
        self._input_hint.setToolTip(tr("Enter 添加标签；支持用逗号或换行分隔多个标签后粘贴。"))
        self._input_hint.setVisible(show_hints)
        help_row.addWidget(self._input_hint)
        help_row.addStretch()
        self._count_label = plain_label()
        help_row.addWidget(self._count_label)
        layout.addLayout(help_row)
        self.error_label = plain_label()
        self.error_label.setObjectName("tag-input-error")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        self._build_popup()
        self.input.textChanged.connect(self._input_changed)
        self.input.composition_finished.connect(self._composition_finished)
        self.name_input.textChanged.connect(self._name_changed)
        self.name_input.composition_finished.connect(lambda: self._name_changed(self.name_input.text()))
        self.hex_input.textChanged.connect(self._hex_changed)
        self.hex_input.composition_finished.connect(lambda: self._hex_changed(self.hex_input.text()))
        self._editor.color_chosen.connect(self._set_color)
        self._editor.collapse_requested.connect(self.collapse_editor)
        self._reposition_timer = QTimer(self)
        self._reposition_timer.setSingleShot(True)
        self._reposition_timer.timeout.connect(self._position_popup)
        self._focus_check_timer = QTimer(self)
        self._focus_check_timer.setSingleShot(True)
        self._focus_check_timer.timeout.connect(self._hide_if_focus_outside)
        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.timeout.connect(self._reveal_inline_editor)
        self._ready = True
        self._refresh_chips()
        self._refresh_candidates()
        self._retheme()

    @property
    def draft(self) -> TagDraft:
        """返回正在编辑的标签草稿，其持久化由外层窗口负责。"""
        return self._draft

    @property
    def name_input(self):
        """提供当前标签编辑卡片中的名称输入控件。"""
        return self._editor.name_input

    @property
    def hex_input(self):
        """提供颜色编辑和校验使用的 HEX 输入控件。"""
        return self._editor.hex_input

    @property
    def editor_card(self):
        """返回候选浮层共用的标签编辑卡片。"""
        return self._editor

    @property
    def is_dirty(self):
        """判断草稿、未提交文本、校验错误或组词状态是否仍需要用户处理。"""
        return bool(
            self.draft.is_dirty
            or self.input.text().strip()
            or self._name_errors
            or self._hex_errors
            or self.is_composing
        )
