"""插件管理页面，展示状态、目录、权限和错误，提供启停、重载及声明式设置。

插件变更通过管理器监听器刷新界面。
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import shiboken6
from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import (
    QBoxLayout,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.plugin_sdk.errors import PluginError
from src.ui import anim, icons, settings_style, theme
from src.ui.plugin_settings import PluginSettingsForm
from src.ui.smooth_scroll import SmoothScroller
from src.ui.widgets.tag_chip import TagFlowLayout
from src.ui.widgets.toggle_switch import ToggleSwitch
from src.utils.i18n import N_
from src.utils.i18n import t as tr
from src.utils.paths import get_plugins_dir

if TYPE_CHECKING:
    from src.plugins.runtime.manager import PluginManager
    from src.plugins.runtime.registry import PluginRecord


_STATE_TEXT = {
    "active": (N_("运行中"), "success"),
    "loaded": (N_("已加载"), "text.secondary"),
    "disabled": (N_("已停用"), "text.tertiary"),
    "discovered": (N_("待激活"), "text.tertiary"),
    "error": (N_("出错"), "danger"),
    "ready": (N_("待命"), "text.secondary"),
    "activating": (N_("正在激活"), "text.secondary"),
    "blocked": (N_("依赖受阻"), "warning"),
}

_PERMISSION_LABELS = {
    "mods:read": N_("读取 Mod"),
    "mods:write": N_("修改 Mod"),
    "mods:manage": N_("管理 Mod"),
    "storage": N_("存储设置"),
    "ui:tools-page": N_("工具箱入口"),
    "ui:menus": N_("右键菜单"),
    "ui:settings": N_("设置控件"),
    "ui:views": N_("插件视图"),
    "ui:shortcuts": N_("命令快捷键"),
    "network": N_("网络访问"),
    "fs:read": N_("读取文件"),
    "fs:write": N_("写入文件"),
    "background": N_("后台运行"),
    "process": N_("启动进程"),
    "native": N_("加载原生代码"),
}


def _permission_label(permission: str) -> str:
    """将已知权限标识转换为本地化名称，未知权限使用统一说明。"""
    return tr(_PERMISSION_LABELS.get(permission, N_("未识别权限")))


def _action_button(text: str, icon: str) -> QPushButton:
    """创建带主题图标的插件操作按钮，并设置可点击的光标。"""
    button = QPushButton(text)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    icons.apply_icon(button, icon)
    button.setIconSize(QSize(16, 16))
    return button


def _plain_widget() -> QWidget:
    """创建插件页使用的透明内容容器，避免继承面板背景和边框。"""
    widget = QWidget()
    widget.setObjectName("pluginPlain")
    widget.setStyleSheet("QWidget#pluginPlain { background: transparent; border: none; }")
    return widget


class _PluginEntry(QPushButton):
    """可选择的插件列表行，活动插件内容由详情面板持有。"""

    def __init__(self, record, parent=None):
        """创建可选中的插件列表项，登记插件身份，并使图标和名称的点击由整行接收。"""
        super().__init__(parent)
        self.setObjectName("pluginEntry")
        self.setProperty("pluginId", record.id)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(11)
        self._icon = QLabel()
        self._icon.setObjectName("pluginAvatar")
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon.setFixedSize(28, 28)
        self._icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self._name = QLabel()
        self._name.setTextFormat(Qt.TextFormat.PlainText)
        self._name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._name.installEventFilter(self)
        layout.addWidget(self._name, 1)
        self.update_record(record)

    def update_record(self, record):
        """替换列表项记录，同步无障碍名称、提示及主题样式，再按可用宽度省略名称。"""
        self._record = record
        self.setAccessibleName(record.name)
        self.setToolTip(f"{record.name}\n{record.description}")
        self.retheme()
        self._elide()

    def retheme(self):
        """刷新插件列表项的选中边框、悬停背景、名称颜色和主题图标。"""
        c = settings_style.color
        self.setStyleSheet(f"""
            QPushButton#pluginEntry {{ background: transparent; border: 1px solid transparent;
                border-radius: 7px; padding: 0; text-align: left; min-height: 56px; }}
            QPushButton#pluginEntry:hover {{ background: {theme.rgba("text.primary", 0.06)}; }}
            QPushButton#pluginEntry:checked {{ background: {settings_style.surface(over="nav")};
                border: 1px solid {settings_style.stroke()}; border-left: 3px solid {c("accent")}; }}
            QLabel#pluginAvatar {{ background: transparent; border: none; }}
        """)
        icons.apply_pixmap(self._icon, "navigation/plugins", 23, "settings.accent")
        self._name.setStyleSheet(f"background: transparent; color: {c('text')}; font-size: 14px; font-weight: 600;")

    def _elide(self):
        """在字体样式生效后按名称标签的实际宽度生成尾部省略文本。"""
        self._name.ensurePolished()
        self._name.setText(
            self._name.fontMetrics().elidedText(
                self._record.name, Qt.TextElideMode.ElideRight, max(0, self._name.contentsRect().width())
            )
        )

    def eventFilter(self, watched, event):
        """名称标签尺寸或字体变化时重新计算省略文本，兼顾子布局延后分配的几何。"""
        # 子布局可能晚于行尺寸事件分配几何。
        if event.type() in (QEvent.Type.Resize, QEvent.Type.FontChange) and hasattr(self, "_record"):
            self._elide()
        return super().eventFilter(watched, event)


class PluginCard(QFrame):
    """持续保留插件详情、设置和活动控件。"""

    def __init__(self, page: PluginsPage, rec: PluginRecord, parent=None) -> None:
        """捕获插件运行时身份和状态，构建详情控件并登记需要随主题调整的标签与设置行。"""
        super().__init__(parent)
        self._page = page
        self._rec = rec
        # 插件记录可变，构造时保存对应运行时身份。
        self._context = rec.context
        self._instance = rec.instance
        self._state = rec.state
        self._error = rec.error
        self._themed_labels: list[tuple[QLabel, str, str]] = []
        self._setting_rows = []
        self._settings_form = None
        self.setObjectName("pluginCard")
        # 详情面板使用样式背景；背景图模式下采用半透明表面，否则使用不透明卡片色。
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._build()
        self.retheme()

    def matches_record(self, rec) -> bool:
        """同时核对记录对象、上下文、实例及运行状态，判断现有详情卡是否仍可复用。"""
        return (
            self._rec is rec
            and self._context is rec.context
            and self._instance is rec.instance
            and self._state == rec.state
            and self._error == rec.error
        )

    def _style_label(self, label: QLabel, token: str, extra: str) -> None:
        """将标签设为纯文本并登记颜色令牌及附加样式，供主题变化时统一重绘。"""
        self._themed_labels.append((label, token, extra))
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setStyleSheet(f"background: transparent; color: {theme.color(token)}; {extra}")

    def retheme(self) -> None:
        """更新宿主样式，不重新创建插件视图或设置控件。"""
        self.setStyleSheet(f"""
            QFrame#pluginCard {{ background-color: {settings_style.surface(over="nav")}; border: none; }}
            QFrame#pluginSection {{ background: transparent; border: 1px solid {settings_style.stroke()};
                border-radius: 8px; }}
            QPushButton#pluginPermissions {{ background: transparent; border: none;
                border-top: 1px solid {settings_style.stroke()}; border-radius: 0;
                padding: 14px 0; text-align: left; color: {settings_style.color("secondary")}; font-size: 13px; }}
            QLabel#pluginPermissionChip {{ background: {settings_style.surface("soft", over="panel")};
                border: 1px solid {settings_style.stroke()}; border-radius: 4px; padding: 3px 8px;
                color: {settings_style.color("secondary")}; font-size: 12px; }}
        """)
        for label, token, extra in self._themed_labels:
            label.setStyleSheet(f"background: transparent; color: {theme.color(token)}; {extra}")
        self._toggle.blockSignals(True)
        self._toggle.setChecked(self._rec.enabled and self._rec.trusted)
        self._toggle.blockSignals(False)
        self._toggle.update()
        icons.retheme_icons(self)
        if self._settings_form is not None:
            self._settings_form.retheme()

    def _build(self) -> None:
        """构建插件身份、状态、启用开关、操作、设置与视图，并提供可展开的权限和来源说明。"""
        mf = self._rec.manifest
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        # 头部：名称 + 版本 + 状态 + 开关
        head = QHBoxLayout()
        symbol = QLabel()
        symbol.setFixedSize(44, 44)
        symbol.setAlignment(Qt.AlignmentFlag.AlignCenter)
        symbol.setStyleSheet("background: transparent;")
        icons.apply_pixmap(symbol, "navigation/plugins", 32, "settings.accent")
        head.addWidget(symbol, 0, Qt.AlignmentFlag.AlignTop)
        identity = QVBoxLayout()
        identity.setSpacing(5)
        name = QLabel(self._rec.name)
        name.setWordWrap(True)
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._style_label(name, "text.primary", "font-weight: 600; font-size: 19px;")
        identity.addWidget(name)

        byline = QHBoxLayout()
        ver = QLabel(f"v{mf.version}")
        self._style_label(ver, "text.tertiary", f"font-size: {theme.font('size_secondary')}px;")
        byline.addWidget(ver)

        byline.addStretch()
        identity.addLayout(byline)
        head.addLayout(identity, 1)

        txt, tok = _STATE_TEXT.get(self._rec.state.value, (self._rec.state.value, "text.secondary"))
        self._state_lbl = QLabel(tr(txt))
        self._style_label(self._state_lbl, tok, f"font-size: {theme.font('size_secondary')}px;")
        head.addWidget(self._state_lbl)

        self._toggle = ToggleSwitch()
        self._toggle.setAccessibleName(tr("启用 {name}", name=self._rec.name))
        self._toggle.blockSignals(True)
        self._toggle.setChecked(self._rec.enabled and self._rec.trusted)
        self._toggle.blockSignals(False)
        self._toggle.toggled.connect(self._on_toggled)
        head.addWidget(self._toggle)
        root.addLayout(head)

        # 作者 + 描述
        author = QLabel(mf.author)
        self._style_label(author, "text.tertiary", "font-size: 13px;")
        root.addWidget(author)
        meta = QLabel(self._rec.description)
        meta.setWordWrap(True)
        self._style_label(meta, "text.secondary", f"font-size: {theme.font('size_secondary')}px;")
        root.addWidget(meta)

        # 错误
        if self._rec.error:
            err = QLabel("⚠ " + self._rec.error)
            err.setWordWrap(True)
            self._style_label(err, "danger", f"font-size: {theme.font('size_secondary')}px;")
            root.addWidget(err)

        # 操作行
        actions = QHBoxLayout()
        reload_btn = _action_button(tr("重载"), "actions/refresh")
        reload_btn.setMinimumWidth(88)
        reload_btn.clicked.connect(lambda: self._page._reload(self._rec.id))
        actions.addWidget(reload_btn)
        uninstall_btn = _action_button(tr("卸载"), "actions/remove")
        uninstall_btn.setMinimumWidth(88)
        uninstall_btn.clicked.connect(lambda: self._page._uninstall(self._rec))
        actions.addWidget(uninstall_btn)
        actions.addStretch()
        root.addLayout(actions)

        self._build_settings(root)
        self._build_views(root)
        if self._rec.state.value != "active":
            note = QLabel(
                tr("插件已就绪，首次使用命令或视图时自动启动。")
                if self._rec.state.value == "ready"
                else tr("启用并满足依赖后可使用插件设置与功能。")
            )
            note.setWordWrap(True)
            self._style_label(note, "text.secondary", "font-size: 13px; padding: 16px 0;")
            root.addWidget(note)

        permissions = _action_button(
            tr("权限与来源 · {count} 项权限", count=len(mf.permissions)), "actions/chevron-down"
        )
        permissions.setObjectName("pluginPermissions")
        permissions.setCheckable(True)
        self._permissions = permissions
        root.addWidget(permissions)
        details = _plain_widget()
        permission_layout = QVBoxLayout(details)
        permission_layout.setContentsMargins(0, 0, 0, 0)
        permission_layout.setSpacing(11)
        if mf.permissions:
            chips = _plain_widget()
            flow = TagFlowLayout(chips, spacing=7)
            for permission in mf.permissions:
                chip = QLabel(_permission_label(permission))
                chip.setObjectName("pluginPermissionChip")
                chip.setTextFormat(Qt.TextFormat.PlainText)
                chip.setToolTip(permission)
                flow.addWidget(chip)
            permission_layout.addWidget(chips)
        else:
            no_permissions = QLabel(tr("此插件未申请额外权限。"))
            no_permissions.setWordWrap(True)
            self._style_label(no_permissions, "text.secondary", "font-size: 13px;")
            permission_layout.addWidget(no_permissions)
        source = QLabel(tr("插件目录：{path}", path=mf.directory))
        source.setObjectName("pluginSource")
        source.setWordWrap(True)
        source.setToolTip(str(mf.directory))
        source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._style_label(source, "text.tertiary", "font-size: 12px;")
        permission_layout.addWidget(source)
        diagnostics = tr("API：{api}", api=mf.api_version)
        if mf.dependencies:
            diagnostics += "\n" + tr(
                "依赖：{dependencies}",
                dependencies=", ".join(f"{pid} {constraint}" for pid, constraint in mf.dependencies.items()),
            )
        if self._rec.activation_reason:
            diagnostics += "\n" + tr(
                "激活原因：{reason}；加载 {load_ms:.1f} ms，激活 {activate_ms:.1f} ms",
                reason=self._rec.activation_reason,
                load_ms=self._rec.load_duration_ms,
                activate_ms=self._rec.activation_duration_ms,
            )
        runtime_info = QLabel(diagnostics)
        runtime_info.setWordWrap(True)
        runtime_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._style_label(runtime_info, "text.secondary", "font-size: 12px;")
        permission_layout.addWidget(runtime_info)
        details.hide()
        permissions.toggled.connect(details.setVisible)
        permissions.toggled.connect(
            lambda opened: icons.apply_icon(permissions, "actions/chevron-up" if opened else "actions/chevron-down")
        )
        root.addWidget(details)
        root.addStretch(1)

    def _section(self, root, title, icon):
        """向详情卡追加带图标和标题的面板，返回供调用方填充内容的布局。"""
        panel = QFrame()
        panel.setObjectName("pluginSection")
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(17, 14, 17, 17)
        layout.setSpacing(16)
        head = QHBoxLayout()
        symbol = QLabel()
        symbol.setFixedSize(20, 20)
        symbol.setStyleSheet("background: transparent;")
        icons.apply_pixmap(symbol, icon, 19, "text.secondary")
        head.addWidget(symbol)
        text = QLabel(title)
        self._style_label(text, "text.primary", "font-size: 15px; font-weight: 600;")
        head.addWidget(text, 1)
        layout.addLayout(head)
        root.addWidget(panel)
        return layout

    def _build_views(self, root: QVBoxLayout) -> None:
        """仅为已激活插件调用注册的视图工厂，将有效 Qt 控件纳入详情布局。"""
        pm = self._page.manager
        if pm is None or self._rec.state.value != "active":
            return
        view_contribs = pm.contributions.views(self._rec.id)
        if not view_contribs:
            return
        for c in view_contribs:
            view_id = str(c.data.get("id", ""))
            title = str(c.data.get("title", view_id))
            section = self._section(root, title, "navigation/plugins")
            widget = pm.create_view(self._rec.id, view_id)
            if isinstance(widget, QWidget) and shiboken6.isValid(widget):
                section.addWidget(widget)

    def _build_settings(self, root: QVBoxLayout) -> None:
        """为已就绪的插件创建共享配置表单，无需为了修改设置执行插件代码。"""
        pm = self._page.manager
        if pm is None or self._rec.state.value not in {"ready", "active"}:
            return
        items = pm.contributions.settings(self._rec.id)
        if not items:
            return
        section = self._section(root, tr("设置"), "settings/settings")
        try:
            form = PluginSettingsForm(pm.get_configuration(self._rec.id), items, self)
        except (OSError, ValueError, RuntimeError, PluginError) as exc:
            error = QLabel(tr("读取插件设置失败：{error}", error=exc))
            error.setWordWrap(True)
            self._style_label(error, "danger", "font-size: 13px;")
            section.addWidget(error)
            return
        self._settings_form = form
        self._setting_rows.extend(form.rows)
        section.addWidget(form)

    def resizeEvent(self, event):
        """详情宽度不足时将设置说明和控件纵向排列，宽度恢复后改回同行布局。"""
        super().resizeEvent(event)
        direction = QBoxLayout.Direction.TopToBottom if self.width() < 520 else QBoxLayout.Direction.LeftToRight
        for row in self._setting_rows:
            row.setDirection(direction)

    def _on_toggled(self, checked: bool) -> None:
        """启用前确认未受信插件的来源与权限，拒绝时恢复开关；其他操作转交插件管理器。"""
        pm = self._page.manager
        if pm is None:
            return
        if checked:
            if not self._rec.trusted:
                if not self._confirm_trust():
                    self._toggle.blockSignals(True)
                    self._toggle.setChecked(False)
                    self._toggle.blockSignals(False)
                    return
                pm.trust(self._rec.id)
            pm.enable(self._rec.id)
        else:
            pm.disable(self._rec.id)

    def _confirm_trust(self) -> bool:
        """以默认拒绝的确认框展示插件来源和权限，未知权限保留原始标识供检查。"""
        mf = self._rec.manifest
        perms = "\n".join(f"  · {_permission_label(p)}" for p in mf.permissions) or tr("  （无）")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("信任并启用插件？"))
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(
            tr(
                "插件「{name}」由 {author} 提供，将在本程序内执行代码。\n\n"
                "申请权限：\n{perms}\n\n仅在你信任来源时启用。",
                name=self._rec.name,
                author=mf.author,
                perms=perms,
            )
        )
        unknown = [permission for permission in mf.permissions if permission not in _PERMISSION_LABELS]
        if unknown:
            box.setDetailedText(tr("未识别的权限标识：\n") + "\n".join(unknown))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes


class PluginsPage(QWidget):
    """插件管理页面。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        """初始化插件记录、列表与详情缓存，以及惰性构建保护、选择和筛选状态。"""
        super().__init__(parent)
        self._manager: PluginManager | None = None
        self._records = {}
        self._entries = {}
        self._detail_cards = {}
        self._detail_scrolls = {}
        self._building_details = set()
        self._selected_id = None
        self._filter = "all"
        self._compact = None
        self._setup_ui()
        self.retheme()

    @property
    def manager(self) -> PluginManager | None:
        """返回宿主绑定的插件管理器，尚未绑定时返回空值。"""
        return self._manager

    def set_manager(self, manager: PluginManager) -> None:
        """注入 PluginManager 并订阅变更（由 MainWindow 调用一次）。"""
        self._manager = manager
        manager.add_change_listener(self.refresh)
        self.refresh()

    def _setup_ui(self) -> None:
        """构建安装操作栏、可搜索筛选的插件列表和惰性详情区域，启用两侧平滑滚动。"""
        self.setObjectName("pluginsPage")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.addStretch()

        install_btn = _action_button(tr("安装插件"), "actions/import")
        install_btn.clicked.connect(self._install_from_archive)
        header.addWidget(install_btn)

        rescan_btn = _action_button(tr("重新扫描"), "actions/refresh")
        rescan_btn.clicked.connect(self._rescan)
        header.addWidget(rescan_btn)

        open_dir_btn = _action_button(tr("打开插件目录"), "actions/folder")
        open_dir_btn.clicked.connect(self._open_dir)
        header.addWidget(open_dir_btn)
        layout.addLayout(header)

        self._discovery_notice = QLabel()
        self._discovery_notice.setWordWrap(True)
        self._discovery_notice.setTextFormat(Qt.TextFormat.PlainText)
        self._discovery_notice.hide()
        layout.addWidget(self._discovery_notice)

        workspace = QFrame()
        workspace.setObjectName("pluginsWorkspace")
        workspace.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(1, 1, 1, 1)
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setObjectName("pluginWorkspaceSplit")
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(1)
        workspace_layout.addWidget(self._splitter)
        layout.addWidget(workspace, 1)

        self._library = _plain_widget()
        self._library.setMinimumWidth(250)
        self._library.setMaximumWidth(420)
        library = QVBoxLayout(self._library)
        library.setContentsMargins(14, 17, 14, 14)
        library.setSpacing(12)
        library_header = QHBoxLayout()
        library_title = QLabel(tr("我的插件"))
        library_title.setStyleSheet("background: transparent; font-size: 15px; font-weight: 600;")
        library_header.addWidget(library_title)
        library_header.addStretch()
        self._library_count = QLabel("0")
        library_header.addWidget(self._library_count)
        library.addLayout(library_header)
        self._search = QLineEdit()
        self._search.setObjectName("pluginSearch")
        self._search.setAccessibleName(tr("搜索插件"))
        self._search.setPlaceholderText(tr("搜索插件名称、作者或描述"))
        self._search.setClearButtonEnabled(True)
        search_action = self._search.addAction(
            icons.get_icon("actions/search"), QLineEdit.ActionPosition.LeadingPosition
        )
        icons.apply_icon(search_action, "actions/search", "text.secondary")
        self._search.textChanged.connect(self._filter_rows)
        library.addWidget(self._search)
        filters = QHBoxLayout()
        filters.setSpacing(4)
        self._filter_buttons = {}
        self._filter_group = QButtonGroup(self)
        for key, label in (("all", tr("全部")), ("enabled", tr("已启用")), ("disabled", tr("已停用"))):
            button = QPushButton(label)
            button.setObjectName(f"pluginFilter-{key}")
            button.setProperty("pluginFilter", True)
            button.setCheckable(True)
            button.setChecked(key == "all")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked, value=key: self._set_filter(value))
            self._filter_group.addButton(button)
            self._filter_buttons[key] = button
            filters.addWidget(button)
        library.addLayout(filters)
        self._scroll = QScrollArea()
        self._scroll.setObjectName("pluginLibraryScroll")
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.viewport().setAutoFillBackground(False)
        self._content = _plain_widget()
        self._list_layout = QVBoxLayout(self._content)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(7)
        self._list_layout.addStretch()
        self._scroll.setWidget(self._content)
        SmoothScroller.attach(self._scroll)
        library.addWidget(self._scroll, 1)
        self._list_empty = QLabel(tr("没有匹配的插件"))
        self._list_empty.setWordWrap(True)
        self._list_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._list_empty.hide()
        library.addWidget(self._list_empty)
        self._library_note = QLabel(tr("插件可从压缩包安装，或放入插件目录后重新扫描。首次启用需确认信任。"))
        self._library_note.setWordWrap(True)
        library.addWidget(self._library_note)
        self._splitter.addWidget(self._library)
        self._details = QStackedWidget()
        self._details.setObjectName("pluginDetails")
        self._details.setMinimumWidth(0)
        self._empty = QLabel(tr("选择插件以查看详情与设置。"))
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        self._details.addWidget(self._empty)
        self._splitter.addWidget(self._details)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([300, 750])

    # 刷新

    def retheme(self) -> None:
        """更换表面时保留选择、设置和插件控件。"""
        c = settings_style.color
        workspace = theme.surface_color("bg.layer", "nav") if theme.is_background_active() else c("canvas")
        self.setStyleSheet(f"""
            QWidget#pluginsPage {{ background: transparent; }}
            QFrame#pluginsWorkspace {{ background: {workspace}; border: 1px solid {settings_style.stroke()};
                border-radius: 10px; }}
            QStackedWidget#pluginDetails, QScrollArea#pluginLibraryScroll,
            QScrollArea#pluginDetailScroll {{ background: transparent; border: none; }}
            QScrollArea#pluginLibraryScroll > QWidget, QScrollArea#pluginDetailScroll > QWidget {{
                background: transparent; border: none; }}
            QSplitter#pluginWorkspaceSplit::handle {{ background: {settings_style.stroke()}; }}
            QPushButton[pluginFilter="true"] {{ background: transparent; border: 1px solid transparent;
                border-radius: 5px; padding: 4px 5px; font-size: 12px; color: {c("secondary")}; }}
            QPushButton[pluginFilter="true"]:checked {{ background: {settings_style.surface(over="nav")};
                border-color: {settings_style.stroke()}; color: {c("text")}; }}
            QPushButton[pluginFilter="true"]:hover {{ background: {theme.rgba("text.primary", 0.06)}; }}
            QLineEdit#pluginSearch {{ font-size: 13px; padding: 6px; min-height: 24px; }}
        """)
        for label in (self._empty, self._library_count, self._list_empty, self._library_note):
            label.setStyleSheet(f"background: transparent; color: {c('secondary')}; font-size: 13px;")
        self._discovery_notice.setStyleSheet(f"color: {theme.color('danger')}; background: transparent;")
        for row in self._entries.values():
            row.retheme()
        for card in self._detail_cards.values():
            card.retheme()
        icons.retheme_icons(self)

    def refresh(self) -> None:
        """对账插件记录，仅替换或停止的运行时释放详情控件。"""
        recs = self._manager.records if self._manager else []
        errors = self._manager.discovery_errors if self._manager else {}
        self._discovery_notice.setVisible(bool(errors))
        self._discovery_notice.setText(tr("有 {count} 个插件未通过清单检查，悬停可查看详情。", count=len(errors)))
        self._discovery_notice.setToolTip("\n\n".join(errors.values()))
        recs = sorted(recs, key=lambda r: r.name)
        self._records = {rec.id: rec for rec in recs}
        for pid in list(self._entries):
            if pid not in self._records:
                row = self._entries.pop(pid)
                self._list_layout.removeWidget(row)
                row.hide()
                row.deleteLater()
        for pid, card in list(self._detail_cards.items()):
            rec = self._records.get(pid)
            if rec is None or not card.matches_record(rec):
                self._discard_detail(pid)
            else:
                card.retheme()
        for index, rec in enumerate(recs):
            row = self._entries.get(rec.id)
            if row is None:
                row = _PluginEntry(rec)
                row.clicked.connect(lambda _checked, pid=rec.id: self._select_plugin(pid))
                self._entries[rec.id] = row
            else:
                row.update_record(rec)
            self._list_layout.insertWidget(index, row)
        self._library_count.setText(str(len(recs)))
        enabled = sum(bool(rec.enabled and rec.trusted) for rec in recs)
        for key, label, count in (
            ("all", tr("全部"), len(recs)),
            ("enabled", tr("已启用"), enabled),
            ("disabled", tr("已停用"), len(recs) - enabled),
        ):
            self._filter_buttons[key].setText(f"{label} {count}")
        self._filter_rows()
        if self._selected_id not in self._records:
            self._selected_id = next(
                (rec.id for rec in recs if self._matches_filter(rec)), recs[0].id if recs else None
            )
        self._select_plugin(self._selected_id)

    def _discard_detail(self, plugin_id):
        """取消可能引用旧控件的淡出快照，再从缓存和页面栈移除插件详情并延后释放。"""
        # 释放插件控件前清理仍可能引用它们的淡出快照。
        if getattr(self._details, "_cross_fade_overlay", None) is not None:
            anim.cross_fade(self._details, None)
        card = self._detail_cards.pop(plugin_id)
        scroll = self._detail_scrolls.pop(plugin_id)
        card.hide()
        scroll.hide()
        self._details.removeWidget(scroll)
        scroll.deleteLater()

    def _select_plugin(self, plugin_id):
        """按需构建所选插件详情，构造后重验运行时身份，丢弃插件自行停用期间生成的旧视图。"""
        if plugin_id not in self._records:
            self._selected_id = None
            self._details.setCurrentWidget(self._empty)
            self._empty.setText(tr("未发现任何插件。可安装插件或打开插件目录。"))
            return
        self._selected_id = plugin_id
        for pid, row in self._entries.items():
            row.setChecked(pid == plugin_id)
        if plugin_id not in self._detail_cards:
            rec = self._records[plugin_id]
            views = rec.manifest.contributes.get("views", [])
            # 隐藏页面只同步选择和元数据，避免启动刷新触发插件入口或视图工厂。
            if views and not self.isVisible():
                return
            if plugin_id in self._building_details:
                return
            self._building_details.add(plugin_id)
            try:
                if rec.state.value == "ready" and views:
                    self.manager.activate(plugin_id, "onView:" + views[0]["id"])
                card = PluginCard(self, self._records[plugin_id])
            finally:
                self._building_details.discard(plugin_id)
            # 视图构造期间插件可能停止自身并同步刷新页面。
            current = self._records.get(plugin_id)
            if current is None or not card.matches_record(current):
                card.hide()
                card.deleteLater()
                self._select_plugin(self._selected_id)
                return
            scroll = QScrollArea()
            scroll.setObjectName("pluginDetailScroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.viewport().setAutoFillBackground(False)
            scroll.setWidget(card)
            SmoothScroller.attach(scroll)
            self._detail_cards[plugin_id] = card
            self._detail_scrolls[plugin_id] = scroll
            self._details.addWidget(scroll)
        if self._selected_id == plugin_id:
            target = self._detail_scrolls[plugin_id]
            if self._details.currentWidget() is not target:
                anim.cross_fade(self._details, lambda: self._details.setCurrentWidget(target))

    def showEvent(self, event) -> None:
        """实际显示时补建选中详情，已缓存视图继续保留原控件和输入状态。"""
        super().showEvent(event)
        self._select_plugin(self._selected_id)

    def _set_filter(self, value):
        """保存插件启用状态筛选条件，并立即更新列表可见性。"""
        self._filter = value
        self._filter_rows()

    def _matches_filter(self, record):
        """结合受信启用状态与分词搜索判断记录是否匹配，搜索覆盖名称、作者、描述和标识。"""
        enabled = bool(record.enabled and record.trusted)
        if self._filter == "enabled" and not enabled or self._filter == "disabled" and enabled:
            return False
        mf = record.manifest
        haystack = f"{mf.name} {mf.author} {mf.description} {mf.id} {record.name} {record.description}".casefold()
        return all(term in haystack for term in self._search.text().casefold().split())

    def _filter_rows(self, _query=None):
        """按当前筛选条件更新列表行，并区分没有匹配项与尚未安装插件的空状态。"""
        visible = 0
        for pid, row in self._entries.items():
            show = self._matches_filter(self._records[pid])
            row.setVisible(show)
            visible += show
        self._list_empty.setVisible(not visible)
        self._list_empty.setText(tr("没有匹配的插件") if self._records else tr("还没有安装插件"))

    def resizeEvent(self, event):
        """跨越窄屏阈值时切换列表与详情的横纵布局，并调整列表约束和说明可见性。"""
        super().resizeEvent(event)
        compact = self.width() < 800
        if compact == self._compact:
            return
        self._compact = compact
        self._splitter.setOrientation(Qt.Orientation.Vertical if compact else Qt.Orientation.Horizontal)
        self._library.setMaximumWidth(16777215 if compact else 420)
        self._library.setMaximumHeight(270 if compact else 16777215)
        self._library_note.setVisible(not compact)
        self._splitter.setSizes(
            [250, max(300, self.height() - 250)] if compact else [300, max(300, self.width() - 300)]
        )

    def _reload(self, plugin_id: str) -> None:
        """将指定插件的重载请求转交已绑定的管理器。"""
        if self._manager:
            self._manager.reload(plugin_id)

    def _uninstall(self, rec) -> None:
        """确认删除插件目录后请求卸载，未绑定管理器时忽略操作。"""
        if self._manager is None:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(tr("卸载插件？"))
        box.setText(tr("将停用并删除插件「{name}」及其目录，此操作不可撤销。", name=rec.name))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() == QMessageBox.StandardButton.Yes:
            self._manager.uninstall(rec.id)

    def _rescan(self) -> None:
        """请求管理器重新扫描并加载插件目录中的插件记录。"""
        if self._manager:
            self._manager.load_all()

    def _install_from_archive(self) -> None:
        """选择受支持归档后交由管理器安装或更新，显示结果并刷新插件列表。"""
        if self._manager is None:
            return
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(self, tr("选择插件压缩包"), "", tr("插件压缩包 (*.zip *.7z *.rar)"))
        if not path:
            return
        from src.plugins.runtime.installer import InstallError

        try:
            result = self._manager.install_archive(path)
        except InstallError as e:
            QMessageBox.warning(self, tr("安装失败"), tr("无法安装该插件：\n{e}", e=e))
            return
        except Exception as e:
            QMessageBox.warning(self, tr("安装失败"), tr("安装出错：\n{e}", e=e))
            return

        verb = (
            tr("更新（{previous_version}→{version}）", previous_version=result.previous_version, version=result.version)
            if result.was_update
            else tr("安装")
        )
        QMessageBox.information(
            self, tr("安装成功"), tr("插件「{name}」{verb}成功。可在列表中管理启用状态。", name=result.name, verb=verb)
        )
        self.refresh()

    def _open_dir(self) -> None:
        """请求系统文件管理器打开插件目录，无法打开时显示目录路径。"""
        path = str(get_plugins_dir())
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            QMessageBox.information(self, tr("插件目录"), path)
