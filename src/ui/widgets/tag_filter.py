"""总览使用的游戏标签目录和稳定身份筛选条件。"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QCollator, QLocale, QObject, QSize, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.models.tag import TagDefinition, tag_key
from src.ui import icons, theme
from src.ui.widgets.tag_chip import TagChip, TagFlowLayout
from src.ui.widgets.tag_display import TagPopup, show_tag_popup
from src.utils.i18n import t as tr


class _FilterButton(QPushButton):
    """在标签筛选按钮右侧绘制随启用状态变化的展开图标。"""

    def paintEvent(self, event) -> None:
        """完成按钮默认绘制后叠加展开箭头，并使用对应的启用或禁用颜色。"""
        super().paintEvent(event)
        painter = QPainter(self)
        token = "text.primary" if self.isEnabled() else "text.disabled"
        painter.drawPixmap(
            self.width() - 20, (self.height() - 12) // 2, icons.pixmap("actions/chevron-down", 12, token)
        )


class TagFilter(QObject):
    """维护当前游戏的标签目录及选择条件，协调筛选按钮、已选标签行和候选浮层。"""

    changed = Signal()

    def __init__(self, parent=None):
        """初始化标签目录和匹配模式，创建筛选入口及条件行并连接选择信号。"""
        super().__init__(parent)
        self.catalog: dict[str, TagDefinition] = {}
        self.selected_ids: list[str] = []
        self.match_mode = "all"
        self._collator = QCollator(QLocale(QLocale.Language.Chinese, QLocale.Country.China))
        self._collator.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._popup = None
        self.button = _FilterButton(tr("标签"), parent)
        self.button.setCheckable(True)
        self.button.setIconSize(QSize(14, 14))
        icons.apply_icon(self.button, "actions/tag")
        self.button.setToolTip(tr("按已使用的 Mod 标签筛选"))
        self.button.clicked.connect(self._toggle_popup)
        self.selection_row = QWidget(parent)
        self.selection_row.setMinimumWidth(0)
        self.selection_row.setStyleSheet("background: transparent;")
        row = QHBoxLayout(self.selection_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self._selection_title = QLabel(tr("标签筛选"))
        row.addWidget(self._selection_title)
        self._selected_widget = QWidget()
        self._selected_widget.setMinimumWidth(0)
        self._selected_layout = TagFlowLayout(self._selected_widget, spacing=6)
        row.addWidget(self._selected_widget, 1)
        self._mode_group = QButtonGroup(self)
        self._mode_buttons = {}
        for mode, label in (("all", tr("满足全部")), ("any", tr("满足任一"))):
            button = QToolButton()
            button.setText(label)
            button.setCheckable(True)
            button.setChecked(mode == self.match_mode)
            button.setAccessibleName(tr("标签匹配：{label}", label=label))
            button.clicked.connect(lambda _checked=False, m=mode: self.set_match_mode(m))
            self._mode_group.addButton(button)
            self._mode_buttons[mode] = button
            row.addWidget(button)
        clear = QToolButton()
        clear.setText(tr("清除"))
        clear.setToolTip(tr("仅清除标签条件"))
        clear.clicked.connect(self.clear)
        row.addWidget(clear)
        self.selection_row.hide()
        self.retheme()

    def set_catalog(self, tags: Iterable[TagDefinition]) -> None:
        """替换标签目录，剔除已经失效的选择标识，并刷新条件行和已有候选浮层。"""
        self.catalog = {tag.id: tag for tag in tags}
        self.selected_ids = [tag_id for tag_id in self.selected_ids if tag_id in self.catalog]
        self._render_selection()
        if self._popup is not None:
            self._render_candidates()

    def available_tags(self, query: str = "") -> list[TagDefinition]:
        """按规范化名称匹配已被引用的标签，优先按使用量降序，再按中文名称排序返回。"""
        query = tag_key(query)
        tags = [tag for tag in self.catalog.values() if tag.usage_count > 0 and query in tag.normalized_name]
        tags.sort(key=lambda tag: (-tag.usage_count, self._collator.sortKey(tag.name)))
        return tags

    def toggle(self, tag_id: str) -> None:
        """切换有效标签的选择状态并通知条件变化，筛选禁用时忽略请求。"""
        if not self.button.isEnabled() or tag_id not in self.catalog:
            return
        if tag_id in self.selected_ids:
            self.selected_ids.remove(tag_id)
        else:
            self.selected_ids.append(tag_id)
        self._render_selection()
        if self._popup is not None:
            self._render_candidates()
        self.changed.emit()

    def clear(self, *, emit=True) -> None:
        """清空标签条件并刷新界面，仅在选择确有变化且允许通知时发出信号。"""
        changed = bool(self.selected_ids)
        self.selected_ids.clear()
        self._render_selection()
        if self._popup is not None:
            self._render_candidates()
        if changed and emit:
            self.changed.emit()

    def set_match_mode(self, mode: str) -> None:
        """接受全部匹配或任一匹配模式，实际变化时同步按钮并通知重新筛选。"""
        if mode not in ("all", "any") or self.match_mode == mode:
            return
        self.match_mode = mode
        self._mode_buttons[mode].setChecked(True)
        self.changed.emit()

    def matches(self, names: Iterable[str]) -> bool:
        """按规范化标签名判断是否满足当前条件；未选标签或筛选禁用时返回 True。"""
        if not self.selected_ids or not self.button.isEnabled():
            return True
        keys = {tag_key(name) for name in names or () if isinstance(name, str)}
        selected = [self.catalog[tag_id].normalized_name for tag_id in self.selected_ids]
        match = all if self.match_mode == "all" else any
        return match(key in keys for key in selected)

    def set_mod_types_enabled(self, enabled: bool) -> None:
        """随当前条目类型启停标签筛选，禁用时关闭浮层但保留已选条件。"""
        self.button.setEnabled(enabled)
        tooltip = tr("按已使用的 Mod 标签筛选") if enabled else tr("标签仅适用于 Mod；切换到全部类型或 Mod 后可使用")
        self.button.setToolTip(tooltip)
        self.selection_row.setEnabled(enabled)
        if not enabled:
            self.close_popup()

    def close_popup(self) -> None:
        """关闭已有候选浮层，并同步筛选按钮的展开或已选状态。"""
        if self._popup is not None:
            self._popup.close()
        self._update_button()

    def _clear_layout(self, layout) -> None:
        """移出布局中的控件，先隐藏和解除父子关系，再安排延后销毁。"""
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _update_button(self) -> None:
        """将标签选择数量与浮层可见性同步到入口文案和选中状态。"""
        count = len(self.selected_ids)
        self.button.setText(tr("标签 {count}", count=count) if count else tr("标签"))
        self.button.setChecked(bool(count) or bool(self._popup and self._popup.isVisible()))

    def _render_selection(self) -> None:
        """重建可点击取消的已选标签行，同步条件行可见性和入口状态。"""
        self._clear_layout(self._selected_layout)
        for tag_id in self.selected_ids:
            chip = TagChip(self.catalog[tag_id], height=24, font_size=11)
            chip.setCheckable(True)
            chip.setChecked(True)
            chip.setAccessibleName(tr("取消筛选：{name}", name=chip.tag.name))
            chip.clicked_tag.connect(self.toggle)
            self._selected_layout.addWidget(chip)
        self.selection_row.setVisible(bool(self.selected_ids))
        self._update_button()

    def _toggle_popup(self) -> None:
        """切换候选浮层显示；打开时清空查询、刷新候选并将焦点交给搜索框。"""
        if self._popup is not None and self._popup.isVisible():
            self.close_popup()
            return
        if self._popup is None:
            self._build_popup()
        self._query.clear()
        self._render_candidates()
        show_tag_popup(self._popup, self.button)
        self._query.setFocus()
        self._update_button()

    def _build_popup(self) -> None:
        """懒创建带搜索、使用数量和滚动候选区的标签筛选浮层。"""
        self._popup = TagPopup(self.button, acrylic=True)
        self._popup.dismissed.connect(self._update_button)
        self._popup.setAccessibleName(tr("按标签筛选"))
        layout = QVBoxLayout(self._popup)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        header = QHBoxLayout()
        title = QLabel(tr("按标签筛选"))
        title.setStyleSheet("font-size: 13px; font-weight: bold;")
        header.addWidget(title)
        header.addStretch()
        self._caption = QLabel()
        header.addWidget(self._caption)
        layout.addLayout(header)
        self._query = QLineEdit()
        self._query.setPlaceholderText(tr("搜索已使用的标签..."))
        self._query.setAccessibleName(tr("搜索筛选标签"))
        self._query.setClearButtonEnabled(True)
        self._query.textChanged.connect(self._render_candidates)
        layout.addWidget(self._query)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setMinimumHeight(70)
        self._scroll.setMaximumHeight(210)
        self._chips = QWidget()
        self._chips.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._chips_layout = TagFlowLayout(self._chips, spacing=8)
        self._scroll.setWidget(self._chips)
        layout.addWidget(self._scroll)
        self._description = QLabel(tr("数字表示当前游戏中的使用数量"))
        self._description.setWordWrap(True)
        layout.addWidget(self._description)
        self._retheme_labels()

    def _render_candidates(self, _query: str = "") -> None:
        """依据当前查询重建候选标签和数量，更新选择标记并按屏幕空间调整浮层高度。"""
        self._clear_layout(self._chips_layout)
        tags = self.available_tags(self._query.text())
        for tag in tags:
            chip = TagChip(tag, count=tag.usage_count)
            chip.setCheckable(True)
            chip.setChecked(tag.id in self.selected_ids)
            chip.clicked_tag.connect(self.toggle)
            self._chips_layout.addWidget(chip)
        if not tags:
            empty = QLabel(tr("没有匹配的已用标签"))
            empty.setStyleSheet(f"color: {theme.color('text.tertiary')}; font-size: 12px;")
            self._chips_layout.addWidget(empty)
        count = len(self.selected_ids)
        self._caption.setText(tr("已选 {count} 个", count=count) if count else tr("点击气泡即可筛选"))
        popup_width = min(420, self.button.screen().availableGeometry().width() - 16)
        content_height = self._chips_layout.heightForWidth(max(1, popup_width - 26)) + 2
        self._scroll.setFixedHeight(min(210, max(42, content_height)))
        self._chips.updateGeometry()
        if self._popup.isVisible():
            show_tag_popup(self._popup, self.button)

    def _retheme_labels(self) -> None:
        """更新条件行标题及已有浮层内说明文字的主题颜色。"""
        self._selection_title.setStyleSheet(f"color: {theme.color('text.tertiary')}; font-size: 12px;")
        if self._popup is not None:
            self._caption.setStyleSheet(f"color: {theme.color('text.tertiary')}; font-size: 11px;")
            self._description.setStyleSheet(
                f"color: {theme.color('text.tertiary')}; font-size: 11px; padding-top: 10px;"
                f" border-top: 1px solid {theme.color('stroke.divider')};"
            )

    def retheme(self) -> None:
        """同步筛选入口、模式按钮和浮层样式，并重绘已选及候选标签。"""
        self._retheme_labels()
        self.button.setStyleSheet(
            "QPushButton { padding: 5px 24px 5px 10px; }"
            f"QPushButton:checked {{ border-color: {theme.color('accent')}; }}"
        )
        for button in self._mode_buttons.values():
            button.setStyleSheet(
                f"QToolButton {{ color: {theme.color('text.secondary')};"
                " padding: 3px 7px; border-radius: 3px; font-size: 11px; }"
                f"QToolButton:checked {{ background: {theme.color('bg.subtle')};"
                f" color: {theme.color('text.primary')}; }}"
            )
        if self._popup is not None:
            self._popup.retheme()
            self._render_candidates()
        self._render_selection()
