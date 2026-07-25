"""标签编辑器及确认窗口的内部控件。"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QColorDialog,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from src.models.tag import TAG_PRESET_NAMES, TAG_PRESETS, TagDefinition
from src.ui import acrylic, icons, theme
from src.ui.widgets.tag_chip import TagChip
from src.utils.i18n import t as tr


def plain_label(text="", parent=None) -> QLabel:
    """创建按纯文本显示的标签，避免用户输入的名称被解释为富文本。"""
    label = QLabel(text, parent)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class CompositionLineEdit(QLineEdit):
    """输入法触发文字变化前暴露组词状态。"""

    composition_finished = Signal()

    def __init__(self, parent=None):
        """初始化组词及输入法事件处理标志，供外层编辑器判断是否可以提交。"""
        super().__init__(parent)
        self.composing = False
        self.handling_ime = False

    def inputMethodEvent(self, event) -> None:
        """在文字变化前公开组词状态，确保处理标志被复位，并在组词结束时发送通知。"""
        self.handling_ime = True
        self.composing = bool(event.preeditString())
        try:
            super().inputMethodEvent(event)
        finally:
            self.handling_ime = False
        if not self.composing:
            self.composition_finished.emit()

    def keyPressEvent(self, event) -> None:
        """组词期间消费回车和逗号，避免输入法确认被同时当作标签提交。"""
        if self.composing and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Comma):
            event.accept()
            return
        super().keyPressEvent(event)


class _ColorSwatch(QPushButton):
    """显示预设颜色及选中标记，并将方向键转换为颜色切换请求。"""

    step_requested = Signal(int)

    def paintEvent(self, event) -> None:
        """在普通按钮表面叠加选中勾号，使颜色选择不只依赖边框区分。"""
        super().paintEvent(event)
        if self.isChecked():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(
                QPen(QColor("#202020"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            )
            painter.drawLine(QPointF(7, 12), QPointF(10, 15))
            painter.drawLine(QPointF(10, 15), QPointF(17, 8))

    def keyPressEvent(self, event) -> None:
        """将方向键映射为相邻颜色的选择步长，其余按键沿用按钮行为。"""
        steps = {Qt.Key.Key_Left: -1, Qt.Key.Key_Up: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Down: 1}
        if event.key() in steps:
            self.step_requested.emit(steps[event.key()])
            event.accept()
            return
        super().keyPressEvent(event)


class TagEditorCard(QFrame):
    """提供名称、预设颜色和 HEX 编辑控件，通过信号将修改交由标签草稿所有者处理。"""

    color_chosen = Signal(str)
    collapse_requested = Signal()

    def __init__(self, parent=None, *, compact=False):
        """按紧凑或常规布局组合标签预览、颜色选择和名称输入，并预留校验提示区域。"""
        super().__init__(parent)
        self.setObjectName("tag-editor-card")
        self.color_dialog = None
        self.setMinimumWidth(0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10 if compact else 12, 10, 10 if compact else 12, 10)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        pencil = QLabel()
        icons.apply_pixmap(pencil, "actions/edit", 14, "accent")
        heading.addWidget(pencil)
        self.title_label = plain_label(tr("编辑标签"))
        heading.addWidget(self.title_label)
        placeholder = TagDefinition("", "", "", "", "blue")
        self.preview = TagChip(placeholder, height=28, font_size=int(theme.font("size_secondary")))
        self.preview.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.preview.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        if not compact:
            heading.addWidget(self.preview)
        heading.addStretch(1)
        self.collapse_button = QPushButton("" if compact else tr("收起"))
        self.collapse_button.setAutoDefault(False)
        self.collapse_button.setAccessibleName(tr("收起标签编辑"))
        if compact:
            self.collapse_button.setFixedSize(24, 24)
            self.collapse_button.setIconSize(QSize(13, 13))
            self.collapse_button.setToolTip(tr("收起标签编辑"))
            self.collapse_button.setStyleSheet("QPushButton { border: 0; background: transparent; padding: 0; }")
            icons.apply_icon(self.collapse_button, "actions/chevron-up", "text.secondary")
        self.collapse_button.clicked.connect(self.collapse_requested)
        heading.addWidget(self.collapse_button)
        layout.addLayout(heading)
        if compact:
            self.preview.setMaximumWidth(200)
            layout.addWidget(self.preview)
            layout.addWidget(plain_label(tr("颜色")))

        colors = QHBoxLayout()
        colors.setSpacing(5 if compact else 7)
        if not compact:
            colors.addWidget(plain_label(tr("颜色")))
        self.swatches = {}
        self._color_group = QButtonGroup(self)
        for index, (key, value) in enumerate(TAG_PRESETS.items()):
            swatch = _ColorSwatch()
            swatch.setObjectName(f"tag-color-{key}")
            swatch.setCheckable(True)
            swatch.setAutoDefault(False)
            swatch.setFixedSize(24, 24)
            swatch.setToolTip(f"{tr(TAG_PRESET_NAMES[key])} · {value}")
            swatch.setAccessibleName(tr(TAG_PRESET_NAMES[key]))
            swatch.setStyleSheet(
                f"QPushButton {{ background: {value}; border: 1px solid #40808080; border-radius: 5px; padding: 0; }}"
                "QPushButton:checked { border: 2px solid #202020; color: #202020; }"
                "QPushButton:focus { border: 2px solid #ffffff; }"
            )
            self._color_group.addButton(swatch)
            swatch.clicked.connect(lambda checked=False, k=key: self.color_chosen.emit(k))
            swatch.step_requested.connect(lambda step, i=index: self._step_color(i, step))
            self.swatches[key] = swatch
            colors.addWidget(swatch)
        self.custom_button = QPushButton(tr("自定义"))
        self.custom_button.setAutoDefault(False)
        if compact:
            self.custom_button.setFixedHeight(26)
            self.custom_button.setStyleSheet(
                f"QPushButton {{ padding: 3px 7px; font-size: {theme.font('size_secondary')}px; }}"
            )
        self.custom_button.clicked.connect(self._choose_custom_color)
        colors.addWidget(self.custom_button)
        self.hex_input = CompositionLineEdit()
        self.hex_input.setObjectName("tag-color-hex")
        self.hex_input.setAccessibleName(tr("自定义标签颜色，HEX 色值"))
        self.hex_input.setPlaceholderText("#RRGGBB")
        self.hex_input.setMinimumWidth(70)
        if not compact:
            colors.addWidget(self.hex_input, 1)
        layout.addLayout(colors)
        if compact:
            hex_row = QHBoxLayout()
            hex_row.setSpacing(6)
            hex_row.addWidget(plain_label("HEX"))
            self.hex_input.setFixedHeight(30)
            hex_row.addWidget(self.hex_input, 1)
            layout.addLayout(hex_row)

        self.color_caption = plain_label()
        layout.addWidget(self.color_caption)
        self.color_error = plain_label()
        self.color_error.setWordWrap(True)
        self.color_error.setObjectName("tag-color-error")
        layout.addWidget(self.color_error)
        self.name_row = QFrame()
        if compact:
            self.name_row.setStyleSheet("QFrame { background: transparent; border: 0; }")
        name_layout = QVBoxLayout(self.name_row) if compact else QHBoxLayout(self.name_row)
        name_layout.setContentsMargins(0, 0, 0, 0)
        if compact:
            name_layout.setSpacing(4)
        name_layout.addWidget(plain_label(tr("名称")))
        self.name_input = CompositionLineEdit()
        self.name_input.setObjectName("tag-name")
        self.name_input.setAccessibleName(tr("标签名称"))
        if compact:
            self.name_input.setFixedHeight(30)
        name_layout.addWidget(self.name_input, 1)
        if compact:
            layout.insertWidget(2, self.name_row)
        else:
            layout.addWidget(self.name_row)
        self.name_error = plain_label()
        self.name_error.setWordWrap(True)
        self.name_error.setObjectName("tag-name-error")
        if compact:
            layout.insertWidget(3, self.name_error)
        else:
            layout.addWidget(self.name_error)
        self.note = plain_label()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.retheme()

    def retheme(self):
        """更新编辑卡片及错误提示的主题样式，样式相同时避免重复设置。"""
        dark = theme.current_theme() == "dark"
        surface, border = ("#252C32", "#39444C") if dark else ("#F2F7FB", "#D6E2EB")
        style = (
            f"QFrame#tag-editor-card {{ background: {surface}; border: 1px solid {border}; border-radius: 8px; }}"
            "QFrame#tag-editor-card QLabel { border: 0; background: transparent; "
            f"color: {theme.color('text.secondary')}; font-size: {theme.font('size_secondary')}px; }}"
            "QFrame#tag-editor-card QLabel#tag-name-error, "
            f"QFrame#tag-editor-card QLabel#tag-color-error {{ color: {theme.color('danger')}; }}"
        )
        if self.styleSheet() != style:
            self.setStyleSheet(style)

    def _step_color(self, index, step):
        """按方向循环选择相邻预设颜色，并把键盘焦点移动到对应色块。"""
        key = tuple(TAG_PRESETS)[(index + step) % len(TAG_PRESETS)]
        self.color_chosen.emit(key)
        self.swatches[key].setFocus()

    def show_color(self, color, *, automatic=False, raw_hex=None):
        """同步色块选择、HEX 输入和颜色说明，更新输入框时阻止反向触发编辑信号。"""
        self._color_group.setExclusive(False)
        for key, swatch in self.swatches.items():
            selected = key == color
            swatch.setChecked(selected)
        self._color_group.setExclusive(True)
        hex_value = raw_hex if raw_hex is not None else TAG_PRESETS.get(color, color)
        if self.hex_input.text() != hex_value:
            previous = self.hex_input.blockSignals(True)
            self.hex_input.setText(hex_value)
            self.hex_input.blockSignals(previous)
        self.color_caption.setText(tr(TAG_PRESET_NAMES.get(color, color)) + (tr(" · 自动") if automatic else ""))

    def _choose_custom_color(self):
        """复用可见的颜色窗口，或创建归顶层窗口所有的非阻塞选择器并发送选定色值。"""
        if self.color_dialog is not None and self.color_dialog.isVisible():
            self.color_dialog.raise_()
            return
        color = QColor(self.hex_input.text())
        if not color.isValid():
            color = QColor(TAG_PRESETS["blue"])
        # 颜色对话框归顶层所有，避免原生窗口处理影响标签面板和滚动区域的命中判断。
        self.color_dialog = QColorDialog(color, self.window())
        self.color_dialog.setWindowTitle(tr("自定义标签颜色"))
        self.color_dialog.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog)
        self.color_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.color_dialog.colorSelected.connect(lambda value: self.color_chosen.emit(value.name().upper()))
        # 仅对外壳应用磨砂，颜色预览保留准确的不透明颜色。
        acrylic.apply_dialog_acrylic(self.color_dialog, tint_alpha=0.70)
        self.color_dialog.open()


class TagRemovalDialog(QDialog):
    """确认游戏范围内的标签删除，接受后仅由所有者修改草稿。"""

    def __init__(self, tag, usage, parent=None):
        """展示标签引用数量及删除影响，将确认结果交由调用方修改草稿，默认选择取消。"""
        super().__init__(parent)
        self.setWindowTitle(tr("删除标签及所有引用？"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        summary = QHBoxLayout()
        summary.addWidget(TagChip(tag))
        self.usage_label = plain_label(tr("{usage} 个 Mod 引用", usage=usage))
        summary.addWidget(self.usage_label)
        summary.addStretch()
        layout.addLayout(summary)
        self.description_label = plain_label(
            tr("将从标签库删除此标签，并从当前游戏内引用它的 {usage} 个 Mod 中同步移除。", usage=usage)
        )
        self.description_label.setWordWrap(True)
        layout.addWidget(self.description_label)
        layout.addWidget(plain_label(tr("点击原编辑窗口的“保存”后生效。")))
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.cancel_button = QPushButton(tr("取消"))
        self.cancel_button.setDefault(True)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.confirm_button = QPushButton(tr("删除并同步移除"))
        self.confirm_button.setAutoDefault(False)
        self.confirm_button.clicked.connect(self.accept)
        buttons.addWidget(self.confirm_button)
        layout.addLayout(buttons)

    def resizeEvent(self, event) -> None:
        """按当前宽度重新计算最小高度，确保标题栏和换行说明完整显示。"""
        super().resizeEvent(event)
        layout = self.layout()
        if layout is not None:
            # 高度包含标题栏和实际换行文字，避免显示时裁剪内容。
            self.setMinimumHeight(layout.totalMinimumHeightForWidth(event.size().width()))

    def showEvent(self, event) -> None:
        """显示确认窗口时将焦点置于取消按钮。"""
        super().showEvent(event)
        self.cancel_button.setFocus()

    def keyPressEvent(self, event) -> None:
        """消费 Ctrl+Enter，避免标签删除确认响应外层编辑器的保存快捷键。"""
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            event.accept()
            return
        super().keyPressEvent(event)
