"""Mod 卡片、表格和预览共用的只读标签行。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOption,
    QStyleOptionViewItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.models.tag import TagDefinition, default_tag_color, tag_key
from src.ui import icons, theme
from src.ui.widgets.tag_chip import TagChip, TagFlowLayout
from src.ui.win_effects import acrylic_surface_alpha
from src.utils.i18n import t as tr


def tags_for_names(names: Iterable[str], catalog: Mapping[str, TagDefinition]) -> list[TagDefinition]:
    """解析规范化名称缓存，独立控件使用稳定本地标识。"""
    result, seen = [], set()
    for name in names or ():
        if not isinstance(name, str) or not (key := tag_key(name)) or key in seen:
            continue
        seen.add(key)
        result.append(catalog.get(key) or TagDefinition(f"local:{key}", "", name, key, default_tag_color(name)))
    return result


def show_tag_popup(popup: QWidget, anchor: QWidget, width: int = 420) -> None:
    """在控件所在屏幕的上下可用区域放置浮层。"""
    bounds = anchor.screen().availableGeometry().adjusted(8, 8, -8, -8)
    popup.setFixedWidth(min(width, bounds.width()))
    popup.ensurePolished()
    popup.layout().activate()
    height = min(420, popup.sizeHint().height(), bounds.height())
    bottom = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
    top = anchor.mapToGlobal(QPoint(0, -4))
    y = bottom.y() if bottom.y() + height <= bounds.bottom() else top.y() - height
    y = max(bounds.top(), min(y, bounds.bottom() - height + 1))
    x = max(bounds.left(), min(bottom.x(), bounds.right() - popup.width() + 1))
    popup.resize(popup.width(), height)
    popup.move(x, y)
    popup.show()
    popup.raise_()


class TagPopup(QFrame):
    """主题化弹窗，使用原生点击外部和退出键关闭语义。"""

    dismissed = Signal()

    def __init__(self, parent=None, *, acrylic=True):
        """创建标签弹出窗口，按配置启用透明亚克力表面并应用当前主题。"""
        super().__init__(parent, Qt.WindowType.Popup)
        self._acrylic = acrylic
        self.setObjectName("tagReadPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation)
        if acrylic:
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.retheme()

    def retheme(self) -> None:
        """按亚克力开关更新浮层底色、文字、边框及子控件透明样式。"""
        fill = theme.rgba("bg.card", acrylic_surface_alpha(0.68)) if self._acrylic else theme.color("bg.card")
        self.setStyleSheet(
            f"QFrame#tagReadPopup {{ background: {fill}; color: {theme.color('text.primary')};"
            f" border: 1px solid {theme.color('stroke.control')}; border-radius: 8px; }}"
            "QFrame#tagReadPopup QWidget { background: transparent; }"
            "QFrame#tagReadPopup QScrollArea { border: none; }"
        )

    def paintEvent(self, event) -> None:
        """透明模式下显式绘制样式表背景，普通模式沿用原生边框绘制。"""
        if not self._acrylic:
            super().paintEvent(event)
            return
        # 透明属性关闭自动背景绘制，样式表面仅绘制一次。
        option = QStyleOption()
        option.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_Widget, option, painter, self)

    def showEvent(self, event) -> None:
        """显示前同步最新主题，并为亚克力浮层应用圆角、模糊及窗口过渡设置。"""
        # 缓存弹窗可能在主题切换期间保持隐藏。
        self.retheme()
        if self._acrylic:
            from src.ui import win_effects

            win_effects.disable_open_close_transition(self)
            win_effects.apply_rounded_corners(self)
            win_effects.apply_acrylic_blurbehind(self, dark=theme.current_theme() == "dark")
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        """在浮层隐藏后发出关闭通知，供使用方清理选择或展开状态。"""
        super().hideEvent(event)
        self.dismissed.emit()


class _MoreButton(QToolButton):
    """显示溢出标签数量，并隔离可能触发外层卡片操作的双击事件。"""

    def mouseDoubleClickEvent(self, event) -> None:
        """消费双击事件，防止查看全部标签时激活外层 Mod。"""
        event.accept()


class TagStrip(QWidget):
    """显示一行标签及溢出数量，或使用不限行数的换行区域。"""

    tag_clicked = Signal(str)

    def __init__(self, parent=None, *, limit: int | None = 1, height=18, font_size=10):
        """按显示数量限制选择单行或流式布局，并建立可复用标签槽位、空提示和更多入口。"""
        super().__init__(parent)
        self._tags: list[TagDefinition] = []
        self._limit = limit
        self._chip_height = height
        self._font_size = font_size
        self._popup = None
        self._render_signature = None
        self._chips: list[TagChip] = []
        self._layout_signature = None
        self.setMinimumWidth(0)
        self.setStyleSheet("background: transparent;")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        if limit is None:
            self._layout = TagFlowLayout(self, spacing=6)
        else:
            self._layout = QHBoxLayout(self)
            self._layout.setContentsMargins(0, 0, 0, 0)
            self._layout.setSpacing(4)
            self.setFixedHeight(height + 2)
        self._more_button = _MoreButton(self)
        self._more_button.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation)
        self._more_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._more_button.setFixedHeight(height)
        self._more_button.setAccessibleName(tr("查看全部标签"))
        self._more_button.clicked.connect(self._show_all)
        self._more_button.hide()
        self._empty_label = QLabel(tr("暂无标签"), self)
        self._empty_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.set_tags([])

    def set_tags(self, tags: Iterable[TagDefinition]) -> None:
        """保存本次标签序列并刷新可见标签及溢出数量。"""
        self._tags = list(tags)
        self._render()

    def set_limit(self, limit: int) -> None:
        """在标签显示上限变化时重新计算可见条目和更多入口。"""
        if self._limit != limit:
            self._limit = limit
            self._render()

    def _render(self) -> None:
        """关闭旧浮层，按标签和主题签名复用按钮，并更新可见槽位、空提示及溢出数量。"""
        if self._popup is not None:
            self._popup.close()
            self._popup.deleteLater()
            self._popup = None
        signature = (
            tuple((tag.id, tag.name, tag.color) for tag in self._tags),
            self._limit,
            theme.current_theme(),
            tuple(
                theme.color(token)
                for token in (
                    "text.tertiary",
                    "text.secondary",
                    "bg.subtle",
                    "stroke.divider",
                    "accent",
                )
            ),
        )
        if signature == self._render_signature:
            return
        self._render_signature = signature
        visible = self._tags if self._limit is None else self._tags[: self._limit]
        for index, tag in enumerate(visible):
            if index == len(self._chips):
                chip = TagChip(tag, self, height=self._chip_height, font_size=self._font_size)
                chip.clicked_tag.connect(self.tag_clicked)
                self._chips.append(chip)
            else:
                chip = self._chips[index]
                chip.set_tag(tag)
            maximum_width = 100 if self._limit is not None else 260
            if chip.maximumWidth() != maximum_width:
                chip.setMaximumWidth(maximum_width)
        # 保留可见槽位容量，隐藏项不占布局空间，重绑时不将标签变为窗口。
        layout_signature = (len(self._chips), self._limit is None)
        if layout_signature != self._layout_signature:
            while self._layout.count():
                self._layout.takeAt(0)
            self._layout.addWidget(self._empty_label)
            for chip in self._chips:
                self._layout.addWidget(chip)
            self._layout.addWidget(self._more_button)
            if self._limit is not None:
                self._layout.addStretch()
            self._layout_signature = layout_signature
        for index, chip in enumerate(self._chips):
            chip.setVisible(index < len(visible))
        self._empty_label.setVisible(not visible)
        if not visible:
            empty_style = f"color: {theme.color('text.tertiary')}; font-size: {self._font_size}px;"
            if self._empty_label.styleSheet() != empty_style:
                self._empty_label.setStyleSheet(empty_style)
        remaining = len(self._tags) - len(visible)
        if remaining:
            self._more_button.setText(f"+{remaining}")
            self._more_button.setToolTip(tr("还有 {remaining} 个标签，点击查看全部", remaining=remaining))
            more_style = (
                f"QToolButton {{ color: {theme.color('text.secondary')};"
                f" background: {theme.color('bg.subtle')}; border: 1px solid {theme.color('stroke.divider')};"
                f" border-radius: {self._chip_height // 2}px; padding: 0 5px; font-size: {self._font_size}px; }}"
                f"QToolButton:hover {{ border-color: {theme.color('accent')}; }}"
            )
            if self._more_button.styleSheet() != more_style:
                self._more_button.setStyleSheet(more_style)
        self._more_button.setVisible(bool(remaining))
        self.updateGeometry()

    def _show_all(self) -> None:
        """创建可滚动的全部标签浮层，按屏幕空间限制尺寸并连接标签选择信号。"""
        if self._popup is not None:
            self._popup.deleteLater()
        self._popup = TagPopup(self)
        layout = QVBoxLayout(self._popup)
        layout.setContentsMargins(12, 12, 12, 12)
        heading = QHBoxLayout()
        heading.addWidget(QLabel(tr("全部标签")))
        heading.addStretch()
        close = QToolButton()
        icons.apply_icon(close, "actions/dismiss")
        close.setAccessibleName(tr("关闭全部标签"))
        close.clicked.connect(self._popup.close)
        heading.addWidget(close)
        layout.addLayout(heading)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = TagStrip(limit=None, height=25, font_size=int(theme.font("size_secondary")))
        content.set_tags(self._tags)
        content.tag_clicked.connect(self._choose_from_popup)
        scroll.setWidget(content)
        width = min(360, self.screen().availableGeometry().width() - 16)
        content_height = content.layout().heightForWidth(max(1, width - 28)) + 2
        scroll.setFixedHeight(min(290, max(46, content_height)))
        layout.addWidget(scroll)
        note = QLabel(tr("点击标签可筛选当前类别中的 Mod"))
        note.setWordWrap(True)
        layout.addWidget(note)
        show_tag_popup(self._popup, self._more_button, 360)

    def _choose_from_popup(self, tag_id: str) -> None:
        """关闭全部标签浮层，再将选中的标签标识交给使用方。"""
        self._popup.close()
        self.tag_clicked.emit(tag_id)


class TagNameCell(QWidget):
    """普通名称保留表格选择行为，仅处理标签点击。"""

    tag_clicked = Signal(str)

    def __init__(self, name: str, tags: Iterable[TagDefinition], parent=None, *, updatable=False):
        """组合纯文本名称、可更新标记和标签行，仅将标签点击作为独立交互。"""
        super().__init__(parent)
        self._name = name
        self.setMinimumWidth(0)
        self.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(2)
        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        self._label = QLabel(name)
        self._label.setTextFormat(Qt.TextFormat.PlainText)
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._label.setMinimumWidth(0)
        self._label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._label.setToolTip(name)
        heading.addWidget(self._label, 1)
        if updatable:
            badge = QLabel(tr("可更新"))
            badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            badge.setStyleSheet(f"color: {theme.color('success')}; font-size: 10px;")
            heading.addWidget(badge)
        layout.addLayout(heading)
        self._tags = TagStrip(self, limit=2)
        self._tags.set_tags(tags)
        self._tags.tag_clicked.connect(self.tag_clicked)
        layout.addWidget(self._tags)

    def resizeEvent(self, event) -> None:
        """根据名称标签的实际宽度省略长名称，保留完整名称作为提示。"""
        super().resizeEvent(event)
        self._label.setText(
            self._label.fontMetrics().elidedText(self._name, Qt.TextElideMode.ElideRight, max(0, self._label.width()))
        )

    def sizeHint(self) -> QSize:
        """提供容纳名称和紧凑标签行的推荐表格单元格尺寸。"""
        return QSize(160, 52)


class TagNameDelegate(QStyledItemDelegate):
    """在名称控件下绘制原生选中和交替行背景。"""

    ROLE_HAS_TAG_CELL = Qt.ItemDataRole.UserRole + 4

    def paint(self, painter, option, index) -> None:
        """存在标签单元格时仅绘制原生行背景，避免与覆盖其上的名称控件重复绘制文字。"""
        if not index.data(self.ROLE_HAS_TAG_CELL):
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        opt.icon = QIcon()
        opt.widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
