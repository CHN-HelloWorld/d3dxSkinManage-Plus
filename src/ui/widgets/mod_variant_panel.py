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

    def eventFilter(self, watched, event):
        """单选标记移动或缩放时同步进度环位置，并保留默认事件过滤行为。"""
        if watched is self.radio and event.type() in (QEvent.Type.Move, QEvent.Type.Resize):
            self._position_progress_ring()
        return super().eventFilter(watched, event)

    def set_switching(self, active):
        """更新切换状态和进度环可见性，仅在行可见时启用旋转动画。"""
        self._switching = bool(active)
        self.progress_ring.setVisible(self._switching)
        self.progress_ring.set_indeterminate(self._switching and self.isVisible())
        if self._switching:
            self._position_progress_ring()
            self.progress_ring.raise_()

    def showEvent(self, event):
        """显示时校正进度环位置，并恢复仍在进行的切换动画。"""
        super().showEvent(event)
        self._position_progress_ring()
        self.progress_ring.set_indeterminate(self._switching)

    def hideEvent(self, event):
        """隐藏行时停止进度环旋转，避免后台无效刷新。"""
        self.progress_ring.set_indeterminate(False)
        super().hideEvent(event)

    def retheme(self):
        """按选中状态和当前主题更新行背景、名称、使用状态、单选标记与更多按钮样式。"""
        selected = bool(self.property("selected"))
        fill = theme.rgba("accent", 0.10) if selected else "transparent"
        border = theme.rgba("accent", 0.34) if selected else "transparent"
        self.setStyleSheet(
            f"#modVariantRow {{ background: {fill}; border: 1px solid {border}; border-radius: 5px; }}"
            f"#modVariantRow:hover {{ background: {theme.rgba('accent', 0.07) if not selected else fill}; }}"
            "#modVariantRow QLabel { background: transparent; border: none; padding: 0; }"
            f"#variantName "
            f"{{ color: {theme.surface_text_color('text.primary')}; "
            f"font-size: {theme.font('size_body')}px; "
            f"}}"
            f"#variantInUse {{ color: {theme.color('success')}; font-size: {theme.font('size_caption')}px; }}"
            "#variantSelect { background: transparent; border: none; padding: 0; min-height: 0; }"
            f"#variantSelect:focus {{ border: 1px dotted {theme.color('accent')}; border-radius: 4px; }}"
            "#variantMore { background: transparent; border: none; padding: 0; min-height: 0; min-width: 0; }"
            f"#variantMore:hover {{ background: {theme.rgba('accent', 0.1)}; border-radius: 4px; }}"
            f"#variantRadio {{ border: 1px solid {theme.color('accent' if selected else 'stroke.control')};"
            " border-radius: 7px; background: transparent; }"
            f"#variantRadioDot {{ border: none; border-radius: 3px; background: {theme.color('accent')}; }}"
        )


