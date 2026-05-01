"""显示单个 Mod 的变体并发送命令，不访问文件。"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui import acrylic, icons, theme
from src.ui.widgets.ring_progress import RingProgressButton
from src.utils.i18n import t as tr


class _NameLabel(QLabel):
    """在空间受限时省略变体名称，并保留完整文字提示的标签。"""

    def __init__(self, parent=None):
        """初始化完整名称缓存，允许标签横向压缩并强制按纯文本显示。"""
        super().__init__(parent)
        self._full_text = ""
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setTextFormat(Qt.TextFormat.PlainText)

    def setText(self, text):
        """保存完整名称为提示文字，并按当前宽度更新省略显示。"""
        self._full_text = str(text)
        self.setToolTip(self._full_text)
        self._elide()

    def _elide(self):
        """按当前字体与内容区宽度对完整变体名称执行右侧省略。"""
        super().setText(
            QFontMetrics(self.font()).elidedText(
                self._full_text, Qt.TextElideMode.ElideRight, max(0, self.contentsRect().width())
            )
        )

    def resizeEvent(self, event):
        """标签尺寸变化后重新计算名称的省略显示。"""
        super().resizeEvent(event)
        self._elide()


class _VariantRow(QFrame):
    """展示一个变体的选择、当前使用状态和更多操作，并提供切换进度反馈。"""

    selected = Signal(object)
    menu_requested = Signal(object, QPoint)

    def __init__(self, variant_id, parent=None):
        """绑定变体标识，创建选择内容、单选标记、切换进度环及更多操作按钮。"""
        super().__init__(parent)
        self.variant_id = variant_id
        self.setObjectName("modVariantRow")
        self.setProperty("variantId", variant_id)
        self.setMinimumWidth(0)
        self.setFixedHeight(44)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(2)
        self.select_button = QPushButton(self)
        self.select_button.setObjectName("variantSelect")
        self.select_button.setMinimumWidth(0)
        self.select_button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.select_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_button.clicked.connect(lambda: self.selected.emit(self.variant_id))
        self.select_button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.select_button.customContextMenuRequested.connect(
            lambda point: self.menu_requested.emit(self.variant_id, self.select_button.mapToGlobal(point))
        )
        content = QHBoxLayout(self.select_button)
        content.setContentsMargins(9, 6, 7, 6)
        content.setSpacing(9)
        self.radio = QFrame()
        self.radio.setFixedSize(15, 15)
        self.radio.setObjectName("variantRadio")
        self.dot = QFrame(self.radio)
        self.dot.setObjectName("variantRadioDot")
        self.dot.setGeometry(4, 4, 7, 7)
        content.addWidget(self.radio)
        self.progress_ring = RingProgressButton(self.select_button, size=27, line_width=2)
        self.progress_ring.setObjectName("variantSwitchProgress")
        self.progress_ring.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.progress_ring.setEnabled(False)
        self.progress_ring.hide()
        self._switching = False
        self.radio.installEventFilter(self)
        self.name_label = _NameLabel()
        self.name_label.setObjectName("variantName")
        content.addWidget(self.name_label, 1)
        self.using_label = QLabel()
        self.using_label.setObjectName("variantInUse")
        content.addWidget(self.using_label)
        for label in (self.radio, self.dot, self.name_label, self.using_label):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.select_button, 1)
        self.more_button = QPushButton()
        self.more_button.setObjectName("variantMore")
        self.more_button.setFixedSize(28, 28)
        self.more_button.setIconSize(QSize(15, 15))
        self.more_button.setCursor(Qt.CursorShape.PointingHandCursor)
        icons.apply_icon(self.more_button, "actions/more", "text.secondary")
        self.more_button.clicked.connect(
            lambda: self.menu_requested.emit(
                self.variant_id, self.more_button.mapToGlobal(QPoint(0, self.more_button.height()))
            )
        )
        layout.addWidget(self.more_button)

    def set_details(self, name, *, selected, in_use, enabled):
        """同步名称、选择状态和实际使用状态，区分当前使用与下次使用并刷新主题样式。"""
        self.name_label.setText(name)
        self.select_button.setAccessibleName(tr("切换到 {name}", name=name))
        self.more_button.setToolTip(tr("{name}的更多操作", name=name))
        self.more_button.setAccessibleName(tr("{name}的更多操作", name=name))
        self.setProperty("selected", bool(selected))
        self.setProperty("inUse", bool(in_use))
        self.using_label.setText((tr("正在使用") if enabled else tr("下次使用")) if in_use else "")
        self.using_label.setVisible(bool(in_use))
        self.dot.setVisible(bool(selected))
        self.retheme()

    def contextMenuEvent(self, event):
        """将行级右键菜单请求连同变体标识和全局坐标转发给面板。"""
        self.menu_requested.emit(self.variant_id, event.globalPos())
        event.accept()

    def _position_progress_ring(self):
        """将切换进度环中心对齐到单选标记中心。"""
        self.progress_ring.move(self.radio.geometry().center() - self.progress_ring.rect().center())
