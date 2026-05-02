"""原生预设面板：明确的应用与管理操作，保留 Windows 多选和内联改名。"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QItemSelectionModel, QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath, QRegion
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyleOption,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.ui import anim, icons, theme
from src.ui.win_effects import acrylic_surface_alpha
from src.utils.i18n import t as tr


class _PresetNameLabel(QLabel):
    """省略显示长名称，避免挤出行操作按钮。"""

    def __init__(self, text: str = "", parent=None) -> None:
        """建立允许缩小的纯文本名称标签，保留完整预设名称。"""
        super().__init__(parent)
        self._full_text = text
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self.setText(text)

    def setText(self, text: str) -> None:
        """保存完整名称作为提示，并更新按宽度省略的显示文本。"""
        self._full_text = text
        self.setToolTip(text)
        self._elide()

    def _elide(self) -> None:
        """依据当前内容宽度省略名称尾部，为同一行的操作按钮保留空间。"""
        super().setText(
            self.fontMetrics().elidedText(
                self._full_text, Qt.TextElideMode.ElideRight, max(0, self.contentsRect().width())
            )
        )

    def resizeEvent(self, event) -> None:
        """名称区域变化后重新计算省略结果。"""
        super().resizeEvent(event)
        self._elide()


class PresetItemWidget(QWidget):
    """展示单个预设的名称、当前状态及应用入口，支持通过行内输入框请求重命名。"""

    apply_clicked = Signal(str)  # preset_id
    rename_committed = Signal(str, str)  # preset_id, new_name
    rename_cancelled = Signal(str)  # preset_id
    request_select = Signal(object, object)  # widget, Qt.KeyboardModifiers
    request_rename = Signal(object)  # widget

    def __init__(
        self,
        preset_id: str,
        name: str,
        is_default: bool,
        is_current: bool,
        parent: QWidget | None = None,
        *,
        mod_count: int | None = None,
    ) -> None:
        """保存预设身份、默认和当前状态，建立显示与重命名共用的行控件。"""
        super().__init__(parent)
        self.setObjectName("presetRow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("presetCurrent", is_current)
        self.setProperty("presetSelected", False)
        self._preset_id = preset_id
        self._is_default = is_default
        self._is_current = is_current
        self._original_name = name
        self._rename_active = False
        self._mod_count = mod_count
        self._setup_ui(name)

    @property
    def preset_id(self) -> str:
        """返回该行绑定的预设标识。"""
        return self._preset_id

    @property
    def is_default(self) -> bool:
        """返回该预设是否为需要保留的默认预设。"""
        return self._is_default

    def _setup_ui(self, name: str) -> None:
        """组合名称显示编辑区域、Mod 数量说明和应用按钮，当前预设的应用入口保持禁用。"""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(12)

        # 显示/编辑 通过 QStackedWidget 切换
        self._stack = QStackedWidget()
        self._stack.setObjectName("presetNameStack")
        self._stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._stack.setMinimumWidth(0)
        self._stack.setFixedHeight(25)

        self._name_label = _PresetNameLabel(name)
        self._name_label.setObjectName("presetName")
        # 标签忽略鼠标事件，由所属预设项处理点击。
        self._name_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._stack.addWidget(self._name_label)

        self._name_edit = QLineEdit()
        self._name_edit.setObjectName("presetNameEdit")
        self._name_edit.setAccessibleName(tr("预设名称"))
        self._name_edit.setMinimumWidth(0)
        self._name_edit.installEventFilter(self)
        self._name_edit.returnPressed.connect(self._commit_rename)
        self._stack.addWidget(self._name_edit)
        self._stack.setCurrentWidget(self._name_label)

        column = QVBoxLayout()
        column.setSpacing(0)
        column.addWidget(self._stack)
        detail = tr("{mod_count} 个 Mod", mod_count=self._mod_count) if self._mod_count is not None else tr("Mod 组合")
        if self._is_default:
            detail = tr("默认预设 · ") + detail
        self._detail_label = QLabel(detail)
        self._detail_label.setObjectName("presetMeta")
        self._detail_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        column.addWidget(self._detail_label)
        layout.addLayout(column, 1)

        self._apply_btn = QPushButton(tr("使用中") if self._is_current else tr("应用"))
        self._apply_btn.setObjectName("presetApply")
        self._apply_btn.setProperty("presetCurrent", self._is_current)
        self._apply_btn.setFixedSize(84, 30)
        self._apply_btn.setAutoDefault(False)
        self._apply_btn.setAccessibleName(
            tr("当前使用：{name}", name=name) if self._is_current else tr("应用预设：{name}", name=name)
        )
        self._apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._apply_btn.clicked.connect(self._on_apply_clicked)
        # 按钮不参与父级 mousePress 转发
        if self._is_current:
            icons.apply_icon(self._apply_btn, "meta/check", "accent")
            self._apply_btn.setIconSize(QSize(14, 14))
            self._apply_btn.setEnabled(False)
            self._apply_btn.setToolTip(tr("当前使用的预设"))
        else:
            self._apply_btn.setToolTip(tr("切换到“{name}”", name=name))
        layout.addWidget(self._apply_btn)
