"""根据当前命令表展示和配置快捷键。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui import icons, theme
from src.ui import settings_style as fluent
from src.ui.dialogs.shortcut_edit import ShortcutEditDialog
from src.ui.shortcut_manager import ShortcutSettings
from src.ui.smooth_scroll import SmoothScroller
from src.ui.widgets.settings_components import icon_label, plain_widget, settings_note
from src.ui.widgets.shortcut_controls import BindingButton, KeyCaps, ShortcutButton, ShortcutToast, button_style, color
from src.utils.i18n import t as tr


def _icon(name, token="text.secondary", size=16):
    """创建不截获鼠标事件的主题图标标签，使点击交由所在操作控件处理。"""
    label = QLabel()
    label.setFixedSize(size, size)
    label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    icons.apply_pixmap(label, name, size, token)
    return label


class _ShortcutRow(QWidget):
    """展示单条命令的用途、当前绑定、自定义状态和恢复默认入口。"""

    def __init__(self, command, binding, *, last=False):
        """构建命令说明与绑定操作列，按当前绑定是否偏离默认值显示恢复入口和自定义标记。"""
        super().__init__()
        self._last = last
        self.setObjectName("shortcutRow")
        self.setMinimumHeight(65)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(14)
        info = QVBoxLayout()
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(2)
        info.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        title_line = QHBoxLayout()
        title_line.setSpacing(7)
        title = QLabel(command.display_title)
        title.setObjectName("shortcutRowTitle")
        title.setMinimumHeight(21)
        title_line.addWidget(title)
        self.custom = QLabel(tr("自定义"))
        self.custom.setObjectName("shortcutCustom")
        self.custom.setFixedHeight(22)
        self.custom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_line.addWidget(self.custom)
        title_line.addStretch()
        info.addLayout(title_line)
        description = QLabel(command.display_description)
        description.setObjectName("shortcutDescription")
        description.setMinimumHeight(20)
        description.setWordWrap(True)
        description.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        info.addWidget(description)
        layout.addLayout(info, 1)
        binding_cell = QWidget()
        self._binding_cell = binding_cell
        binding_cell.setFixedWidth(170)
        cell = QHBoxLayout(binding_cell)
        cell.setContentsMargins(0, 0, 0, 0)
        self.button = BindingButton(command.display_title, binding, self)
        self.button.set_column_width(170)
        self.button.focus_changed.connect(self.update)
        cell.addWidget(self.button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(binding_cell)
        self.restore_button = ShortcutButton(tr("还原"))
        self.restore_button.setObjectName("shortcutRestore")
        self.restore_button.setAccessibleName(tr("还原{title}的快捷键", title=command.display_title))
        self.restore_button.setFixedWidth(54)
        self.restore_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.restore_button.setEnabled(binding != command.default)
        layout.addWidget(self.restore_button)
        self.custom.setVisible(binding != command.default)

    def set_compact(self, compact, narrow=False):
        """按紧凑与窄屏状态调整行高、内边距及快捷键列宽，保持各行对齐。"""
        inset = 14 if narrow else 20
        width = 155 if narrow else 170
        self.setMinimumHeight(60 if compact else 65)
        self.layout().setContentsMargins(inset, 8 if compact else 10, inset, 8 if compact else 10)
        self.layout().setSpacing(10 if narrow else 14)
        self._binding_cell.setFixedWidth(width)
        self.button.set_column_width(width)

    def paintEvent(self, event):
        """绘制悬停或绑定按钮聚焦背景及分隔线，末行保留分组面板的底部圆角。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.underMouse() or self.button.hasFocus():
            painter.setPen(Qt.PenStyle.NoPen)
            fill = QColor(fluent.color("soft"))
            if theme.is_background_active():
                fill = QColor(theme.color("text.primary"))
                fill.setAlphaF(0.04)
            painter.setBrush(fill)
            if self._last:
                path = QPainterPath()
                path.addRoundedRect(0, -8, self.width(), self.height() + 8, 7, 7)
                painter.drawPath(path)
            else:
                painter.drawRect(self.rect())
        painter.fillRect(20, 0, max(0, self.width() - 40), 1, fluent.stroke_color())

    def enterEvent(self, event):
        """鼠标进入时请求重绘悬停背景，再交由基类处理进入事件。"""
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        """鼠标离开时撤销悬停绘制，并保留基类事件处理。"""
        self.update()
        super().leaveEvent(event)


