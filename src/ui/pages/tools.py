"""提供限定范围的维护工具和活动插件命令。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui import icons, settings_style, theme
from src.ui.smooth_scroll import SmoothScroller
from src.ui.widgets.tag_chip import TagFlowLayout
from src.utils.i18n import t


def _label(text="", role="body", name=""):
    """创建可换行的纯文本标签，并登记工具页样式角色与对象名称。"""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setMinimumWidth(0)
    label.setProperty("toolsRole", role)
    label.setObjectName(name)
    return label


def _symbol(name, size=24, token="settings.secondary"):
    """创建固定尺寸的主题图标标签，使用指定颜色令牌绘制图标。"""
    label = _label()
    label.setFixedSize(size, size)
    icons.apply_pixmap(label, name, size, token)
    return label


def _button(text, name, callback, *, primary=False, icon=""):
    """创建工具操作按钮，绑定回调并按主次样式配置图标与尺寸。"""
    button = QPushButton(text)
    button.setObjectName(name)
    button.setProperty("toolsButton", "primary" if primary else "standard")
    button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    button.setMinimumHeight(36)
    button.clicked.connect(callback)
    if icon:
        icons.apply_icon(button, icon, "settings.on_accent" if primary else "settings.text")
        button.setIconSize(QSize(17, 17))
    return button


class ToolCard(QWidget):
    """紧凑透明操作行，窄页面将按钮排列到说明下方。"""

    def __init__(self, title, description, btn_text, *, icon, scope="", name="", parent=None):
        """组合工具图标、作用范围、说明和操作按钮，保留网格布局以适应窄屏换行。"""
        super().__init__(parent)
        self.setObjectName(name or "toolCard")
        self.setProperty("toolsPlain", True)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(20, 15, 20, 15)
        self._grid.setHorizontalSpacing(16)
        self._grid.setVerticalSpacing(10)
        self._grid.setColumnStretch(1, 1)
        self._grid.addWidget(_symbol(icon), 0, 0, Qt.AlignmentFlag.AlignVCenter)
        copy = QWidget()
        copy.setProperty("toolsPlain", True)
        text = QVBoxLayout(copy)
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(4)
        self._title_label = _label(title, "strong")
        if scope:
            heading = QHBoxLayout()
            heading.setSpacing(9)
            heading.addWidget(self._title_label)
            heading.addWidget(_label(scope, "scope"))
            heading.addStretch()
            text.addLayout(heading)
        else:
            text.addWidget(self._title_label)
        self._desc_label = _label(description, "secondary")
        text.addWidget(self._desc_label)
        self._grid.addWidget(copy, 0, 1)
        self.button = _button(btn_text, "", lambda: None)
        self.button.setMinimumWidth(88)
        self._grid.addWidget(self.button, 0, 2, Qt.AlignmentFlag.AlignRight)
        self._compact = False

    def resizeEvent(self, event):
        """仅在跨越宽度阈值时移动操作按钮，使窄卡片中的按钮换到说明下方。"""
        super().resizeEvent(event)
        compact = self.width() < 490
        if compact != self._compact:
            self._compact = compact
            self._grid.removeWidget(self.button)
            self._grid.addWidget(
                self.button,
                1 if compact else 0,
                1 if compact else 2,
                Qt.AlignmentFlag.AlignLeft if compact else Qt.AlignmentFlag.AlignRight,
            )


class ToolsPage(QWidget):
    """提供分类预设、索引维护、日志及插件工具入口，由宿主接收业务操作请求。"""

    rebuild_index_requested = Signal()
    view_logs_requested = Signal()
    category_preset_import_requested = Signal()
    category_preset_export_requested = Signal()
    manage_plugins_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        """构建分类整理和维护工具分区，连接操作请求信号并启用平滑滚动。"""
        super().__init__(parent)
        self.setObjectName("toolsPage")
        self._plugin_group = None
        self._plugin_section = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setObjectName("toolsScroll")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.viewport().setAutoFillBackground(False)
        content = QWidget()
        content.setObjectName("toolsContent")
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(24, 20, 24, 24)
        self._content_layout.setSpacing(22)
        category, category_layout = self._section(t("分类整理"))
        card = self._body("categoryPresetCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(24, 16, 24, 22)
        card_layout.setSpacing(12)
        heading = QHBoxLayout()
        heading.setSpacing(12)
        heading.addWidget(_symbol("actions/folder", 26, "settings.accent"))
        heading.addWidget(_label(t("游戏分类预设"), "feature"))
        heading.addWidget(_label(t("当前游戏"), "scope"))
        heading.addStretch()
        card_layout.addLayout(heading)
        card_layout.addWidget(
            _label(t("导入分类预设，补充分类与目录图片。\n也可以把当前游戏的完整分类树分享给他人。"), "secondary")
        )
        card_layout.addSpacing(6)
        actions = TagFlowLayout(spacing=9)
        actions.addWidget(
            _button(
                t("导入分类预设"),
                "categoryPresetImport",
                self.category_preset_import_requested.emit,
                primary=True,
                icon="actions/import",
            )
        )
        actions.addWidget(
            _button(
                t("导出分类预设"),
                "categoryPresetExport",
                self.category_preset_export_requested.emit,
                icon="actions/export",
            )
        )
        card_layout.addLayout(actions)
        hint = QHBoxLayout()
        hint.setSpacing(6)
        hint.addWidget(_symbol("status/info", 14), 0, Qt.AlignmentFlag.AlignTop)
        hint.addWidget(_label(t("分享分类结构与目录图片，不包含 Mod 文件"), "muted"), 1)
        card_layout.addLayout(hint)
        category_layout.addWidget(card)
        self._content_layout.addWidget(category)

        maintenance, maintenance_layout = self._section(t("维护与排查"))
        group = self._body("toolsMaintenanceGroup")
        rows = QVBoxLayout(group)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(0)
        rebuild = ToolCard(
            t("重建索引"),
            t("从文件库重新扫描并恢复当前游戏的索引。"),
            t("重建索引"),
            icon="actions/refresh",
            scope=t("当前游戏"),
        )
        rebuild.button.setObjectName("rebuildIndexButton")
        rebuild.button.setToolTip(
            t("通常仅重建当前游戏的索引。若启动时检测到索引数据库损坏，将恢复所有已配置游戏的索引。")
        )
        rebuild.button.clicked.connect(self.rebuild_index_requested.emit)
        rows.addWidget(rebuild)
        rows.addWidget(self._divider())
        logs = ToolCard(
            t("查看日志"),
            t("查看应用运行日志，排查崩溃 / 报错 / 行为异常"),
            t("查看日志"),
            icon="navigation/overview",
            scope=t("应用"),
        )
        logs.button.setObjectName("viewLogsButton")
        logs.button.clicked.connect(self.view_logs_requested.emit)
        rows.addWidget(logs)
        maintenance_layout.addWidget(group)
        self._content_layout.addWidget(maintenance)
        self._content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)
        SmoothScroller.attach(scroll)
        self.retheme()

    @staticmethod
    def _body(name):
        """创建使用工具页透明背景规则的命名内容容器。"""
        body = QWidget()
        body.setObjectName(name)
        body.setProperty("toolsPlain", True)
        return body

    @staticmethod
    def _divider():
        """创建由主题样式控制颜色的单像素水平分隔线。"""
        divider = QFrame()
        divider.setObjectName("toolsDivider")
        divider.setFixedHeight(1)
        return divider

    @staticmethod
    def _section(title, action=None):
        """创建带标题和可选操作控件的工具分区，返回容器及供调用方追加内容的布局。"""
        section = QFrame()
        section.setProperty("toolsPanel", True)
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        heading = QHBoxLayout()
        heading.setContentsMargins(20, 14, 20, 0)
        heading.addWidget(_label(title, "strong", "toolsSectionTitle"), 1)
        if action is not None:
            heading.addWidget(action)
        layout.addLayout(heading)
        return section, layout

    def refresh_plugin_tools(self, items, executor) -> None:
        """释放已移除贡献项，只显示可执行命令。"""
        if self._plugin_section is not None:
            self._content_layout.removeWidget(self._plugin_section)
            self._plugin_section.hide()
            self._plugin_section.setParent(None)
            self._plugin_section.deleteLater()
            self._plugin_section = None
            self._plugin_group = None
        valid_items = [item for item in (items or []) if item.command.strip()]
        if not valid_items:
            return
        manage = _button(t("管理插件"), "managePluginsButton", self.manage_plugins_requested.emit)
        manage.setProperty("toolsButton", "quiet")
        section, layout = self._section(t("插件工具"), manage)
        section.setObjectName("toolsPluginSection")
        group = self._body("toolsPluginGroup")
        rows = QVBoxLayout(group)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(0)
        for index, item in enumerate(valid_items):
            if index:
                rows.addWidget(self._divider())
            icon = item.icon.strip()
            if not icon or icons.get_icon(icon).isNull():
                icon = "navigation/tools"
            card = ToolCard(
                item.title if item.title else item.command,
                item.plugin_name,
                t("执行"),
                icon=icon,
                name=f"pluginToolRow_{index}",
            )
            card._title_label.setObjectName(f"pluginToolTitle_{index}")
            card._desc_label.setObjectName(f"pluginToolSource_{index}")
            card.button.setObjectName(f"pluginToolExecute_{index}")
            card.button.clicked.connect(lambda _=False, command=item.command: executor(command))
            rows.addWidget(card)
        layout.addWidget(group)
        self._content_layout.insertWidget(self._content_layout.count() - 1, section)
        self._plugin_section, self._plugin_group = section, group
        self.retheme()

    def retheme(self) -> None:
        """根据当前主题重新生成工具页面板、文字和按钮样式，并刷新登记的图标。"""
        c = settings_style.color
        self.setStyleSheet(f"""
            QWidget#toolsPage, QWidget#toolsContent, QScrollArea#toolsScroll,
            QWidget#toolsPage QWidget#qt_scrollarea_viewport, QWidget[toolsPlain="true"] {{
                background: transparent; border: none;
            }}
            QWidget#toolsPage QLabel {{ background: transparent; border: none;
                color: {c("text")}; font-size: {theme.font("size_body")}px; }}
            QWidget#toolsPage QLabel[toolsRole="strong"] {{ font-weight: 600; }}
            QWidget#toolsPage QLabel[toolsRole="feature"] {{
                font-size: {theme.font("size_title")}px; font-weight: 600; }}
            QWidget#toolsPage QLabel[toolsRole="secondary"] {{
                color: {c("secondary")}; font-size: {theme.font("size_secondary")}px; }}
            QWidget#toolsPage QLabel[toolsRole="muted"], QWidget#toolsPage QLabel[toolsRole="scope"] {{
                color: {c("muted")}; font-size: {theme.font("size_caption")}px;
            }}
            QFrame[toolsPanel="true"] {{ background: {settings_style.surface()};
                border: 1px solid {settings_style.stroke()}; border-radius: {theme.radius("card")}px; }}
            QFrame#toolsDivider {{ background: {settings_style.stroke()}; border: none; }}
            QWidget#toolsPage QPushButton {{ color: {c("text")}; background: transparent;
                border: 1px solid {settings_style.stroke("control")}; border-radius: {theme.radius("small")}px;
                padding: 0 14px; min-height: 34px; font-size: {theme.font("size_body")}px; }}
            QWidget#toolsPage QPushButton:hover {{ background: {theme.rgba("text.primary", 0.06)}; }}
            QWidget#toolsPage QPushButton:focus {{ border-color: {c("accent")}; }}
            QWidget#toolsPage QPushButton[toolsButton="primary"] {{
                background: {c("accent")}; border-color: {c("accent")}; color: {c("on_accent")}; }}
            QWidget#toolsPage QPushButton[toolsButton="primary"]:hover {{ background: {theme.color("accent.hover")}; }}
            QWidget#toolsPage QPushButton[toolsButton="quiet"] {{ color: {c("accent")};
                border-color: transparent; font-size: {theme.font("size_secondary")}px;
                padding: 0 4px; min-height: 26px; }}
        """)
        icons.retheme_icons(self)
