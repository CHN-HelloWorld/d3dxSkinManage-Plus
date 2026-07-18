"""Fluent 设置布局共用的原生小组件。"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QBoxLayout,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui import icons, theme
from src.ui.settings_style import stroke, stroke_color
from src.ui.widgets.toggle_switch import ToggleSwitch
from src.utils.i18n import t as tr


def plain_widget() -> QWidget:
    """创建带透明设置容器属性的普通控件。"""
    widget = QWidget()
    widget.setProperty("settingsPlain", True)
    return widget


def text_label(text: str = "", role: str = "help", *, wrap: bool = False) -> QLabel:
    """创建使用角色属性匹配设置样式的纯文本标签，并配置可选换行。"""
    label = QLabel(text)
    label.setProperty("settingsRole", role)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    return label


def icon_label(name: str, size: int = 18, token: str = "settings.text") -> QLabel:
    """创建固定尺寸、居中且穿透鼠标的设置图标标签，并应用指定主题颜色。"""
    label = QLabel()
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setFixedSize(size, size)
    label.setProperty("settingsIcon", True)
    label.setStyleSheet("background: transparent; border: none;")
    label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    icons.apply_pixmap(label, f"settings/{name}", size, token)
    return label


def style_button(button: QPushButton, *, icon: str = "", link: bool = False) -> QPushButton:
    """为按钮标记普通或链接样式，按需安装设置图标后返回原按钮。"""
    button.setProperty("settingsButton", "link" if link else "standard")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if icon:
        icons.apply_icon(button, f"settings/{icon}", "settings.secondary")
        button.setIconSize(QSize(14, 14))
    return button


def settings_note(text: str) -> QWidget:
    """组合提示图标和可换行的弱化文字，返回限制最大宽度的设置说明容器。"""
    note = plain_widget()
    note.setMaximumWidth(930)
    layout = QHBoxLayout(note)
    layout.setContentsMargins(2, 2, 2, 2)
    layout.setSpacing(7)
    layout.addWidget(icon_label("info", 13, "settings.muted"), 0, Qt.AlignmentFlag.AlignTop)
    layout.addWidget(text_label(text, "muted", wrap=True), 1)
    return note


class SettingsRow(QFrame):
    """说明与控件并列，空间不足时纵向排列。"""

    def __init__(
        self,
        title: str,
        control,
        description: str = "",
        *,
        extra=None,
        dense: bool = False,
        details: str = "",
        keywords: str = "",
    ):
        """组合设置标题、说明与控件，建立可搜索文本及无障碍关联。

        支持开关状态文字、附加内容和可展开详情，并记录宽度阈值以适应紧凑布局。
        """
        super().__init__()
        self.setObjectName("settingsRow")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.title_text = title
        self.search_text = f"{title} {description} {details} {keywords}"
        self._compact = None
        self._label_width = 180 if dense else 310
        self._compact_threshold = 430 if dense else 540
        self._gap = 16 if dense else 24
        self._state_label = None
        self._toggle = control if isinstance(control, ToggleSwitch) else None
        self._fit_control_text = isinstance(control, QWidget) and bool(control.property("settingsFitControlText"))
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 8, 0, 8) if dense else self._outer.setContentsMargins(20, 14, 20, 14)
        self._outer.setSpacing(6)
        self._pair = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._pair.setSpacing(self._gap)
        self._info = plain_widget()
        self._info.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        info = QVBoxLayout(self._info)
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(4)
        self.title_label = text_label(title, "label", wrap=True)
        info.addWidget(self.title_label)
        if description:
            self.description_label = text_label(description, "smallHelp" if dense else "help", wrap=True)
            info.addWidget(self.description_label)
        if extra is not None:
            if isinstance(extra, QWidget):
                info.addWidget(extra)
            else:
                info.addLayout(extra)
        self._controls = plain_widget()
        self._controls.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        controls = QHBoxLayout(self._controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        if isinstance(control, QWidget):
            if not control.accessibleName():
                control.setAccessibleName(title)
            self.title_label.setBuddy(control)
            controls.addWidget(control)
        else:
            controls.addLayout(control)
        if self._toggle is not None:
            self._state_label = text_label(tr("开") if self._toggle.isChecked() else tr("关"), "muted")
            controls.addWidget(self._state_label)
            self._toggle.toggled.connect(self._sync_toggle)
        controls.addStretch(1)
        self._pair.addWidget(self._info)
        self._pair.addWidget(self._controls)
        self._pair.addStretch(1)
        self._outer.addLayout(self._pair)
        if details:
            disclosure = style_button(QPushButton(tr("了解详情")), link=True)
            disclosure.setObjectName("settingsDetailsToggle")
            disclosure.setCheckable(True)
            detail_text = text_label(details, "help", wrap=True)
            detail_text.hide()
            disclosure.toggled.connect(detail_text.setVisible)
            disclosure.toggled.connect(lambda opened: disclosure.setText(tr("收起详情") if opened else tr("了解详情")))
            self._outer.addWidget(disclosure, 0, Qt.AlignmentFlag.AlignLeft)
            self._outer.addWidget(detail_text)

    def _sync_toggle(self, _checked: bool) -> None:
        # 依赖校验可以阻止点击，并在屏蔽信号时还原状态。
        """开关状态变化时刷新对应的开关文字。"""
        self.refresh()

    def paintEvent(self, event):
        """完成默认绘制后，按分隔属性在设置行顶部绘制主题分隔线。"""
        super().paintEvent(event)
        if self.property("settingsSeparator"):
            painter = QPainter(self)
            painter.setPen(stroke_color())
            painter.drawLine(20, 0, max(20, self.width() - 20), 0)

    def refresh(self) -> None:
        """将开关旁的状态文字同步到当前选中状态。"""
        if self._toggle is not None:
            self._state_label.setText(tr("开") if self._toggle.isChecked() else tr("关"))

    def showEvent(self, event):
        """设置行显示时同步开关文字并适配当前可用宽度。"""
        super().showEvent(event)
        if self._toggle is not None:
            self._sync_toggle(self._toggle.isChecked())
        self._adapt()

    def resizeEvent(self, event):
        """设置行尺寸变化时重新分配说明区和控件区布局。"""
        super().resizeEvent(event)
        self._adapt()

    def _adapt(self):
        """依据可用宽度和控件文字尺寸切换纵横排列，并设置说明栏及控件栏的宽度约束。

        仅在紧凑状态发生变化时使外部尺寸提示失效，随后激活内部布局。
        """
        margins = self._outer.contentsMargins()
        available = max(0, self.width() - margins.left() - margins.right())
        label_width = min(self._label_width, max(150, (available - 24) // 2))
        control_width = max(0, self._controls.layout().sizeHint().width()) if self._fit_control_text else 0
        compact = (
            available < self._compact_threshold
            or self._fit_control_text
            and label_width + self._gap + control_width > available
        )
        self._pair.setDirection(QBoxLayout.Direction.TopToBottom if compact else QBoxLayout.Direction.LeftToRight)
        self._pair.setSpacing(10 if compact else self._gap)
        if compact:
            self._info.setMinimumWidth(0)
            self._info.setMaximumWidth(16777215)
            self._controls.setMaximumWidth(16777215)
        else:
            self._info.setFixedWidth(label_width)
            self._controls.setMaximumWidth(max(310, control_width))
        if self._compact != compact:
            self._compact = compact
            self.updateGeometry()
        # 首次页面快照应使用最终行几何。
        self._outer.activate()


class SettingsCard(QFrame):
    """包含图标标题、设置内容和可选操作行的分区。"""

    def __init__(self, title: str, icon: str = "settings", description: str = "", badge: str = ""):
        """创建包含图标、标题、可选说明和徽标的设置分组卡片，并预留设置行布局。"""
        super().__init__()
        self.setObjectName("settingsGroup")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setMaximumWidth(1190 if badge else 930)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.title_text = title
        self.search_text = f"{title} {description} {badge}"
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(0, 0, 0, 0)
        self.outer.setSpacing(0)
        self.header = QWidget()
        self.header.setObjectName("settingsCardHeader")
        heading = QHBoxLayout(self.header)
        heading.setContentsMargins(18 if badge else 20, 12 if badge else 16, 20, 12 if badge else 16)
        heading.setSpacing(11)
        symbol = icon_label(icon, 20, "text.primary")
        symbol.setObjectName("settingsCardIcon")
        heading.addWidget(symbol)
        words = QVBoxLayout()
        words.setContentsMargins(0, 0, 0, 0)
        words.setSpacing(3)
        title_line = QHBoxLayout()
        title_line.setSpacing(8)
        title_line.addWidget(text_label(title, "heading"))
        if badge:
            title_line.addWidget(text_label(badge, "badge"), 0, Qt.AlignmentFlag.AlignVCenter)
        title_line.addStretch(1)
        words.addLayout(title_line)
        if description:
            words.addWidget(text_label(description, "smallHelp" if badge else "help", wrap=True))
        heading.addLayout(words, 1)
        self.outer.addWidget(self.header)
        self.rows = QVBoxLayout()
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(0)
        self.outer.addLayout(self.rows)
        self._footer_layout = None