class ModVariantPanel(QWidget):
    """管理当前 Mod 的变体列表、选择状态和操作请求，并防止旧菜单操作误用新上下文。"""

    apply_requested = Signal(str, object)
    enable_requested = Signal(str, object)
    save_requested = Signal(str)
    update_requested = Signal(str, str)
    rename_requested = Signal(str, str)
    copy_requested = Signal(str, str)
    delete_requested = Signal(str, str)
    info_requested = Signal()

    def __init__(self, parent=None):
        """创建保存入口、可滚动变体列表和状态提示，初始化上下文代次与复用菜单。"""
        super().__init__(parent)
        self.setObjectName("modVariantPanel")
        self.setMinimumSize(0, 0)
        self.mod_id = ""
        self._state = {}
        self._variants = {}
        self._rows = {}
        self._pending_id = None
        self._context_generation = 0
        self._busy = False
        self._menu = QMenu(self)
        self._menu.setToolTipsVisible(True)
        acrylic.prepare_acrylic_menu(self._menu)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.save_button = QPushButton(tr("保存当前状态为新变体"))
        self.save_button.setObjectName("saveNewVariant")
        self.save_button.setMinimumHeight(34)
        icons.apply_icon(self.save_button, "actions/add")
        self.save_button.clicked.connect(lambda: self.save_requested.emit(self.mod_id) if self.mod_id else None)
        layout.addWidget(self.save_button)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setMinimumSize(0, 0)
        self._content = QWidget()
        self._content.setObjectName("variantScrollContent")
        self._content.setMinimumWidth(0)
        self._content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._list_layout = QVBoxLayout(self._content)
        self._list_layout.setContentsMargins(1, 3, 3, 6)
        self._list_layout.setSpacing(3)
        self.notice = QLabel()
        self.notice.setObjectName("variantWorkingNotice")
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        self.notice.setMinimumWidth(0)
        self.notice.hide()
        self._list_layout.addWidget(self.notice)
        self._group_label = QLabel()
        self._group_label.setObjectName("variantGroupLabel")
        self._list_layout.addWidget(self._group_label)
        self._empty_label = QLabel(tr("尚未保存变体"))
        self._empty_label.setObjectName("variantEmpty")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._list_layout.addWidget(self._empty_label)
        self._info_button = QPushButton(tr("查看 Mod 信息"))
        self._info_button.setObjectName("variantInfoLink")
        self._info_button.clicked.connect(self.info_requested)
        self._info_button.hide()
        self._list_layout.addWidget(self._info_button)
        self._list_layout.addStretch()
        self.scroll_area.setWidget(self._content)
        layout.addWidget(self.scroll_area, 1)
        self.retheme()
        self.clear_context()

    def set_state(self, state, variants, *, reference_counts=None):
        """应用服务中的当前选择，保留记录和滚动位置。"""
        scroll = self.scroll_area.verticalScrollBar().value()
        mod_id = str(state.get("mod_id") or "")
        changed_mod = mod_id != self.mod_id
        if changed_mod:
            self.clear_context()
            self.mod_id = mod_id
            scroll = 0
        self._close_menu()
        self._state = dict(state)
        self._variants = {item.variant_id: item for item in variants}
        ids = [None, *self._variants]
        self._pending_id = state.get("selected_variant_id")
        if self._pending_id not in ids:
            self._pending_id = None
        for variant_id in set(self._rows) - set(ids):
            widget = self._rows.pop(variant_id)
            self._list_layout.removeWidget(widget)
            widget.hide()
            widget.deleteLater()
        for variant_id in ids:
            if variant_id not in self._rows:
                widget = _VariantRow(variant_id)
                widget.selected.connect(self._on_variant_clicked)
                widget.menu_requested.connect(self._show_menu)
                self._rows[variant_id] = widget
            # 依次排列提示、默认项、分组、命名项和空状态。
            index = 1 if variant_id is None else 3 + list(self._variants).index(variant_id)
            self._list_layout.insertWidget(index, self._rows[variant_id])
        self._render_state()
        self._list_layout.activate()
        self.scroll_area.verticalScrollBar().setValue(scroll)

    def _render_state(self):
        """根据 Mod 启用状态、变量支持和忙碌状态更新变体列表及保存能力。

        区分待选变体与正在使用的变体，对无可保存变量和未确认修改分别展示说明。
        """
        enabled = bool(self._state.get("enabled"))
        reason = str(self._state.get("reason") or "")
        unsupported = (
            not self._variants
            and not self._state.get("variable_count")
            and (not reason or reason == tr("此 Mod 没有可保存的持久化数值变量"))
        )
        can_attempt_save = enabled and bool(self._state.get("can_save") or self._state.get("variable_count"))
        for variant_id, row in self._rows.items():
            saved = self._variants.get(variant_id)
            name = saved.name if saved else tr("Mod 默认")
            row.set_details(
                name,
                selected=variant_id == self._pending_id,
                in_use=variant_id == self._state.get("selected_variant_id"),
                enabled=enabled,
            )
            row.setVisible(not unsupported)
            row.setEnabled(not self._busy and bool(self.mod_id))
        self._group_label.setText(tr("已保存的变体 · {count}", count=len(self._variants)))
        self._group_label.setVisible(not unsupported)
        self._empty_label.setText(
            tr("暂无可保存的变体\n\n未发现可保存的外观变量。\n这个 Mod 仍可正常启用或禁用。")
            if unsupported
            else tr("尚未保存变体")
        )
        self._empty_label.setVisible(not self._variants)
        self._info_button.setVisible(unsupported)
        self.save_button.setVisible(not unsupported)
        # 确认时可从字面默认值补齐采集结果，实际保存前由服务重新验证。
        self.save_button.setEnabled(bool(self.mod_id) and can_attempt_save and not self._busy)
        self.save_button.setToolTip(tr("启用 Mod 后才能在游戏中调整并保存当前状态") if not enabled else reason)
        status = self._state.get("status")
        notice = (
            tr("有尚未保存的外观调整")
            if status == "modified"
            else tr("状态尚未确认")
            if status == "unconfirmed"
            else ""
        )
        if reason:
            notice = f"{notice}\n{reason}" if notice else reason
        self.notice.setText(notice)
        self.notice.setVisible(enabled and bool(notice) and not unsupported)

    def _on_variant_clicked(self, variant_id):
        """在上下文有效且未忙碌时，对默认变体或已知变体发出应用请求。"""
        if self.mod_id and not self._busy and (variant_id is None or variant_id in self._variants):
            self.apply_requested.emit(self.mod_id, variant_id)

    def select_variant(self, variant_id):
        """更新有效变体的待选状态并刷新列表，将对应行滚动到可见区域。"""
        if variant_id is not None and variant_id not in self._variants:
            return
        self._pending_id = variant_id
        self._render_state()
        row = self._rows.get(variant_id)
        if row is not None:
            self.scroll_area.ensureWidgetVisible(row, 0, 6)

    def set_busy(self, busy):
        """更新忙碌状态，关闭并禁用旧菜单后重新计算可执行操作。"""
        self._busy = bool(busy)
        self._close_menu()
        self._render_state()

    def set_switch_progress(self, variant_id=None, *, active=False):
        """显示任务目标，不改变已提交的选择。"""
        for row_id, row in self._rows.items():
            row.set_switching(active and row_id == variant_id)

    def _close_menu(self):
        """关闭变体菜单并禁用已有动作，防止关闭后的排队触发继续执行。"""
        self._menu.close()
        for action in self._menu.actions():
            action.setEnabled(False)

    def clear_context(self):
        """增加上下文代次、关闭菜单并清空当前 Mod 与待选状态，使旧操作回调失效。"""
        self._context_generation += 1
        self._close_menu()
        self.mod_id = ""
        self._state = {}
        self._pending_id = None
        self._busy = False
        self.save_button.setEnabled(False)
        self.set_switch_progress()

    def _show_menu(self, variant_id, point):
        """在当前 Mod 有效且未忙碌时构建指定变体的菜单并在给定全局位置显示。"""
        if self.mod_id and not self._busy:
            self.menu_for_variant(variant_id).popup(point)

    def menu_for_variant(self, variant_id):
        """两种指针入口均绑定打开菜单的记录。"""
        self._close_menu()
        self._menu.clear()
        acrylic.prepare_acrylic_menu(self._menu)
        saved = self._variants.get(variant_id)
        if not self.mod_id or (variant_id is not None and saved is None):
            return self._menu
        enabled = bool(self._state.get("enabled"))
        mid, generation = self.mod_id, self._context_generation

        def add(name, text, icon, signal, *, available=True, token="text.primary"):
            """向变体菜单添加带图标和可用性约束的动作，并绑定上下文校验后的请求回调。"""
            action = self._menu.addAction(text)
            action.setObjectName(name)
            icons.apply_icon(action, icon, token)
            action.setEnabled(bool(available) and not self._busy)

            def request(_checked=False):
                """仅在 Mod 标识、上下文代次和动作可用性仍一致时发出对应变体操作信号。"""
                if (
                    mid == self.mod_id
                    and generation == self._context_generation
                    and not self._busy
                    and action.isEnabled()
                ):
                    signal.emit(mid, variant_id)

            action.triggered.connect(request)
            return action

        apply_text = (tr("应用此变体") if saved else tr("应用 Mod 默认")) if enabled else tr("设为下次使用")
        add("variantApply", apply_text, "status/enabled" if saved else "actions/refresh", self.apply_requested)
        if not enabled:
            add("variantEnable", tr("启用并应用"), "status/enabled", self.enable_requested)
        if saved:
            self._menu.addSeparator()
            update = add(
                "variantUpdate",
                tr("用工作状态更新此变体"),
                "actions/refresh",
                self.update_requested,
                available=enabled and bool(self._state.get("can_save") or self._state.get("variable_count")),
            )
            update.setToolTip(
                tr("启用 Mod 后可用当前状态更新变体") if not enabled else str(self._state.get("reason") or "")
            )
            add("variantRename", tr("重命名"), "actions/edit", self.rename_requested)
            add("variantCopy", tr("创建副本"), "actions/copy", self.copy_requested)
            self._menu.addSeparator()
            add("variantDelete", tr("删除此变体"), "actions/remove", self.delete_requested, token="danger")
        return self._menu

    def retheme(self):
        """更新面板、列表说明和保存入口的主题样式，并逐行刷新变体外观。"""
        self.setStyleSheet(
            "#modVariantPanel, #variantScrollContent, #modVariantPanel QScrollArea,"
            " #modVariantPanel QScrollArea > QWidget { background: transparent; border: none; }"
            f"#variantGroupLabel {{ color: {theme.surface_text_color('text.secondary')};"
            f" font-size: {theme.font('size_body')}px; font-weight: 600;"
            f" border-top: 1px solid {theme.color('stroke.divider')}; padding: 8px 9px 4px; }}"
            f"#variantWorkingNotice {{ color: {theme.color('warning')}; background: {theme.rgba('warning', 0.09)};"
            f" border: none; border-radius: 4px; padding: 8px; font-size: {theme.font('size_secondary')}px; }}"
            f"#variantEmpty {{ color: {theme.surface_text_color('text.secondary')};"
            f" background: transparent; border: none; font-size: {theme.font('size_caption')}px; padding: 4px; }}"
            "#variantEmpty { padding: 18px 3px; }"
            f"#variantInfoLink {{ color: {theme.color('accent')}; background: transparent; border: none; }}"
            f"#saveNewVariant {{ margin-top: 8px; font-size: {theme.font('size_body')}px; }}"
        )
        for row in self._rows.values():
            row.retheme()
