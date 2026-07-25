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

    @property
    def is_composing(self):
        """判断搜索、名称或颜色输入框是否仍在输入法组词。"""
        return self.input.composing or self.name_input.composing or self.hex_input.composing

    @property
    def has_active_modal(self):
        """判断标签删除确认或颜色选择窗口是否正在显示。"""
        return bool(
            (self.removal_dialog and self.removal_dialog.isVisible())
            or (self._editor.color_dialog and self._editor.color_dialog.isVisible())
        )

    def set_popup_boundary(self, widget: QWidget) -> None:
        """设置浮层下边界及焦点保留区域，避免遮挡表单底部操作。"""
        self._popup_boundary = widget

    def set_next_focus_widget(self, widget: QWidget) -> None:
        """将浮层中的制表键导航交回原表单。"""
        self._next_focus_widget = widget
        self._link_tab_order()

    def set_draft(self, draft: TagDraft) -> None:
        """显示编辑窗口前配置当前标签目录。"""
        self.hide_popup()
        self._reveal_timer.stop()
        for owner, name in ((self, "removal_dialog"), (self._editor, "color_dialog")):
            dialog = getattr(owner, name, None)
            if dialog is not None:
                dialog.reject()
                dialog.deleteLater()
                setattr(owner, name, None)
        self._draft = draft
        self._active_tag_id = self._armed_tag_id = None
        self._active_choice = -1
        self._new_color = None
        for values in (self._name_values, self._name_errors, self._hex_values, self._hex_errors):
            values.clear()
        self._updating = True
        self.input.clear()
        self._updating = False
        self._show_error("")
        self._refresh_chips()
        self._refresh_candidates()
        if self._selected_scroll is not None:
            self._selected_scroll.verticalScrollBar().setValue(0)

    def _build_popup(self):
        """建立归宿主窗口所有的候选浮层，按配置将共用编辑器放在候选行内或独立区域。"""
        self.popup = _TagPopup(self.window())
        self.popup.setObjectName("tag-picker-popup")
        self.popup.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation)
        self.popup.hide()
        self.popup.setMinimumWidth(0)
        self._panel_layout = QVBoxLayout(self.popup)
        self._panel_layout.setContentsMargins(1, 1, 1, 1)
        self._panel_layout.setSpacing(0)
        heading = QWidget()
        heading.setObjectName("tag-picker-heading")
        head_layout = QHBoxLayout(heading)
        head_layout.setContentsMargins(12, 8, 12, 7)
        self._heading = plain_label(tr("可用标签"))
        self._caption = plain_label()
        head_layout.addWidget(self._heading)
        head_layout.addStretch()
        head_layout.addWidget(self._caption)
        self._panel_layout.addWidget(heading)
        self._candidate_scroll = QScrollArea()
        self._candidate_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._candidate_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._candidate_scroll.setWidgetResizable(True)
        self._candidate_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._candidate_scroll.setMinimumHeight(42)
        self._candidate_content = QWidget()
        self._candidate_layout = QVBoxLayout(self._candidate_content)
        self._candidate_layout.setContentsMargins(6, 0, 6, 4)
        self._candidate_layout.setSpacing(0)
        self._candidate_scroll.setWidget(self._candidate_content)
        self._panel_layout.addWidget(self._candidate_scroll, 1)
        self._editor_scroll = QScrollArea()
        self._editor_scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._editor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._editor_scroll.setWidgetResizable(True)
        self._editor_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._editor_container = QWidget()
        editor_layout = QVBoxLayout(self._editor_container)
        if self._inline_editor:
            editor_layout.setContentsMargins(0, 3, 0, 7)
        else:
            editor_layout.setContentsMargins(9, 9, 9, 9)
        self._editor = TagEditorCard(compact=self._inline_editor)
        editor_layout.addWidget(self._editor)
        self._editor_scroll.setWidget(self._editor_container)
        self._editor_scroll.hide()
        if self._inline_editor:
            self._candidate_layout.addWidget(self._editor_scroll)
        else:
            self._panel_layout.addWidget(self._editor_scroll)
        self._footer = plain_label(
            tr("Enter 添加 · Esc 收起") if self._inline_editor else tr("↑ ↓ 选择     Enter 添加     Esc 收起")
        )
        self._footer.setContentsMargins(12, 7, 12, 7)
        self._footer.setVisible(self._show_hints)
        self._panel_layout.addWidget(self._footer)

    def _retheme(self):
        """仅在样式变化时刷新输入区域、候选浮层和编辑卡片，避免调色板事件反复更新后代。"""
        # 仅样式实际变化时更新，避免调色板事件反复处理所有后代。
        field_style = (
            f"QFrame#tag-control {{ background: {theme.color('control.bg')}; "
            f"border: 1px solid {theme.color('stroke.control')}; border-radius: 4px; }}"
            "QScrollArea#tag-selected-scroll, QScrollArea#tag-selected-scroll > QWidget > QWidget "
            "{ background: transparent; border: 0; }"
        )
        if self._field.styleSheet() != field_style:
            self._field.setStyleSheet(field_style)
        error_style = f"color: {theme.color('danger')}; font-size: {theme.font('size_secondary')}px;"
        if self.error_label.styleSheet() != error_style:
            self.error_label.setStyleSheet(error_style)
        popup_style = (
            "QFrame#tag-picker-popup { background: transparent; border: none; }"
            "QWidget#tag-picker-heading, QFrame#tag-picker-popup QScrollArea, "
            "QFrame#tag-picker-popup QScrollArea > QWidget, "
            "QFrame#tag-picker-popup QScrollArea > QWidget > QWidget "
            "{ background: transparent; border: 0; }"
            f"QFrame#tag-candidate-row {{ border: 0; border-radius: 6px; background: transparent; }}"
            f"QFrame#tag-candidate-row[highlighted='true'] {{ background: {theme.color('bg.hover')}; }}"
            f"QFrame#tag-candidate-row[editing='true'] {{ background: {theme.color('accent.subtle')}; }}"
            f"QFrame#tag-candidate-row QPushButton {{ font-size: {theme.font('size_secondary')}px; "
            f"color: {theme.color('text.secondary')}; "
            "background: transparent; border: 0; padding: 3px 5px; }"
            f"QFrame#tag-candidate-row QPushButton:hover {{ background: {theme.color('bg.base')}; }}"
            f"QPushButton#tag-remove-action:hover {{ color: {theme.color('danger')}; }}"
        )
        if self.popup.styleSheet() != popup_style:
            self.popup.setStyleSheet(popup_style)
        label_style = f"color: {theme.color('text.secondary')}; font-size: {theme.font('size_secondary')}px;"
        for label in (self._heading, self._caption, self._footer, self._count_label, self._input_hint):
            if label.styleSheet() != label_style:
                label.setStyleSheet(label_style)
        self._editor.retheme()

    def _refresh_chips(self):
        """依据草稿重建已选标签与移除入口，保留输入框并更新数量及滚动区高度。"""
        self._flow.removeWidget(self.input)
        while self._flow.count():
            item = self._flow.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        for tag_id in self.draft.selected_ids:
            if tag_id in self.draft.deleted_ids:
                continue
            chip = TagChip(
                self.draft.definitions[tag_id],
                height=28,
                font_size=int(theme.font("size_secondary")),
                removable=True,
            )
            chip.setObjectName(f"selected-tag-{tag_id}")
            chip.setAccessibleName(tr("编辑标签 {name}", name=chip.tag.name))
            chip.setCheckable(True)
            chip.setChecked(tag_id == self._armed_tag_id)
            chip.clicked_tag.connect(self.open_editor)
            chip.removed.connect(self._detach)
            self._flow.addWidget(chip)
            chip.show()
        if self._selected_scroll is None:
            self._flow.addWidget(self.input)
        else:
            self._selection_resize_timer.start(0)
        self._count_label.setText(f"{len(self.draft.selected_ids)} / {MAX_TAGS}")
        self._field.updateGeometry()
        self.updateGeometry()

    def _update_selected_height(self):
        """按流式布局和高度上限调整已选标签区，必要时滚动显示待删除的标签。"""
        if self._selected_scroll is None:
            return
        height = self._flow.heightForWidth(max(1, self._selected_scroll.viewport().width()))
        self._selected_content.setMinimumHeight(height)
        self._selected_scroll.setFixedHeight(min(self._selected_height_limit, height))
        self._selected_scroll.setVisible(self._flow.count() > 0)
        if self._armed_tag_id:
            self._flow.activate()
            for index in range(self._flow.count()):
                chip = self._flow.itemAt(index).widget()
                if isinstance(chip, TagChip) and chip.tag.id == self._armed_tag_id:
                    self._selected_scroll.ensureWidgetVisible(chip, 0, 3)
                    break

    def _new_preview(self):
        """为有效且尚不存在的输入名称创建临时标签预览，名称无效或重复时返回 None。"""
        raw = self.input.text()
        key = tag_key(raw)
        if not key or any(
            tag.normalized_name == key
            for tag in self.draft.definitions.values()
            if tag.id not in self.draft.deleted_ids
        ):
            return None
        try:
            name = validate_tag_name(raw)
        except ValueError:
            return None
        return TagDefinition("", self.draft.game_id, name, key, self._new_color or default_tag_color(name))

    def _refresh_candidates(self, *, reset=False, force=False):
        """刷新候选及新建预览，保留编辑器、滚动位置和键盘选择；隐藏时延后重建到下次展开。"""
        if not force and not self.popup.isVisible():
            self._candidates_dirty = True
            self._candidate_reset_pending |= reset
            return
        reset |= self._candidate_reset_pending
        self._candidates_dirty = self._candidate_reset_pending = False
        old_scroll = self._candidate_scroll.verticalScrollBar().value()
        active_index = next((index for index, tag_id in enumerate(self._rows) if tag_id == self._active_tag_id), None)
        if self._inline_editor:
            # 重建相邻候选行时保留编辑器和焦点。
            self._candidate_layout.removeWidget(self._editor_scroll)
        self._editor_anchor = None
        while self._candidate_layout.count():
            item = self._candidate_layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self._choices, self._rows = [], {}
        query = self.input.text()
        candidates = self.draft.candidates(query)
        if self._inline_editor and self._active_tag_id:
            active_tag = self.draft.definitions.get(self._active_tag_id)
            if active_tag is not None and active_tag.id not in self.draft.deleted_ids:
                candidates = [tag for tag in candidates if tag.id != active_tag.id]
                # 名称修改时保留正在编辑标签的位置。
                candidates.insert(min(active_index or 0, len(candidates)), active_tag)
        self._heading.setText(tr("搜索标签") if tag_key(query) else tr("可用标签"))
        self._caption.setText(
            tr("{count} 个", count=len(candidates))
            if self._inline_editor
            else tr("{count} 个 · 点击气泡添加", count=len(candidates))
        )
        self._caption.setToolTip(tr("点击标签添加到当前 Mod"))
        for tag in candidates:
            selected = tag.id in self.draft.selected_ids
            row = _CandidateRow(tag, selected=selected, compact=self._inline_editor)
            row.chip.clicked_tag.connect(self._attach)
            if row.edit_button is not None:
                row.edit_button.clicked.connect(
                    lambda checked=False, tid=tag.id: self.open_editor(tid, from_candidate=True)
                )
                row.remove_button.clicked.connect(lambda checked=False, tid=tag.id: self.open_removal(tid))
            row.setProperty("editing", tag.id == self._active_tag_id)
            self._candidate_layout.addWidget(row)
            if self._inline_editor and tag.id == self._active_tag_id:
                self._editor_anchor = row
                self._candidate_layout.addWidget(self._editor_scroll)
            self._rows[tag.id] = row
            if not selected:
                self._choices.append((tag.id, row))
        preview = self._new_preview()
        if preview:
            row = _CandidateRow(preview, prefix=tr("新建"), compact=self._inline_editor)
            row.chip.clicked_tag.connect(lambda _: self._commit_input())
            self._candidate_layout.addWidget(row)
            if self._inline_editor and not self._active_tag_id:
                self._editor_anchor = row
                self._candidate_layout.addWidget(self._editor_scroll)
            self._choices.append((None, row))
        if not candidates and not preview:
            text = tr("输入新名称，开始添加标签。")
            if tag_key(query):
                try:
                    validate_tag_name(query)
                    text = tr("没有匹配的标签，输入新名称即可创建。")
                except ValueError as exc:
                    text = str(exc)
            empty = plain_label(text)
            empty.setWordWrap(True)
            empty.setContentsMargins(12, 12, 12, 12)
            self._candidate_layout.addWidget(empty)
        self._candidate_layout.addStretch()
        # 先显示新子控件并确定实际行高，再测量和定位浮层。
        for index in range(self._candidate_layout.count()):
            widget = self._candidate_layout.itemAt(index).widget()
            if widget is not None:
                widget.show()
        self._candidate_layout.activate()
        if reset:
            self._active_choice = 0 if query and self._choices else -1
        elif self._active_choice >= len(self._choices):
            self._active_choice = -1
        self._update_editor()
        self._link_tab_order()
        self._candidate_scroll.verticalScrollBar().setValue(old_scroll)
        self._highlight_choice()
        if self.popup.isVisible():
            self._position_popup()

    def _link_tab_order(self):
        """按候选行和编辑器的实际排列连接 Tab 顺序，末尾可返回外层表单控件。"""
        controls = [self.input]
        if self._add_button is not None:
            controls.append(self._add_button)
        editor_controls = list(self._editor.swatches.values())
        editor_controls.extend((self._editor.custom_button, self.hex_input))
        if self._inline_editor:
            editor_controls.insert(0, self.name_input)
        else:
            editor_controls.append(self.name_input)
        editor_controls.append(self._editor.collapse_button)
        editor_linked = False
        for tag_id, row in self._rows.items():
            if row.edit_button is not None:
                controls.extend((row.edit_button, row.remove_button))
            if self._inline_editor and tag_id == self._active_tag_id:
                controls.extend(editor_controls)
                editor_linked = True
        if not editor_linked:
            controls.extend(editor_controls)
        if self._next_focus_widget is not None:
            controls.append(self._next_focus_widget)
        for first, second in zip(controls, controls[1:]):
            QWidget.setTabOrder(first, second)

    def _update_editor(self):
        """显示当前标签或新建预览的编辑字段，保留原始输入和校验错误，并提示草稿修改的影响。"""
        tag = self.draft.definitions.get(self._active_tag_id) if self._active_tag_id else self._new_preview()
        if tag is None or tag.id in self.draft.deleted_ids:
            self._editor_scroll.hide()
            return
        creating = not self._active_tag_id
        self._editor_scroll.show()
        self._editor.title_label.setText(tr("新标签颜色") if creating else tr("编辑标签"))
        self._editor.collapse_button.setVisible(not creating)
        self._editor.name_row.setVisible(not creating)
        self._updating = True
        raw = self._name_values.get(tag.id, tag.name)
        if self.name_input.text() != raw:
            self.name_input.setText(raw)
        preview = copy.copy(tag)
        preview.name = normalize_tag_name(raw) or tr("未命名标签")
        self._editor.preview.set_tag(preview)
        self._editor.show_color(
            tag.color, automatic=creating and not self._new_color, raw_hex=self._hex_values.get(tag.id)
        )
        self._updating = False
        self._editor.name_error.setText(self._name_errors.get(tag.id, ""))
        self._editor.name_error.setVisible(bool(self._name_errors.get(tag.id)))
        self._editor.color_error.setText(self._hex_errors.get(tag.id, ""))
        self._editor.color_error.setVisible(bool(self._hex_errors.get(tag.id)))
        self.name_input.setAccessibleDescription(self._name_errors.get(tag.id, ""))
        self.hex_input.setAccessibleDescription(self._hex_errors.get(tag.id, ""))
        if creating:
            note = tr("新标签自动成为候选，保存后可在其他 Mod 中使用。")
        else:
            count = self.draft.usage(tag.id)
            note = (
                tr("名称与颜色将应用于 {count} 个 Mod，随本次保存生效。", count=count)
                if count
                else tr("名称与颜色随本次保存生效。")
            )
        self._editor.note.setText(note)

    def _input_changed(self, _text, *, allow_commit=True):
        """重置当前编辑选择，按分隔符决定提交或刷新候选，输入法处理期间延后操作。"""
        if self._updating or self.input.composing or self.input.handling_ime:
            return
        self._active_tag_id = self._armed_tag_id = None
        self._show_error("")
        if not self.input.text():
            self._new_color = None
            self._hex_values.pop("", None)
            self._hex_errors.pop("", None)
        if allow_commit and re.search(r"[,，\r\n]", self.input.text()):
            self._commit_input()
        else:
            self._refresh_chips()
            self._refresh_candidates(reset=True)
            self.show_popup()
        self.changed.emit()

    def _composition_finished(self):
        """在组词结束后刷新输入状态和候选，分隔符提交仍等待明确的确认操作。"""
        # 使用与普通输入相同的编辑状态重置，分隔符提交等待用户按下确认键。
        self._input_changed(self.input.text(), allow_commit=False)

    def _commit_input(self):
        """将分隔符拆分后的名称添加到草稿，校验失败时保留输入并提示原因，成功时返回 True。"""
        if self.is_composing:
            return False
        names = [raw for raw in re.split(r"[,，\r\n]+", self.input.text()) if normalize_tag_name(raw)]
        if not names:
            return False
        if "" in self._hex_errors:
            self._show_error(self._hex_errors[""])
            self.show_popup()
            self.hex_input.setFocus()
            return False
        already_selected = {
            self.draft.definitions[tag_id].normalized_name
            for tag_id in self.draft.selected_ids
            if tag_id not in self.draft.deleted_ids
        }
        if all(tag_key(name) in already_selected for name in names):
            self._show_error(tr("该标签已经在当前 Mod 中，无需重复添加。"))
            return False
        try:
            self.draft.add_names(names, color=self._new_color if len(names) == 1 else None)
        except ValueError as exc:
            self._show_error(tr("{exc} 输入已保留。", exc=exc))
            self._refresh_candidates(reset=True)
            self.show_popup()
            return False
        self._after_add()
        return True

    def _add_from_button(self):
        """把焦点交还输入框并尝试添加当前文本，未成功时保留候选浮层。"""
        self.input.setFocus()
        if not self._commit_input():
            self.show_popup()

    def _attach(self, tag_id):
        """将已有候选标签关联到当前 Mod 草稿，失败时显示校验原因。"""
        try:
            self.draft.attach(tag_id)
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self._after_add()

    def _after_add(self):
        """成功添加后清空临时输入和新建颜色状态，刷新标签及候选并通知草稿变化。"""
        self._active_tag_id = self._armed_tag_id = None
        self._new_color = None
        self._updating = True
        self.input.clear()
        self._updating = False
        self._hex_values.pop("", None)
        self._hex_errors.pop("", None)
        self._show_error("")
        self._active_choice = -1
        self._refresh_chips()
        self._refresh_candidates()
        self.show_popup()
        self.input.setFocus()
        self.changed.emit()

    def _detach(self, tag_id):
        """从当前 Mod 草稿解除标签关联，清除相关选择状态并刷新界面。"""
        self.draft.detach(tag_id)
        self._armed_tag_id = None
        if self._active_tag_id == tag_id:
            self._active_tag_id = None
        self._refresh_chips()
        self._refresh_candidates()
        self.input.setFocus()
        self.changed.emit()

    def open_editor(self, tag_id, *, from_candidate=False):
        """展开有效标签的编辑卡片并聚焦名称，候选入口会先清除未提交的新建状态。"""
        if tag_id in self.draft.deleted_ids or tag_id not in self.draft.definitions:
            return
        if from_candidate:
            self._updating = True
            self.input.clear()
            self._updating = False
            self._new_color = None
            self._hex_values.pop("", None)
            self._hex_errors.pop("", None)
            self._show_error("")
        self._active_tag_id = tag_id
        self._armed_tag_id = None
        self._active_choice = -1
        self._refresh_chips()
        self._refresh_candidates()
        self.show_popup()
        row = self._rows.get(tag_id)
        if row:
            self._candidate_scroll.ensureWidgetVisible(row)
        self.name_input.setFocus()
        self.name_input.selectAll()
        if self._inline_editor:
            self._reveal_timer.start(0)

    def collapse_editor(self):
        """结束当前标签编辑展示，刷新候选并把焦点交回添加输入框。"""
        self._active_tag_id = None
        self._refresh_candidates()
        self.input.setFocus()

    def _name_changed(self, raw):
        """保留原始名称输入并尝试更新草稿，记录校验错误后刷新预览和候选。"""
        if self._updating or not self._active_tag_id or self.name_input.composing or self.name_input.handling_ime:
            return
        tag_id = self._active_tag_id
        self._name_values[tag_id] = raw
        try:
            self.draft.rename(tag_id, raw)
        except ValueError as exc:
            self._name_errors[tag_id] = str(exc)
        else:
            self._name_errors.pop(tag_id, None)
        self._refresh_chips()
        self._refresh_candidates()
        self.changed.emit()

    def _hex_changed(self, raw):
        """校验完整 HEX 输入，保留错误文本并定位提示，有效时同步标签或新建预览颜色。"""
        if self._updating or self.hex_input.composing or self.hex_input.handling_ime:
            return
        tag_id = self._active_tag_id or ""
        self._hex_values[tag_id] = raw
        try:
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
                raise ValueError(tr("请输入完整的 #RRGGBB 色值。"))
            color = normalize_tag_color(raw)
        except ValueError as exc:
            self._hex_errors[tag_id] = str(exc)
            self._update_editor()
            self._position_popup()
        else:
            self._set_color(color, preserve_raw=True)
        self.changed.emit()

    def _set_color(self, color, *, preserve_raw=False):
        """清除颜色校验错误并更新草稿或新建颜色，按需保留原始 HEX 输入。"""
        tag_id = self._active_tag_id or ""
        self._hex_errors.pop(tag_id, None)
        if not preserve_raw:
            self._hex_values.pop(tag_id, None)
        if tag_id:
            self.draft.set_color(tag_id, color)
        else:
            self._new_color = color
        self._refresh_chips()
        self._refresh_candidates()
        self.changed.emit()

    def open_removal(self, tag_id):
        """为未删除的候选标签非阻塞打开全游戏引用删除确认，已有模态窗口时忽略请求。"""
        if self.has_active_modal or tag_id in self.draft.deleted_ids:
            return
        self.removal_dialog = TagRemovalDialog(self.draft.definitions[tag_id], self.draft.usage(tag_id), self.window())
        confirmation = self.removal_dialog

        def finished(result):
            """接受确认后在草稿标记标签及引用删除并清除编辑缓存，随后恢复候选浮层和输入焦点。"""
            if result == QDialog.DialogCode.Accepted:
                self.draft.remove_candidate(tag_id)
                for values in (self._name_values, self._name_errors, self._hex_values, self._hex_errors):
                    values.pop(tag_id, None)
                if self._active_tag_id == tag_id:
                    self._active_tag_id = None
                self._refresh_chips()
                self.changed.emit()
            self._refresh_candidates()
            self.show_popup()
            self.input.setFocus()

        confirmation.finished.connect(finished)
        confirmation.open()

    def _show_error(self, message):
        """同步输入错误文案、可见性和无障碍描述。"""
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))
        self.input.setAccessibleDescription(message)

    def validate(self) -> bool:
        """检查模态交互、组词、未提交输入及草稿错误，失败时定位对应控件，全部有效才允许保存。"""
        if self.has_active_modal:
            return False
        if self.is_composing:
            self._show_error(tr("请先完成输入法组词，再保存。"))
            return False
        if self.input.text().strip():
            self._show_error(tr("请先按 Enter 添加，或清空正在输入的标签。"))
            self.show_popup()
            self.input.setFocus()
            return False
        for tag_id in self._name_errors.keys() | self._hex_errors.keys():
            if tag_id and tag_id not in self.draft.deleted_ids:
                self.open_editor(tag_id)
                (self.name_input if tag_id in self._name_errors else self.hex_input).setFocus()
                return False
        try:
            self.draft.validate()
        except ValueError as exc:
            self._show_error(str(exc))
            self.show_popup()
            return False
        self._show_error("")
        return True

    def show_popup(self):
        """在控件可见且无模态交互时刷新待办候选，将浮层绑定到当前宿主并定位显示。"""
        if not self.isVisible() or self.has_active_modal:
            return
        if self._candidates_dirty:
            self._refresh_candidates(force=True)
        owner = self.window()
        if self.popup.parentWidget() is not owner:
            self.popup.setParent(owner)
        if owner is self:
            self.popup.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
            self.popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        if not self._position_popup():
            return
        self.popup.show()
        self.popup.raise_()

    def hide_popup(self):
        """停止焦点检查和编辑器显露定时器，隐藏浮层并取消待删除标签选择。"""
        self._focus_check_timer.stop()
        self._reveal_timer.stop()
        self.popup.hide()
        self._armed_tag_id = None

    def _hide_if_focus_outside(self):
        """仅在焦点确实离开编辑器、浮层及保留区域后收起浮层，忽略模态切换中的临时焦点。"""
        if not self.popup.isVisible() or self.has_active_modal:
            return
        focused = QApplication.focusWidget()
        # 子对话框关闭时，焦点可能暂留在已隐藏控件上；等待可见目标后再判断是否离开。
        if focused is None or not focused.isVisible():
            return
        inside = (
            focused is self or self.isAncestorOf(focused) or focused is self.popup or self.popup.isAncestorOf(focused)
        )
        boundary = self._popup_boundary
        saving = boundary is not None and (focused is boundary or boundary.isAncestorOf(focused))
        if not inside and not saving:
            self.hide_popup()

    def _position_popup(self):
        """按屏幕、宿主、滚动视口和表单边界定位浮层并分配编辑空间，锚点不可见时返回 False。"""
        if not self._ready or not self.isVisible():
            return False
        owner = self.window()
        anchor = QRect(self._field.mapToGlobal(QPoint()), self._field.size())
        screen = self.screen().availableGeometry().adjusted(8, 8, -8, -8)
        if not self.popup.isWindow():
            owner_rect = QRect(owner.mapToGlobal(QPoint()), owner.size()).adjusted(8, 8, -8, -8)
            screen = screen.intersected(owner_rect)
        ancestor = self.parentWidget()
        while ancestor and ancestor is not owner:
            if isinstance(ancestor, QScrollArea):
                viewport = ancestor.viewport()
                visible = QRect(viewport.mapToGlobal(QPoint()), viewport.size())
                if not visible.intersects(anchor):
                    self.hide_popup()
                    return False
            ancestor = ancestor.parentWidget()
        bottom = screen.bottom()
        if self._popup_boundary and self._popup_boundary.isVisible():
            bottom = min(bottom, self._popup_boundary.mapToGlobal(QPoint()).y() - 6)
        footer_height = self._footer.sizeHint().height() if not self._footer.isHidden() else 0
        fixed = self._heading.parentWidget().sizeHint().height() + footer_height + 2
        below, above = bottom - anchor.bottom() - 5, anchor.top() - screen.top() - 5
        if self._inline_editor:
            width = max(1, min(anchor.width(), screen.width()))
            self.popup.setFixedWidth(width)
            # 按锚点可用空间选择展开方向，编辑器在浮层内滚动。
            opens_below = below >= fixed + 42 or below >= above
            room = max(1, min(420, below if opens_below else above))
            overlay = False
            self._size_inline_candidates(height_limit=room - fixed)
            height = fixed + self._candidate_scroll.height()
        else:
            width = min(max(412, anchor.width()), screen.width())
            self.popup.setFixedWidth(max(180, width))
            self._editor_container.setFixedWidth(max(1, width - 2))
            self._editor_container.layout().activate()
            editor_height = self._editor_container.sizeHint().height() if not self._editor_scroll.isHidden() else 0
            required = min(420, fixed + editor_height + 84)
            opens_below = below >= required or below >= above
            room = below if opens_below else above
            overlay = room < fixed + editor_height + 42
            if overlay:
                room = bottom - screen.top() + 1
            room = max(80, min(420, room))
            editor_room = min(editor_height, max(0, room - fixed - 42))
            self._editor_scroll.setFixedHeight(editor_room)
            rows_height = min(210, max(42, self._candidate_content.sizeHint().height()))
            candidate_height = max(25, min(rows_height, room - fixed - editor_room))
            self._candidate_scroll.setMinimumHeight(min(42, candidate_height))
            self._candidate_scroll.setFixedHeight(candidate_height)
            height = fixed + candidate_height + editor_room
        self.popup.setFixedHeight(height)
        left = max(screen.left(), min(anchor.left(), screen.right() - width + 1))
        top = anchor.bottom() + 5 if opens_below and not overlay else anchor.top() - height - 5
        top = max(screen.top(), min(top, bottom - height + 1))
        position = QPoint(left, top)
        self.popup.move(position if self.popup.isWindow() else owner.mapFromGlobal(position))
        return True

    def _size_inline_candidates(self, *, height_limit=None):
        """在有界候选列表中将编辑器放在对应行下方。"""
        self.layout().activate()
        self._panel_layout.activate()
        self._candidate_layout.activate()
        self._editor_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 按实际视口宽度计算编辑区域，避免边距造成右侧裁剪。
        width = max(1, self._editor_scroll.viewport().width())
        self._editor_container.setFixedWidth(width)
        editor_layout = self._editor_container.layout()
        editor_layout.activate()
        editor_height = max(
            self._editor_container.sizeHint().height(),
            editor_layout.totalHeightForWidth(width),
        )
        self._editor_scroll.setFixedHeight(editor_height)
        self._candidate_layout.activate()
        editor_room = editor_height if not self._editor_scroll.isHidden() else 0
        height = min(144 + editor_room, max(42, self._candidate_content.sizeHint().height()))
        if height_limit is not None:
            height = min(height, max(1, height_limit))
        self._candidate_scroll.setMinimumHeight(min(42, height))
        self._candidate_scroll.setFixedHeight(height)
        self._panel_layout.activate()
        self.popup.updateGeometry()
        self.updateGeometry()

    def _reveal_inline_editor(self):
        """逐层滚动包含编辑器的滚动区域，使编辑卡片及其关联候选行可见。"""
        if not self.isVisible() or not self.popup.isVisible():
            return
        ancestor = self._editor.parentWidget()
        while ancestor is not None:
            if isinstance(ancestor, QScrollArea):
                ancestor.ensureWidgetVisible(self._editor, 0, 10)
                if self._editor_anchor is not None and ancestor.isAncestorOf(self._editor_anchor):
                    ancestor.ensureWidgetVisible(self._editor_anchor, 0, 10)
            ancestor = ancestor.parentWidget()
