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
