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

    def set_selected(self, selected: bool) -> None:
        """仅在选择状态变化时更新动态属性并重新应用行样式。"""
        if self.property("presetSelected") == selected:
            return
        self.setProperty("presetSelected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def _on_apply_clicked(self) -> None:
        """发送绑定预设的应用请求，实际部署由使用方执行。"""
        self.apply_clicked.emit(self._preset_id)

    def mousePressEvent(self, event) -> None:
        """非编辑状态下转发左键选择及修饰键，消费事件以防列表重复切换多选状态。"""
        if event.button() == Qt.MouseButton.LeftButton and not self._rename_active:
            self.request_select.emit(self, event.modifiers())
            # 消费事件，防止 QListWidget 的 ExtendedSelection 再次切换 Ctrl 选中状态。
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        """非编辑状态下将左键双击转换为该行的重命名请求。"""
        if event.button() == Qt.MouseButton.LeftButton and not self._rename_active:
            self.request_rename.emit(self)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def enter_rename_mode(self) -> None:
        """进入重命名模式并全选名称。"""
        self._rename_active = True
        self._name_edit.setText(self._original_name)
        self._stack.setCurrentWidget(self._name_edit)
        self._name_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self._name_edit.selectAll()

    def _exit_rename_mode(self, restore: bool = False) -> None:
        """退出重命名模式"""
        self._rename_active = False
        new_name = self._original_name if restore else self._name_edit.text().strip()
        if not new_name:
            new_name = self._original_name
        self._original_name = new_name
        self._name_label.setText(new_name)
        self._stack.setCurrentWidget(self._name_label)

    def _commit_rename(self) -> None:
        """提交去除首尾空白的新名称；空名称或未变化的名称按取消处理。"""
        if not self._rename_active:
            return
        new_name = self._name_edit.text().strip()
        original = self._original_name
        if not new_name or new_name == original:
            self._exit_rename_mode(restore=True)
            self.rename_cancelled.emit(self._preset_id)
            return
        self._exit_rename_mode(restore=False)
        self.rename_committed.emit(self._preset_id, new_name)

    def _cancel_rename(self) -> None:
        """恢复原名称并退出编辑态，向外层报告重命名取消。"""
        if not self._rename_active:
            return
        self._exit_rename_mode(restore=True)
        self.rename_cancelled.emit(self._preset_id)

    def eventFilter(self, obj, event) -> bool:
        """在名称编辑中用 Esc 取消，失去焦点时按回车相同规则提交。"""
        if obj is self._name_edit:
            if event.type() == QEvent.Type.KeyPress:
                if event.key() == Qt.Key.Key_Escape and self._rename_active:
                    self._cancel_rename()
                    return True
            elif event.type() == QEvent.Type.FocusOut and self._rename_active:
                # 失焦提交（与 Enter 行为一致）
                self._commit_rename()
                return False
        return super().eventFilter(obj, event)


class PresetPopup(QFrame):
    """下拉浮层：当前状态、预设列表与显式管理工具栏。"""

    apply_requested = Signal(str)
    rename_requested = Signal(str, str)  # preset_id, new_name
    create_requested = Signal()
    delete_requested = Signal(list)  # 选中的预设标识。
    duplicate_requested = Signal(list)  # preset_ids

    def __init__(self, parent: QWidget | None = None) -> None:
        """创建支持多选的预设浮层，初始化局部复制记录并在首次显示前设置透明材质。"""
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("presetPopup")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._presets: list = []
        self._current_id: str = ""
        self._clipboard_ids: list[str] = []
        self._anchor_row: int = -1  # Shift 范围选择的锚点
        self._setup_ui()
        # 首次显示前设置 WA_TranslucentBackground，避免在父窗口显示过程中重建原生窗口，
        # 引发事件重入。acrylic.py 的应用级过滤器会跳过 PresetPopup。
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.apply_acrylic()

    def paintEvent(self, event) -> None:
        """在透明窗口上绘制一次圆角底色，避免面板空白区域完全透出下层内容。"""
        option = QStyleOption()
        option.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_Widget, option, painter, self)
        painter.end()

    def apply_acrylic(self) -> None:
        """按当前主题设置半透明填充，显示顶层窗口的亚克力模糊背景。

        在初始化和每次弹出前调用，配合 showEvent 的 apply_acrylic_blurbehind。
        列表及其 viewport 均需透明，避免 QAbstractScrollArea 的视口自动填充遮挡背景。
        """
        fill = theme.rgba("bg.card", acrylic_surface_alpha(0.72))
        self._theme_name = theme.current_theme()
        self.setStyleSheet(f"""
            QFrame#presetPopup {{ background: {fill}; border: 1px solid {theme.color("stroke.card")};
                           border-radius: 12px; }}
            PresetPopup QLabel {{ background: transparent; border: 0; padding: 0;
                                  color: {theme.color("text.primary")}; }}
            QLabel#presetTitle {{ font-size: 17px; font-weight: 600; }}
            QLabel#presetCount {{ color: {theme.color("text.secondary")}; font-size: 12px;
                                  background: {theme.rgba("bg.subtle", 0.8)};
                                  border-radius: 8px; padding: 2px 6px; }}
            QLabel#presetHint, QLabel#presetSelection, QLabel#presetMeta {{
                color: {theme.color("text.secondary")}; font-size: 12px; }}
            QLabel#presetName {{ font-size: 14px; font-weight: 600; }}
            PresetPopup QPushButton {{ min-width: 0; min-height: 20px; padding: 4px 9px;
                border: 1px solid {theme.color("stroke.control")}; border-radius: 5px;
                background: {theme.rgba("control.bg", 0.7)}; color: {theme.color("text.primary")}; }}
            PresetPopup QPushButton:hover {{ background: {theme.color("bg.hover")}; }}
            PresetPopup QPushButton:pressed {{ background: {theme.color("bg.pressed")}; }}
            PresetPopup QPushButton:disabled {{ color: {theme.color("text.disabled")};
                border-color: {theme.color("stroke.card")}; background: transparent; }}
            QPushButton#presetCreate {{ background: {theme.color("accent")};
                border-color: {theme.color("accent")}; color: {theme.color("text.on_accent")}; }}
            QPushButton#presetCreate:hover {{ background: {theme.color("accent.hover")}; }}
            QPushButton#presetDelete:hover {{ color: {theme.color("danger")};
                background: {theme.rgba("danger", 0.09)}; border-color: {theme.rgba("danger", 0.25)}; }}
            QWidget#presetRow {{ background: transparent; border: 1px solid transparent;
                                  border-radius: 7px; }}
            QWidget#presetRow:hover {{ background: {theme.rgba("bg.hover", 0.75)}; }}
            QWidget#presetRow[presetCurrent="true"] {{ background: {theme.rgba("accent", 0.07)}; }}
            QWidget#presetRow[presetSelected="true"] {{ background: {theme.color("accent.subtle")};
                border-color: {theme.rgba("accent", 0.4)}; }}
            QStackedWidget#presetNameStack {{ background: transparent; border: 0; }}
            QPushButton#presetApply {{ min-width: 82px; max-width: 82px; min-height: 28px;
                                      max-height: 28px; padding: 0; }}
            QPushButton#presetApply[presetCurrent="true"]:disabled {{
                color: {theme.color("accent")}; border: 1px solid transparent; background: transparent; }}
            QLineEdit#presetNameEdit {{ min-height: 0; min-width: 0; padding: 1px 4px;
                border: 1px solid {theme.color("accent")}; border-radius: 4px; }}
            QFrame#presetFooter {{ background: transparent; border: 0;
                border-top: 1px solid {theme.color("stroke.divider")}; }}
        """)
        # 清零 ::item 内边距，避免全局 padding:4px 6px 压缩行控件的可用高度，
        # 裁切固定 24px 高的应用按钮。PresetItemWidget 已提供上下各 4px 的内边距。
        # 透明背景与 item 规则一并设置，确保多次调用时规则完整。
        self._list.setStyleSheet(
            "QListWidget { background: transparent; border: none; }"
            "QListWidget::item { padding: 0px; margin: 0px; border: none; }"
            "QListWidget::item:selected { background: transparent; }"
        )
        vp = self._list.viewport()
        vp.setObjectName("presetViewport")
        vp.setAutoFillBackground(False)
        vp.setStyleSheet("QWidget#presetViewport { background: transparent; }")
        icons.retheme_icons(self)

    def _apply_round_mask(self) -> None:
        """使用窗口区域遮罩裁出圆角，同时裁切亚克力模糊背景。

        QSS border-radius 仅裁切绘制背景，DWM 圆角在透明分层窗口上可能无效。
        区域遮罩可同时约束窗口和模糊层的形状，其边缘采用离散像素裁切。
        """
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), 12, 12)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def showEvent(self, event) -> None:
        """原生窗口显示后更新圆角裁剪区域。"""
        super().showEvent(event)
        self._apply_round_mask()

    def resizeEvent(self, event) -> None:
        """浮层尺寸变化后同步圆角裁剪区域。"""
        super().resizeEvent(event)
        self._apply_round_mask()

    def _setup_ui(self) -> None:
        """建立预设列表、数量提示及新建、重命名、复制、删除工具栏。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(9)

        heading = QHBoxLayout()
        heading.setSpacing(7)
        title = QLabel(tr("预设"))
        title.setObjectName("presetTitle")
        heading.addWidget(title)
        self._count_label = QLabel("0")
        self._count_label.setObjectName("presetCount")
        heading.addWidget(self._count_label)
        heading.addStretch()
        self._add_btn = self._management_button(
            tr("新建预设"), "presetCreate", "actions/add", self.create_requested.emit
        )
        icons.apply_icon(self._add_btn, "actions/add", "text.on_accent")
        self._add_btn.setToolTip(tr("新建一个空预设；如需沿用现有搭配，请选中后复制"))
        heading.addWidget(self._add_btn)
        layout.addLayout(heading)
        hint = QLabel(tr("一键切换整套 Mod 搭配"))
        hint.setObjectName("presetHint")
        layout.addWidget(hint)

        self._list = QListWidget()
        self._list.setAccessibleName(tr("预设列表，单击选择，双击重命名"))
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setSpacing(2)
        self._list.setMinimumHeight(0)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        layout.addWidget(self._list, 1)

        footer = QFrame()
        footer.setObjectName("presetFooter")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(0, 10, 0, 0)
        footer_layout.setSpacing(7)
        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self._rename_btn = self._management_button(
            tr("重命名"), "presetRename", "actions/edit", self._on_rename_selected
        )
        self._rename_btn.setToolTip(tr("重命名选中的预设，也可双击名称"))
        bottom.addWidget(self._rename_btn)
        self._duplicate_btn = self._management_button(tr("复制"), "presetDuplicate", "actions/copy", self._on_duplicate)
        self._duplicate_btn.setToolTip(tr("为选中预设创建副本，保留原来的 Mod 搭配"))
        bottom.addWidget(self._duplicate_btn)
        bottom.addStretch()
        self._del_btn = self._management_button(tr("删除"), "presetDelete", "actions/remove", self._on_delete)
        bottom.addWidget(self._del_btn)
        footer_layout.addLayout(bottom)
        self._selection_label = QLabel()
        self._selection_label.setObjectName("presetSelection")
        footer_layout.addWidget(self._selection_label)
        layout.addWidget(footer)
        self._list.itemSelectionChanged.connect(self._update_actions)
        self._update_actions()

    @staticmethod
    def _management_button(text: str, name: str, icon: str, callback) -> QPushButton:
        """创建固定高度的管理按钮，设置对象名和图标并绑定指定操作。"""
        button = QPushButton(text)
        button.setObjectName(name)
        button.setAutoDefault(False)
        button.setFixedHeight(30)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        icons.apply_icon(button, icon)
        button.setIconSize(QSize(14, 14))
        button.clicked.connect(callback)
        return button

    def set_presets(self, presets: list, current_id: str, rename_id: str | None = None) -> None:
        """填充列表；rename_id 给出时，填充后进入该预设的重命名模式"""
        selected_ids = set(self._selected_ids())
        anchor = self._list.item(self._anchor_row) if self._anchor_row >= 0 else None
        anchor_id = anchor.data(Qt.ItemDataRole.UserRole) if anchor else None
        scroll = self._list.verticalScrollBar().value()
        self._presets = list(presets)
        self._current_id = current_id
        self._list.blockSignals(True)
        try:
            self._list.clear()
            self._anchor_row = -1
            for index, p in enumerate(self._presets):
                item = QListWidgetItem()
                enabled_ids = getattr(p, "enabled_mod_ids", None)
                widget = PresetItemWidget(
                    preset_id=p.id,
                    name=p.display_name,
                    is_default=p.is_default,
                    is_current=(p.id == current_id),
                    mod_count=len(set(enabled_ids)) if enabled_ids is not None else None,
                )
                widget.apply_clicked.connect(self._on_apply)
                widget.rename_committed.connect(self._on_rename_committed)
                widget.request_select.connect(self._on_request_select)
                widget.request_rename.connect(self._on_request_rename)
                # 即使预设名很长，行宽仍跟随视口。
                item.setSizeHint(QSize(0, 60))
                item.setData(Qt.ItemDataRole.UserRole, p.id)
                self._list.addItem(item)
                self._list.setItemWidget(item, widget)
                item.setSelected(p.id in selected_ids)
                if p.id == anchor_id:
                    self._anchor_row = index
                    self._list.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            self._list.blockSignals(False)
        self._list.verticalScrollBar().setValue(scroll)
        self._count_label.setText(str(len(self._presets)))
        self._update_actions()
        if rename_id:
            self._enter_rename_mode_by_id(rename_id)
