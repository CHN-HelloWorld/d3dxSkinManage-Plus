"""编辑和浏览共用的主题化纯文本标签。"""

from __future__ import annotations

import html
from math import ceil

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QLayout, QLayoutItem, QSizePolicy, QToolButton, QToolTip

from src.models.tag import TagDefinition, tag_colors
from src.ui import icons, theme
from src.utils.i18n import t as tr


class _RemoveButton(QToolButton):
    """提供标签移除入口，并阻止双击事件传播到外层 Mod 卡片。"""

    def mouseDoubleClickEvent(self, event) -> None:
        """消费双击事件，避免移除标签时触发父控件的双击操作。"""
        event.accept()


class TagChip(QAbstractButton):
    """可访问按钮，可带独立获取焦点的移除按钮。"""

    clicked_tag = Signal(str)
    removed = Signal(str)

    def __init__(self, tag: TagDefinition, parent=None, *, height=24, font_size=11, removable=False, count=None):
        """创建带标签名称、可选数量和移除入口的按钮，并绑定标签标识信号。"""
        super().__init__(parent)
        self._tag = tag
        self._count = count
        self._height = height
        self._remove_button = None
        self.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(height)
        self.setMaximumWidth(260)
        font = self.font()
        font.setPixelSize(font_size)
        self.setFont(font)
        # 全局样式准备后仍保持紧凑尺寸。
        self.setStyleSheet(f"TagChip {{ font-size: {font_size}px; }}")
        if removable:
            self._remove_button = _RemoveButton(self)
            self._remove_button.setAutoRaise(True)
            self._remove_button.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation)
            self._remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._remove_button.setStyleSheet(
                "QToolButton { border: 0; border-radius: 7px; padding: 0; background: transparent; }"
                "QToolButton:hover { background: #20808080; }"
            )
            self._remove_button.clicked.connect(lambda: self.removed.emit(self._tag.id))
            icons.apply_icon(self._remove_button, "actions/dismiss")
            self._remove_button.setIconSize(QSize(11, 11))
        self.clicked.connect(lambda: self.clicked_tag.emit(self._tag.id))
        self.set_tag(tag)

    @property
    def tag(self) -> TagDefinition:
        """返回当前按钮绑定的标签定义。"""
        return self._tag

    def set_tag(self, tag: TagDefinition) -> None:
        """替换标签定义，同步名称、提示和无障碍描述后重新计算按钮尺寸。"""
        self._tag = tag
        self.setText(tag.name)
        self.setToolTip(tag.name)
        count_text = tr("，{count} 个 Mod", count=self._count) if self._count is not None else ""
        self.setAccessibleName(tr("标签 {name}{count_text}", name=tag.name, count_text=count_text))
        if self._remove_button is not None:
            self._remove_button.setAccessibleName(tr("从当前 Mod 移除 {name}", name=tag.name))
            self._remove_button.setToolTip(tr("<qt>从当前 Mod 移除 {value}</qt>", value=html.escape(tag.name)))
        self.updateGeometry()
        self.update()

    def _text_metrics(self) -> QFontMetricsF:
        """按实际字体和绘制设备保留小数宽度，使布局测量与省略判断保持一致。"""
        return QFontMetricsF(self.font(), self)

    def _count_width(self) -> int:
        """向上取整数量文字及前置间隔的宽度，避免分数像素被截断。"""
        return ceil(self._text_metrics().horizontalAdvance(f"  {self._count}")) if self._count is not None else 0

    def sizeHint(self) -> QSize:
        """根据标签名、数量和移除按钮计算推荐宽度，并限制在最大宽度内。"""
        self.ensurePolished()
        width = ceil(self._text_metrics().horizontalAdvance(self._tag.name)) + 16 + self._count_width()
        if self._remove_button is not None:
            width += 19
        return QSize(min(self.maximumWidth(), width), self._height)

    def minimumSizeHint(self) -> QSize:
        """保留数量和基本点击区域，同时允许长标签名在窄布局中省略显示。"""
        return QSize(min(42 + self._count_width(), self.sizeHint().width()), self._height)

    def paintEvent(self, event) -> None:
        """绘制主题化标签背景、状态边框、省略名称及可选数量，为移除按钮预留空间。"""
        background, foreground = tag_colors(self._tag.color, theme.current_theme() == "dark")
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        border = QColor(foreground)
        border.setAlpha(115 if self.underMouse() else 50)
        if self.isChecked() or self.hasFocus():
            border = QColor(theme.color("accent"))
        painter.setPen(QPen(border, 1))
        painter.setBrush(QColor(background))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self._height / 2, self._height / 2)
        painter.setFont(self.font())
        painter.setPen(QColor(foreground))
        remove_width = 19 if self._remove_button is not None else 0
        count_width = self._count_width()
        text_rect = self.rect().adjusted(8, 0, -8 - remove_width - count_width, 0)
        text = self._text_metrics().elidedText(self._tag.name, Qt.TextElideMode.ElideRight, max(0, text_rect.width()))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        if self._count is not None:
            count_rect = self.rect().adjusted(self.width() - 8 - remove_width - count_width, 0, -8 - remove_width, 0)
            painter.drawText(count_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, str(self._count))

    def resizeEvent(self, event) -> None:
        """随标签尺寸变化将独立移除按钮定位到右侧并垂直居中。"""
        super().resizeEvent(event)
        if self._remove_button is not None:
            self._remove_button.setGeometry(self.width() - 21, (self.height() - 18) // 2, 18, 18)

    def changeEvent(self, event) -> None:
        """在调色板或样式变化后重绘标签颜色和边框。"""
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            self.update()

    def event(self, event) -> bool:
        """显示经过 HTML 转义的完整标签提示，避免标签名称被解释为富文本。"""
        if event.type() == QEvent.Type.ToolTip:
            # 仅转义提示中的显示内容，保留原始名称与可访问文本。
            QToolTip.showText(event.globalPos(), f"<qt>{html.escape(self._tag.name)}</qt>", self)
            event.accept()
            return True
        return super().event(event)

    def mouseDoubleClickEvent(self, event) -> None:
        """消费标签双击事件，防止启用所属的 Mod 卡片。"""
        # 双击标签不能启用所属 Mod 卡片。
        event.accept()


class TagFlowLayout(QLayout):
    """自动换行，可按行居中，并拉伸末尾输入框。"""

    def __init__(self, parent=None, margin=0, spacing=6):
        """建立布局项列表，并设置各行共用的边距和间距。"""
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def addItem(self, item: QLayoutItem) -> None:
        """按顺序接管布局项并使当前几何计算失效。"""
        self._items.append(item)
        self.invalidate()

    def count(self) -> int:
        """返回布局项数量；Python 状态已清理时向 Qt 报告空布局。"""
        # Python 状态清理后 Qt 仍可能查询布局，项目列表已释放时报告为空。
        return len(getattr(self, "_items", ()))

    def itemAt(self, index: int):
        """返回指定位置的布局项，索引越界或布局已清理时返回 None。"""
        items = getattr(self, "_items", ())
        return items[index] if 0 <= index < len(items) else None

    def takeAt(self, index: int):
        """移出并返回指定布局项但不销毁它，调用方接管后续处理。"""
        items = getattr(self, "_items", ())
        if 0 <= index < len(items):
            item = items.pop(index)
            self.invalidate()
            return item
        return None

    def expandingDirections(self):
        """声明布局自身不主动请求额外的水平或垂直空间。"""
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        """告知 Qt 布局高度取决于给定宽度下的标签换行结果。"""
        return True

    def heightForWidth(self, width: int) -> int:
        """仅测量指定宽度下的换行高度，不修改子项几何。"""
        return self._layout(QRect(0, 0, width, 0), measure=True)

    def setGeometry(self, rect: QRect) -> None:
        """接受布局区域并把换行计算结果应用到各个子项。"""
        super().setGeometry(rect)
        self._layout(rect, measure=False)

    def sizeHint(self) -> QSize:
        """以能够容纳单个最大子项及边距的尺寸作为布局建议。"""
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        """合并各子项的最小尺寸并加上布局边距。"""
        size = QSize()
        for item in getattr(self, "_items", ()):
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _layout(self, rect: QRect, *, measure: bool) -> int:
        """按可用宽度排列非空子项，支持行居中和末项水平拉伸，并返回含边距的总高度。"""
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, row_height = area.x(), area.y(), 0
        centered = bool(self.alignment() & Qt.AlignmentFlag.AlignHCenter)
        row = []
        items = [item for item in getattr(self, "_items", ()) if not item.isEmpty()]
        for index, item in enumerate(items):
            hint = item.sizeHint()
            width = min(hint.width(), max(1, area.width()))
            if x > area.x() and x + width > area.right() + 1:
                self._center_row(row, area)
                row.clear()
                x, y, row_height = area.x(), y + row_height + self.spacing(), 0
            if index == len(items) - 1 and item.expandingDirections() & Qt.Orientation.Horizontal:
                width = max(width, area.right() + 1 - x)
            if not measure:
                geometry = QRect(QPoint(x, y), QSize(width, hint.height()))
                if centered:
                    row.append((item, geometry))
                else:
                    item.setGeometry(geometry)
            x += width + self.spacing()
            row_height = max(row_height, hint.height())
        self._center_row(row, area)
        return y + row_height - rect.y() + margins.bottom()

    @staticmethod
    def _center_row(row: list[tuple[QLayoutItem, QRect]], area: QRect) -> None:
        """将一行已测量的子项整体水平居中后应用其几何。"""
        if not row:
            return
        offset = max(0, (area.right() - row[-1][1].right()) // 2)
        for item, geometry in row:
            item.setGeometry(geometry.translated(offset, 0))