class ShortcutsPage(QWidget):
    """按命令目录展示快捷键分组，协调编辑、恢复默认、冲突提示与上次变更撤销。"""

    def __init__(self, settings: ShortcutSettings, parent=None, *, embedded=False):
        """构建独立或嵌入式快捷键页，连接命令目录与绑定变化，并建立编辑和撤销状态。"""
        super().__init__(parent)
        self._embedded = embedded
        self.settings = settings
        self._dialog = None
        self._undo_snapshot = None
        self.binding_buttons = {}
        self._rows = {}
        self._group_headings = []
        self._column_rows = []
        self._key_headings = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.scroll = None
        if not embedded:
            self.scroll = QScrollArea()
            self.scroll.setWidgetResizable(True)
            self.scroll.setFrameShape(QFrame.Shape.NoFrame)
            self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
            SmoothScroller.attach(self.scroll)
        else:
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
            self.setMaximumWidth(930)
        self._scroll_container = plain_widget()
        self._outer = QHBoxLayout(self._scroll_container)
        self._outer.setContentsMargins(0, 0, 0, 0) if embedded else self._outer.setContentsMargins(20, 22, 20, 24)
        content = plain_widget()
        content.setMaximumWidth(930)
        self._outer.addWidget(content)
        self._outer.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self._groups = plain_widget()
        self._groups_layout = QVBoxLayout(self._groups)
        self._groups_layout.setContentsMargins(0, 0, 0, 0)
        self._groups_layout.setSpacing(16)
        body.addWidget(self._groups)
        body.addSpacing(16)
        self._rebuild_groups()
        body.addWidget(settings_note(tr("系统保留组合键不可覆盖；快捷键冲突会在录入时提示。")))
        body.addSpacing(16)
        body.addWidget(self._build_system_card())
        body.addStretch(1)
        if embedded:
            root.addWidget(self._scroll_container)
        else:
            self.scroll.setWidget(self._scroll_container)
            root.addWidget(self.scroll, 1)
        self.toast = ShortcutToast(self)
        self.toast.undo_requested.connect(self.undo)
        settings.changed.connect(self.refresh)
        settings.catalog_changed.connect(self._rebuild_groups)
        self.retheme()
        self.refresh()

    def _rebuild_groups(self):
        """按当前命令目录重建分组与按钮映射，关闭已失效命令的编辑器并释放旧行控件。"""
        commands_by_id = {command.id: command for command in self.settings.commands}
        if self._dialog is not None and self._dialog.command.id not in commands_by_id:
            self._dialog.reject()
        while self._groups_layout.count():
            item = self._groups_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.binding_buttons.clear()
        self._rows.clear()
        self._group_headings.clear()
        self._column_rows.clear()
        self._key_headings.clear()
        bindings = self.settings.bindings
        for group_id, title, _icon_name in self.settings.groups:
            commands = [command for command in commands_by_id.values() if command.group == group_id]
            if not commands:
                continue
            group = QFrame()
            group.setObjectName("shortcutGroup")
            group_layout = QVBoxLayout(group)
            group_layout.setContentsMargins(0, 0, 0, 0)
            group_layout.setSpacing(0)
            heading = plain_widget()
            heading.setObjectName("shortcutGroupHeading")
            heading_layout = QHBoxLayout(heading)
            heading_layout.setContentsMargins(20, 16, 20, 16)
            heading_layout.setSpacing(11)
            symbol = icon_label("keyboard", 20)
            symbol.setObjectName("shortcutHeaderIcon")
            heading_layout.addWidget(symbol)
            group_title = QLabel(title if group_id.startswith("plugin:") else tr(title))
            group_title.setMinimumHeight(20)
            heading_layout.addWidget(group_title)
            heading_layout.addStretch()
            self._group_headings.append(heading)
            group_layout.addWidget(heading)
            columns = plain_widget()
            columns.setObjectName("shortcutColumns")
            columns.setFixedHeight(36)
            header = QHBoxLayout(columns)
            header.setContentsMargins(20, 7, 20, 7)
            header.setSpacing(14)
            header.addWidget(QLabel(tr("操作")), 1)
            key_title = QLabel(tr("快捷键"))
            key_title.setFixedWidth(170)
            header.addWidget(key_title)
            action_title = QLabel(tr("操作"))
            action_title.setFixedWidth(54)
            header.addWidget(action_title)
            self._column_rows.append(columns)
            self._key_headings.append(key_title)
            group_layout.addWidget(columns)
            for index, command in enumerate(commands):
                row = _ShortcutRow(command, bindings[command.id], last=index == len(commands) - 1)
                row.button.clicked.connect(lambda _checked=False, cid=command.id: self.open_editor(cid))
                row.restore_button.clicked.connect(
                    lambda _checked=False, cid=command.id: self.open_editor(cid, restore=True)
                )
                self.binding_buttons[command.id] = row.button
                self._rows[command.id] = row
                group_layout.addWidget(row)
            self._groups_layout.addWidget(group)

    def attach_scroll(self, scroll: QScrollArea) -> None:
        """使用分类自身视口滚动，并显示撤销反馈。"""
        if self._embedded:
            self.scroll = scroll
            self.toast.setParent(scroll.viewport())

    def _build_system_card(self):
        """创建不可自定义的系统交互说明，默认折叠并绑定展开状态与方向图标。"""
        card = QFrame()
        card.setObjectName("shortcutSystem")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.system_toggle = ShortcutButton()
        self.system_toggle.setObjectName("shortcutSystemToggle")
        self.system_toggle.setAccessibleName(tr("系统交互"))
        self.system_toggle.setCheckable(True)
        self.system_toggle.setMinimumHeight(45)
        self.system_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        summary = QHBoxLayout(self.system_toggle)
        summary.setContentsMargins(16, 13, 16, 13)
        summary.setSpacing(8)
        summary.addWidget(_icon("actions/lock"))
        title = QLabel(tr("系统交互"))
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        title.setObjectName("shortcutSystemTitle")
        summary.addWidget(title)
        chevron = _icon("actions/chevron-right")
        summary.addWidget(chevron)
        summary.addStretch()
        layout.addWidget(self.system_toggle)
        details = QWidget()
        lines = QVBoxLayout(details)
        lines.setContentsMargins(16, 0, 16, 14)
        lines.setSpacing(0)
        for key, text in (
            ("Esc", tr("取消录制、取消剪切，或关闭当前弹窗。")),
            ("Enter", tr("确认当前操作，由具有焦点的控件处理。")),
            ("Tab", tr("移动键盘焦点；Shift + Tab 反向移动。")),
        ):
            line = QFrame()
            line.setObjectName("shortcutSystemLine")
            row = QHBoxLayout(line)
            row.setContentsMargins(0, 9, 0, 9)
            row.setSpacing(12)
            caps = KeyCaps(key)
            caps.setFixedWidth(110)
            row.addWidget(caps)
            text_label = QLabel(text)
            text_label.setWordWrap(True)
            row.addWidget(text_label, 1)
            lines.addWidget(line)
        lines.addSpacing(8)
        note = QLabel(tr("输入框中的复制、粘贴、全选等保持文本编辑含义。"))
        note.setObjectName("shortcutDescription")
        note.setWordWrap(True)
        lines.addWidget(note)
        layout.addWidget(details)
        details.hide()

        def toggle(opened):
            """同步系统交互详情的可见性与展开箭头方向。"""
            details.setVisible(opened)
            icons.apply_pixmap(
                chevron, "actions/chevron-down" if opened else "actions/chevron-right", 16, "text.secondary"
            )

        self.system_toggle.toggled.connect(toggle)
        return card
