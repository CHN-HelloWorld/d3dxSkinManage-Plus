"""快捷键界面的键帽、完整组合键按钮和可撤销轻提示。"""

from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from src.ui import anim, icons, theme
from src.utils.i18n import t as tr

color = theme.shortcut_color


def _key_geometry(parts: list[str], scale: str = "normal", width: int = 10000):
    """根据字体、缩放规格和可用宽度计算键帽及加号的位置，返回布局尺寸和绘制字体。"""
    size, height, minimum, padding = {"small": (11, 22, 24, 6), "normal": (12, 26, 27, 7), "large": (16, 37, 38, 10)}[
        scale
    ]
    font = QFont()
    font.setPixelSize(size)
    metrics = QFontMetricsF(font)
    plus_font = QFont()
    plus_font.setPixelSize(15 if scale == "large" else 11)
    plus_width = math.ceil(QFontMetricsF(plus_font).horizontalAdvance("+"))
    items = []
    x = y = widest = 0
    for index, part in enumerate(parts):
        key_width = max(minimum, math.ceil(metrics.horizontalAdvance(part)) + padding * 2 + 2)
        leading = plus_width + 10 if index else 0
        if x and x + leading + key_width > width:
            widest = max(widest, x)
            x = 0
            y += height + 6
        if index:
            if x:
                x += 5
            items.append(("+", QRectF(x, y, plus_width, height), True))
            x += plus_width + 5
        items.append((part, QRectF(x, y, key_width, height), False))
        x += key_width
    return items, QSize(max(widest, x), y + height), font, plus_font


def draw_keys(painter: QPainter, parts: list[str], rect: QRectF, scale: str = "normal", *, centered=False):
    """在指定区域内绘制可换行的键帽组合，支持居中布局并保存、恢复调用方画笔状态。"""
    items, size, font, plus_font = _key_geometry(parts, scale, int(rect.width()))
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(
        rect.left() + (max(0, (rect.width() - size.width()) / 2) if centered else 0),
        rect.top() + max(0, (rect.height() - size.height()) / 2),
    )
    radius = 5 if scale == "large" else 4
    for text, box, is_plus in items:
        if is_plus:
            painter.setFont(plus_font)
            painter.setPen(QColor(color("text.tertiary")))
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color("key.shadow")))
            painter.drawRoundedRect(box.translated(0, 1), radius, radius)
            painter.setBrush(QColor(color("stroke.control")))
            painter.drawRoundedRect(box, radius, radius)
            painter.setBrush(QColor(color("bg.layer")))
            painter.drawRoundedRect(box.adjusted(1, 1, -1, -2), radius - 1, radius - 1)
            painter.setFont(font)
            painter.setPen(QColor(color("text.primary")))
        painter.drawText(box.adjusted(0, 0, 0, -1), Qt.AlignmentFlag.AlignCenter, text)
    painter.restore()


class KeyCaps(QWidget):
    """将快捷键绑定绘制为支持按宽度换行的键帽组合。"""

    def __init__(self, binding: str | None = None, parent=None, *, scale="normal", centered=False):
        """设置键帽规格、对齐方式和高度随宽度变化的尺寸策略，并载入初始绑定。"""
        super().__init__(parent)
        self.scale = scale
        self.centered = centered
        self._parts: list[str] = []
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.set_binding(binding)

    def set_binding(self, binding: str | None):
        """按加号拆分绑定为键帽内容，并同步辅助技术可读的绑定名称。"""
        self.set_parts(binding.split("+") if binding else [])
        self.setAccessibleName(binding.replace("+", " + ") if binding else tr("未绑定"))

    def set_parts(self, parts: list[str]):
        """复制键帽文字列表并使布局尺寸和绘制内容失效。"""
        self._parts = list(parts)
        self.updateGeometry()
        self.update()

    def sizeHint(self):
        """返回未换行键帽组合的建议尺寸，未绑定时返回文字占位尺寸。"""
        if not self._parts:
            return QSize(48, 22)
        return _key_geometry(self._parts, self.scale)[1]

    def minimumSizeHint(self):
        """保留建议高度并允许父布局将键帽控件横向压缩到零。"""
        return QSize(0, self.sizeHint().height())

    def heightForWidth(self, width):
        """返回在指定可用宽度下排列全部键帽所需的高度。"""
        return _key_geometry(self._parts, self.scale, width)[1].height()

    def paintEvent(self, event):
        """绘制当前键帽组合，空绑定时显示弱化的未绑定提示。"""
        painter = QPainter(self)
        if self._parts:
            draw_keys(painter, self._parts, QRectF(self.rect()), self.scale, centered=self.centered)
        else:
            painter.setPen(QColor(color("text.tertiary")))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignVCenter, tr("未绑定"))


class BindingButton(QAbstractButton):
    """整组键帽是一个可聚焦按钮；悬停/聚焦时才显示编辑图标。"""

    focus_changed = Signal()

    def __init__(self, title: str, binding: str | None, parent=None):
        """创建支持键盘焦点的绑定编辑按钮，设置列宽并加载当前快捷键。"""
        super().__init__(parent)
        self.title = title
        self.binding = binding
        self._column_width = 204
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(tr("点击修改快捷键"))
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.set_binding(binding)

    def changeEvent(self, event):
        """字体变化后重新计算键帽换行高度并通知父布局。"""
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(self, "binding"):
            self.setFixedHeight(self.heightForWidth(self.width()))
            self.updateGeometry()

    def set_binding(self, binding: str | None):
        """更新绑定和无障碍说明，根据是否已绑定选择按钮宽度并重新计算所需高度。"""
        self.binding = binding
        value = binding.replace("+", " + ") if binding else tr("未绑定")
        self.setAccessibleName(tr("修改{title}的快捷键，当前{value}", title=self.title, value=value))
        self.setFixedWidth(self._column_width if binding else 130)
        self.setFixedHeight(self.heightForWidth(self.width()))
        self.updateGeometry()
        self.update()

    def set_column_width(self, width: int):
        """更新已绑定按钮的统一列宽，并重新应用当前绑定以刷新尺寸。"""
        if width != self._column_width:
            self._column_width = width
            self.set_binding(self.binding)
