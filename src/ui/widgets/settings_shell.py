"""独立于配置处理的设置页布局和导航。"""

import shiboken6
from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractButton,
    QBoxLayout,
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLayout,
    QLineEdit,
    QPushButton,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.ui import anim, icons, theme
from src.ui import settings_style as style
from src.ui.pages.shortcuts import ShortcutsPage
from src.ui.widgets.settings_components import (
    SettingsCard,
    SettingsRow,
    icon_label,
    style_button,
    text_label,
)
from src.ui.widgets.settings_navigation import (
    SettingsCategoryPage,
    SettingSearchEntry,
    SettingsSearchPanel,
)
from src.utils.i18n import t
from src.utils.i18n import t as tr
from src.utils.qt_timers import call_later


class SettingsShellMixin:
    """为设置页面提供分类工作区、全局搜索、快捷键入口和搜索结果定位反馈。"""

    def _setup_ui(self):
        """构建顶部分类导航、搜索框、各类设置页及自动保存说明区域。

        建立搜索索引和快捷键绑定，连接命令目录变化，并准备搜索目标的限时高亮。
        """
        self.setObjectName("settingsPage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        outer = QVBoxLayout(self)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        outer.setContentsMargins(16, 16, 16, 16)
        self._workspace = QWidget()
        self._workspace.setObjectName("settingsWorkspace")
        self._workspace.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        outer.addWidget(self._workspace)
        layout = QVBoxLayout(self._workspace)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        self._restyle(self, style.stylesheet)
        header = QWidget()
        header.setObjectName("settingsToolbar")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._header_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, header)
        self._header_layout.setContentsMargins(20, 12, 20, 12)
        self._header_layout.setSpacing(16)
        self._stack = QStackedWidget()
        nav = self._build_top_nav(
            [
                (tr("常规"), "settings"),
                (tr("个性化"), "palette"),
                (tr("快捷键"), "keyboard"),
                (tr("导入与导出"), "import"),
                (tr("在线 Mod"), "globe"),
                (tr("更新"), "refresh"),
            ]
        )
        self._header_layout.addWidget(nav, 1)
        self._search_box = QWidget()
        self._search_box.setObjectName("settingsSearchBox")
        self._search_box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._search_box.setFixedHeight(40)
        search_layout = QHBoxLayout(self._search_box)
        search_layout.setContentsMargins(11, 0, 9, 0)
        search_layout.setSpacing(9)
        search_layout.addWidget(icon_label("search", 16, "settings.muted"))
        self._settings_search = QLineEdit()
        self._settings_search.setObjectName("settingsSearch")
        self._settings_search.setAccessibleName(tr("搜索全部设置"))
        self._settings_search.setPlaceholderText(tr("搜索设置，例如：缓存、代理、快捷键"))
        self._settings_search.setClearButtonEnabled(True)
        search_layout.addWidget(self._settings_search, 1)
        key = text_label("Ctrl K", "breadcrumb")
        key.setObjectName("settingsSearchKey")
        key.setFixedHeight(22)
        self._settings_search_key = key
        search_layout.addWidget(key)
        self._header_layout.addWidget(self._search_box, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(header)
        self._shortcuts_page = ShortcutsPage(self._shortcut_settings, embedded=True)
        shortcuts = SettingsCategoryPage(
            tr("快捷键"),
            [
                ("keymap", tr("按键绑定"), tr("按使用场景分组，点击按键即可修改。"), [self._shortcuts_page]),
            ],
        )
        self._shortcuts_page.attach_scroll(shortcuts._scrolls["keymap"])
        for page in (
            self._build_general_page(),
            self._build_background_page(),
            shortcuts,
            self._build_import_page(),
            self._build_store_page(),
            self._build_update_page(),
        ):
            self._stack.addWidget(page)
        self._content_stack = QStackedWidget()
        self._content_stack.addWidget(self._stack)
        self._search_results = SettingsSearchPanel()
        self._search_results.activated.connect(self._open_search_result)
        self._content_stack.addWidget(self._search_results)
        layout.addWidget(self._content_stack, 1)
        self._settings_footer = QWidget()
        self._settings_footer.setObjectName("settingsFooter")
        self._settings_footer.setFixedHeight(43)
        self._settings_footer.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        footer = QHBoxLayout(self._settings_footer)
        footer.setContentsMargins(20, 0, 20, 0)
        footer.setSpacing(7)
        footer.addWidget(icon_label("check", 12, "settings.teal"))
        footer.addWidget(text_label(tr("更改即时生效并自动保存"), "muted"))
        footer.addStretch(1)
        reset = style_button(QPushButton(t("恢复默认")), link=True)
        reset.clicked.connect(self._reset_settings)
        footer.addWidget(reset)
        layout.addWidget(self._settings_footer)
        self._search_entries = self._build_search_index()
        self._settings_search.textChanged.connect(self._search_settings)
        self._search_shortcut = QShortcut(self)
        self._search_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._search_shortcut.activated.connect(self._focus_settings_search)
        self._shortcut_settings.changed.connect(self._refresh_search_shortcut)
        self._refresh_search_shortcut()
        self._highlighted_setting = None
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(self._clear_setting_highlight)
        self._shortcut_settings.catalog_changed.connect(self._refresh_shortcut_search)

    def _refresh_search_shortcut(self):
        """按快捷键设置更新搜索动作及提示文字，未绑定时禁用快捷键并隐藏提示。"""
        binding = self._shortcut_settings.binding("settings.search")
        self._search_shortcut.setKey(QKeySequence(binding or ""))
        self._search_shortcut.setEnabled(bool(binding))
        self._settings_search_key.setText((binding or "").replace("+", " "))
        self._settings_search_key.setVisible(bool(binding))

    def _build_top_nav(self, items):
        """创建互斥的设置分类导航按钮及活动指示条，并绑定分类切换操作。"""
        bar = QWidget()
        bar.setObjectName("settingsTabs")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._nav_group = QButtonGroup(self)
        self._settings_nav_btns = []
        for index, (title, icon) in enumerate(items):
            button = QPushButton(title)
            button.setObjectName("settingsNavBtn")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            icons.apply_icon(button, f"settings/{icon}", "text.primary")
            button.setIconSize(QSize(18, 18))
            button.setChecked(index == 0)
            button.clicked.connect(lambda _checked, i=index: self._switch_settings_section(i))
            self._nav_group.addButton(button, index)
            self._settings_nav_btns.append(button)
            layout.addWidget(button)
        layout.addStretch(1)
        self._settings_indicator = QWidget(bar)
        self._settings_indicator.setObjectName("settingsNavIndicator")
        self._settings_indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._settings_indicator.hide()
        return bar

    def _switch_settings_section(self, idx: int):
        """清空搜索并切换到有效分类，同步按钮选中状态和活动指示条。"""
        if not 0 <= idx < self._stack.count():
            return
        self._settings_search.clear()
        if self._stack.currentIndex() != idx:
            anim.cross_fade(self._stack, lambda: self._stack.setCurrentIndex(idx))
        self._settings_nav_btns[idx].setChecked(True)
        self._update_settings_indicator(idx)

    def _update_settings_indicator(self, idx: int, animate: bool = True):
        """按目标分类按钮的几何位置放置底部指示条，可选择平移动画或立即定位。"""
        if not 0 <= idx < len(self._settings_nav_btns):
            return
        button = self._settings_nav_btns[idx]
        inset = theme.radius("card")
        target = QRect(button.x() + inset, button.y() + button.height() - 3, max(0, button.width() - 2 * inset), 3)
        indicator = self._settings_indicator
        if animate and indicator.isVisible():
            anim.animate_geometry(indicator, target)
        else:
            indicator.setGeometry(target)
        indicator.show()
        indicator.raise_()

    def _refresh_font_layout(self):
        """根据当前字体测量搜索提示及导航所需宽度，必要时将工具栏改为上下排列。

        布局调整后立即重定位活动指示条，避免字体或窗口尺寸变化造成错位。
        """
        search = self._settings_search
        search.ensurePolished()
        margins = search.textMargins()
        # 输入框默认尺寸提示不为占位文字预留空间。
        search.setMinimumWidth(
            search.fontMetrics().horizontalAdvance(search.placeholderText()) + margins.left() + margins.right() + 12
        )
        search_width = max(360, self._search_box.layout().minimumSize().width())
        self._search_box.setFixedWidth(search_width)
        header = self._header_layout
        margins = header.contentsMargins()
        required_width = (
            header.itemAt(0).widget().minimumSizeHint().width()
            + search_width
            + header.spacing()
            + margins.left()
            + margins.right()
        )
        header.setDirection(
            QBoxLayout.Direction.TopToBottom
            if header.parentWidget().width() < required_width
            else QBoxLayout.Direction.LeftToRight
        )
        self._update_settings_indicator(max(0, self._nav_group.checkedId()), animate=False)

    def changeEvent(self, event):
        """字体或样式变化后排队刷新工具栏布局，避免在样式事件栈内递归调整。"""
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(self, "_settings_search"):
            call_later(0, self, self._refresh_font_layout)

    def resizeEvent(self, event):
        """窗口尺寸变化后排队重新测量导航和搜索布局。"""
        super().resizeEvent(event)
        call_later(0, self, self._refresh_font_layout)

    def _make_group(self, title: str, *, icon: str = "", description: str = "", badge: str = ""):
        """根据标题选择默认图标并创建设置卡片，返回卡片和可添加设置行的布局。"""
        names = {
            "Mod 管理": "box",
            "代理配置": "globe",
            "下载数据": "import",
            "外观": "palette",
            "整窗背景": "image",
            "显示效果": "settings",
            "动态背景": "image",
            "导入": "import",
            "导出": "box",
            "在线 Mod 代理": "globe",
            "Mod 更新": "refresh",
            "程序更新": "refresh",
        }
        translated_names = {tr(source): name for source, name in names.items()}
        card = SettingsCard(title, icon or translated_names.get(title, "settings"), description, badge)
        return card, card.rows

    def _add_row(self, rows, title, control, desc="", *, extra=None, details="", keywords=""):
        """创建带搜索关键词的设置行，补齐分隔线和按钮样式后添加到目标布局。"""
        row = SettingsRow(title, control, desc, extra=extra, details=details, keywords=keywords)
        row.setProperty("settingsSeparator", bool(rows.count()))
        for button in row.findChildren(QPushButton):
            if not button.objectName().startswith("settings"):
                style_button(button)
        rows.addWidget(row)
        return row

    def _add_caption(self, rows, text):
        """在设置分组内追加具有统一边距的可换行说明文字。"""
        caption = text_label(text, "smallHelp", wrap=True)
        caption.setContentsMargins(20, 8, 20, 16)
        rows.addWidget(caption)

    def _focus_settings_search(self):
        """将焦点移到设置搜索框并全选现有查询文字。"""
        self._settings_search.setFocus()
        self._settings_search.selectAll()

    @staticmethod
    def _setting_search_text(target):
        # 按可访问名称和真实操作控件索引设置，不将用户输入值或临时用量当作设置名称。
        """合并设置自身说明、控件无障碍文字、组合框选项和按钮文字作为搜索内容。"""
        parts = [target.search_text]
        if isinstance(target, SettingsRow):
            parts.append(target.title_label.text())
        for control in target.findChildren(QWidget):
            if not isinstance(control, (QAbstractButton, QComboBox, QLineEdit, QSlider)):
                continue
            parts.extend((control.accessibleName(), control.accessibleDescription()))
            if isinstance(control, QComboBox):
                parts.extend(control.itemText(index) for index in range(control.count()))
            elif isinstance(control, QAbstractButton):
                parts.append(control.text())
        return " ".join(parts)

    def _build_search_index(self):
        """遍历设置卡片、设置行和快捷键命令，生成带分类路径与目标控件的搜索索引。"""
        entries = []
        for index in range(self._stack.count()):
            category = self._stack.widget(index)
            for target in [*category.findChildren(SettingsCard), *category.findChildren(SettingsRow)]:
                section = category.section_for(target)
                if section is None:
                    continue
                context = f"{category.category}  ›  {category.section_titles[section]}"
                keywords = f"{context} {self._setting_search_text(target)}"
                if target.isAncestorOf(self._font_combo):
                    keywords += tr(" font typeface 字体")
                title = target.title_label.text() if isinstance(target, SettingsRow) else target.title_text
                entries.append(SettingSearchEntry(index, section, title, context, keywords, target))
        for command in self._shortcuts_page.settings.commands:
            entries.append(
                SettingSearchEntry(
                    2,
                    "keymap",
                    command.display_title,
                    tr("快捷键  ›  按键绑定"),
                    tr(
                        "快捷键 keyboard shortcut {title} {description} {default}",
                        title=command.display_title,
                        description=command.display_description,
                        default=command.default,
                    ),
                    self._shortcuts_page.binding_buttons[command.id],
                )
            )
        return entries

    def _search_settings(self, query):
        """非空查询重新建立索引并展示搜索结果，清空查询时恢复分类页面。"""
        if query.strip():
            # 模式变化可改变行名称，搜索结果使用当前显示内容。
            self._search_entries = self._build_search_index()
            self._search_results.search(query, self._search_entries)
            self._content_stack.setCurrentWidget(self._search_results)
        else:
            self._content_stack.setCurrentWidget(self._stack)

    def _refresh_shortcut_search(self):
        """命令目录变化后清除失效高亮并重建索引，保留当前查询并刷新结果。"""
        self._clear_setting_highlight()
        self._search_entries = self._build_search_index()
        query = self._settings_search.text()
        if query.strip():
            self._search_results.search(query, self._search_entries)
            self._content_stack.setCurrentWidget(self._search_results)

    def _open_search_result(self, category, section, target):
        """验证目标 Qt 控件仍有效后切换分类与小节，短时高亮并延迟滚动到目标位置。

        目标已失效时重建搜索结果，避免继续访问已销毁控件。
        """
        if target is None or (not shiboken6.isValid(target)):
            self._refresh_shortcut_search()
            return
        self._switch_settings_section(category)
        page = self._stack.widget(category)
        page.select_section(section)
        self._clear_setting_highlight()
        target.setProperty("settingsHighlight", True)
        target.style().unpolish(target)
        target.style().polish(target)
        self._highlighted_setting = target
        self._highlight_timer.start(2000)
        call_later(
            0,
            page,
            lambda: page.reveal(target) if shiboken6.isValid(page) and shiboken6.isValid(target) else None,
        )

    def _clear_setting_highlight(self):
        """清空高亮引用，并对仍有效的目标移除高亮属性后重新应用样式。"""
        target = self._highlighted_setting
        self._highlighted_setting = None
        if target is not None and shiboken6.isValid(target):
            target.setProperty("settingsHighlight", False)
            target.style().unpolish(target)
            target.style().polish(target)
