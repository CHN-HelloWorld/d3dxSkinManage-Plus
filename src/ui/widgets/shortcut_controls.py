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


def _binding_parts(binding: str | None) -> list[str]:
    """将保存的侧键名称转成易读键帽文字，键盘名称保持不变。"""
    labels = {"XButton1": tr("侧键1"), "XButton2": tr("侧键2")}
    return [labels.get(part, part) for part in binding.split("+")] if binding else []


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
        parts = _binding_parts(binding)
        self.set_parts(parts)
        self.setAccessibleName(" + ".join(parts) if binding else tr("未绑定"))

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
        value = " + ".join(_binding_parts(binding)) if binding else tr("未绑定")
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

    def heightForWidth(self, width):
        """返回容纳键帽换行和内边距的按钮高度，未绑定时使用固定最小高度。"""
        if not self.binding:
            return 34
        return max(34, _key_geometry(_binding_parts(self.binding), width=width - 32)[1].height() + 6)

    def sizeHint(self):
        """返回当前按钮宽度及该宽度下所需的键帽高度。"""
        return QSize(self.width(), self.heightForWidth(self.width()))

    def minimumSizeHint(self):
        """以当前建议尺寸作为绑定按钮的最小布局尺寸。"""
        return self.sizeHint()

    def paintEvent(self, event):
        """绘制绑定键帽或设置入口，并根据悬停、按下和键盘焦点呈现编辑图标及边框。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        hovered = self.underMouse() or self.hasFocus()
        if hovered or not self.binding:
            painter.setBrush(
                QColor(color("bg.pressed" if self.isDown() else "bg.card")) if hovered else Qt.BrushStyle.NoBrush
            )
            pen = QPen(QColor(color("accent" if self.hasFocus() else "stroke.control")), 1)
            if not self.binding:
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRoundedRect(box, 4, 4)
        if self.binding:
            draw_keys(painter, _binding_parts(self.binding), box.adjusted(6, 3, -26, -3))
            if hovered:
                icons.get_icon("actions/edit", "text.tertiary").paint(
                    painter, self.width() - 19, (self.height() - 13) // 2, 13, 13
                )
        else:
            icons.get_icon("actions/add", "text.tertiary").paint(painter, 10, (self.height() - 16) // 2, 16, 16)
            font = QFont(self.font())
            font.setPixelSize(12)
            painter.setFont(font)
            painter.setPen(QColor(color("accent" if hovered else "text.tertiary")))
            painter.drawText(box.adjusted(33, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter, tr("点击设置"))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(color("accent")), 2))
            painter.drawRoundedRect(box.adjusted(1, 1, -1, -1), 4, 4)

    def enterEvent(self, event):
        """鼠标进入时请求重绘，使编辑提示和悬停背景生效。"""
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        """鼠标离开时请求重绘，移除悬停编辑提示。"""
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event):
        """获得焦点时发出焦点变化信号，供外部保持当前编辑行可见。"""
        self.focus_changed.emit()
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        """失去焦点时发出焦点变化信号并保留默认焦点处理。"""
        self.focus_changed.emit()
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        """无修饰键的回车触发绑定编辑按钮，其余按键交给默认处理。"""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers():
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)


class ShortcutButton(QPushButton):
    """页面按钮与 HTML 一样支持 Enter，Space 保留 Qt 原生行为。"""

    def keyPressEvent(self, event):
        """无修饰键的回车触发一次点击，并忽略按住回车产生的自动重复。"""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.modifiers():
            if not event.isAutoRepeat():
                self.click()
            event.accept()
            return
        super().keyPressEvent(event)


def button_style() -> str:
    """返回快捷键设置区域中普通、幽灵和主操作按钮的主题样式。"""
    return (
        f"QPushButton {{ background: {color('bg.card')}; color: {color('text.primary')};"
        f" border: 1px solid {color('stroke.control')}; border-radius: 4px;"
        " font-size: 13px; padding: 5px 12px; min-height: 20px; }"
        f"QPushButton:hover {{ background: {color('bg.hover')}; }}"
        f"QPushButton:pressed {{ background: {color('bg.pressed')}; }}"
        f"QPushButton:focus {{ border: 2px solid {color('accent')}; padding: 4px 11px; }}"
        f"QPushButton[ghost='true'] {{ background: transparent; border-color: transparent; }}"
        f"QPushButton[ghost='true']:hover {{ background: {color('bg.hover')}; }}"
        f"QPushButton[ghost='true']:focus {{ border-color: {color('accent')}; }}"
        f"QPushButton[primary='true'] {{ background: {color('accent')}; border-color: {color('accent')};"
        f" color: {color('text.on_accent')}; font-weight: 600; }}"
        f"QPushButton[primary='true']:hover {{ background: {color('accent.hover')}; }}"
        f"QPushButton:disabled {{ color: {color('text.disabled')}; }}"
        f"QPushButton[primary='true']:disabled {{ background: {theme.rgba('accent', 0.43)};"
        f" border-color: transparent; color: {theme.rgba('text.on_accent', 0.7)}; }}"
        f"QToolButton {{ background: transparent; color: {color('text.secondary')}; border: none;"
        " padding: 0; border-radius: 4px; }"
        f"QToolButton:hover {{ background: {color('bg.hover')}; }}"
        f"QToolButton:focus {{ border: 2px solid {color('accent')}; }}"
    )


class ShortcutToast(QFrame):
    """展示快捷键变更结果，并提供可选撤销操作的短时通知。"""

    undo_requested = Signal()

    def __init__(self, parent):
        """创建状态图标、文字、撤销和关闭按钮，配置阴影及自动隐藏定时器。"""
        super().__init__(parent)
        self.setObjectName("shortcutToast")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(15, 11, 12, 11)
        layout.setSpacing(10)
        self._icon = QLabel()
        self._icon.setFixedSize(16, 16)
        layout.addWidget(self._icon)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message, 1)
        self.undo_button = ShortcutButton(tr("撤销"))
        self.undo_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.undo_button.clicked.connect(self.undo_requested.emit)
        layout.addWidget(self.undo_button)
        close = QToolButton()
        close.setFixedSize(24, 24)
        close.setAccessibleName(tr("关闭提示"))
        icons.apply_icon(close, "actions/dismiss", "text.secondary")
        close.clicked.connect(self.hide)
        layout.addWidget(close)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 34))
        self.setGraphicsEffect(shadow)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, message: str, *, undo: bool = False, error: bool = False):
        """显示成功或错误提示和可选撤销按钮，按内容定位后执行轻微入场位移并定时隐藏。"""
        self.setStyleSheet(
            button_style()
            + f"#shortcutToast {{ background: {color('bg.card')}; border: 1px solid {color('stroke.control')};"
            " border-radius: 8px; }"
            "#shortcutToast QLabel { background: transparent; border: none; font-size: 12px;"
            f" color: {color('text.primary')}; }}"
            f"#shortcutToast QPushButton {{ background: transparent; border: none; color: {color('accent')};"
            " font-weight: 600; padding: 3px 8px; min-height: 0; }"
        )
        icons.apply_pixmap(
            self._icon, "status/warning" if error else "meta/check", 16, "danger" if error else "success"
        )
        self.message.setText(message)
        self.undo_button.setVisible(undo)
        self.ensurePolished()
        self.reposition()
        self.show()
        self.raise_()
        end = self.pos()
        self.move(end + QPoint(0, 8))
        anim.animate_move(self, end, duration=150)
        self._timer.start(6500)

    def reposition(self):
        # 先按单行内容取得自然宽度，只有超出窗口可用宽度时才折行。
        """按父控件宽度限制文字和通知尺寸，将其居中放置在底部操作区上方。"""
        self.message.setWordWrap(False)
        maximum = max(100, self.parentWidget().width() - 40)
        self.setMinimumWidth(0)
        self.setMaximumWidth(maximum)
        self.layout().activate()
        width = min(maximum, self.sizeHint().width())
        self.message.setWordWrap(True)
        self.setFixedWidth(width)
        height = max(self.layout().minimumSize().height(), self.layout().totalHeightForWidth(width))
        self.resize(width, height)
        self.move(
            max(20, (self.parentWidget().width() - self.width()) // 2),
            max(0, self.parentWidget().height() - self.height() - 72),
        )
