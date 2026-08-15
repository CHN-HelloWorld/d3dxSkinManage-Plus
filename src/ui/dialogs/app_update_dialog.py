"""浏览发布说明，始终以最新安装包作为更新目标。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from src.services.app_update.checker import compare_versions
from src.ui.dialogs.app_update_widgets import (
    UpdateDialog,
    update_button,
    update_footer,
    update_header,
    update_label,
)
from src.utils.i18n import t as tr


class UpdatePromptDialog(UpdateDialog):
    """关闭后记录更新、跳过或稍后操作，版本选择仅切换说明内容。"""

    def __init__(self, version_info: dict, parent: QWidget | None = None) -> None:
        """根据版本检查结果构建发布历史；未显式选择操作时按稍后提醒处理。"""
        self.user_action = "later"
        self._info = version_info or {}
        self._latest = self._info.get("latest_version", "")
        self._current = self._info.get("current_version", "")
        self._has_update = self._info.get("has_update", compare_versions(self._latest, self._current) > 0)
        title = tr("发现新版本") if self._has_update else tr("当前已是最新版本")
        if not self._has_update and compare_versions(self._current, self._latest) > 0:
            title = tr("暂无可用更新")
        super().__init__(title, parent)
        self.setMinimumSize(720, 490)
        self.resize(820, 570)
        self._releases = self._info.get("releases") or [
            {
                "version": self._latest,
                "release_name": self._info.get("release_name", ""),
                "changelog": self._info.get("changelog", ""),
                "published_at": "",
            }
        ]
        self._build(title)
        self.prepare()

    def _build(self, title: str) -> None:
        """创建版本列表、说明区域与操作按钮，并默认展示首条发布记录。"""
        meta = [tr("当前版本 v{cur}", cur=self._current), tr("最新版本 v{latest}", latest=self._latest)]
        size = int(self._info.get("download_size", 0) or 0)
        if self._has_update and size > 0:
            meta.append(tr("更新包 {value:.1f} MB", value=size / 1024 / 1024))
        self.content.addWidget(
            update_header(title, "  ·  ".join(meta), "status/update" if self._has_update else "status/enabled")
        )
        body = QHBoxLayout()
        body.setContentsMargins(26, 0, 26, 18)
        body.setSpacing(20)
        history = QVBoxLayout()
        history.setSpacing(12)
        history.addWidget(update_label(tr("版本记录"), "caption"))
        self._release_list = QListWidget()
        self._release_list.setObjectName("releaseList")
        self._release_list.setAccessibleName(tr("版本记录"))
        self._release_list.setFixedWidth(194)
        self._release_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._release_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        history.addWidget(self._release_list, 1)
        body.addLayout(history)

        pane = QFrame()
        pane.setObjectName("releasePane")
        notes = QVBoxLayout(pane)
        notes.setContentsMargins(20, 18, 20, 18)
        notes.setSpacing(9)
        self._release_title = update_label("", "heading", wrap=True)
        self._release_meta = update_label("", "caption", wrap=True)
        notes.addWidget(self._release_title)
        notes.addWidget(self._release_meta)
        self._browser = QTextBrowser()
        self._browser.setObjectName("releaseNotes")
        self._browser.setAccessibleName(tr("更新说明"))
        self._browser.setOpenExternalLinks(True)
        self._browser.setMinimumWidth(300)
        self._browser.document().setDefaultStyleSheet("p, li { line-height: 150%; } h3 { margin-top: 14px; }")
        notes.addWidget(self._browser, 1)
        body.addWidget(pane, 1)
        self.content.addLayout(body, 1)

        for release in self._releases:
            version = release.get("version", "")
            row = QWidget()
            row.setObjectName("releaseRow")
            row.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout = QVBoxLayout(row)
            layout.setContentsMargins(12, 10, 12, 10)
            layout.setSpacing(6)
            top = QHBoxLayout()
            top.setSpacing(6)
            top.addWidget(update_label(f"v{version}"))
            top.addStretch()
            badge = (
                tr("当前")
                if compare_versions(version, self._current) == 0
                else tr("最新")
                if version == self._latest
                else ""
            )
            if badge:
                top.addWidget(update_label(badge, "badge"))
            layout.addLayout(top)
            date = release.get("published_at", "")[:10]
            layout.addWidget(update_label(date or tr("更新说明"), "caption"))
            item = QListWidgetItem(self._release_list)
            item.setSizeHint(QSize(176, 72))
            item.setData(Qt.ItemDataRole.AccessibleTextRole, f"v{version} {badge} {date}")
            self._release_list.setItemWidget(item, row)
        self._release_list.currentRowChanged.connect(self._select_release)
        self._release_list.setCurrentRow(0)

        info = QVBoxLayout()
        info.setContentsMargins(26, 0, 26, 18)
        info.setSpacing(7)
        if self._info.get("history_error"):
            info.addWidget(update_label(self._info["history_error"], "warning", wrap=True))
        if self._has_update:
            info.addWidget(update_label(tr("更新将保留你的 Mod、设置和插件。"), "caption", wrap=True))
        self.content.addLayout(info)

        footer, buttons = update_footer()
        self._skip_btn = update_button(tr("跳过此版本"))
        self._skip_btn.setObjectName("skipAction")
        self._skip_btn.clicked.connect(lambda: self._choose("skip"))
        self._skip_btn.setVisible(self._has_update)
        buttons.addWidget(self._skip_btn)
        buttons.addStretch()
        self._later_btn = update_button(
            tr("稍后提醒") if self._has_update else tr("完成"), primary=not self._has_update
        )
        self._later_btn.setObjectName("laterAction")
        self._later_btn.clicked.connect(lambda: self._choose("later"))
        buttons.addWidget(self._later_btn)
        self._update_btn = update_button(tr("更新至 v{version}", version=self._latest), primary=True)
        self._update_btn.setObjectName("updateAction")
        self._update_btn.setVisible(self._has_update)
        self._update_btn.setDefault(self._has_update)
        self._update_btn.clicked.connect(lambda: self._choose("update"))
        buttons.addWidget(self._update_btn)
        self.content.addWidget(footer)

    def _select_release(self, index: int) -> None:
        """展示有效索引对应的发布说明并复位滚动位置，不改变安装目标版本。"""
        if not 0 <= index < len(self._releases):
            return
        release = self._releases[index]
        version = release.get("version", "")
        self._release_title.setText(f"v{version}")
        name = release.get("release_name", "")
        date = release.get("published_at", "")[:10]
        parts = [name] if name and name.lstrip("vV") != version else []
        if date:
            parts.append(tr("发布于 {date}", date=date))
        self._release_meta.setText("  ·  ".join(parts))
        self._release_meta.setVisible(bool(parts))
        text = release.get("changelog", "") or tr("此版本暂无更新说明。")
        self._browser.setMarkdown(text)
        self._browser.verticalScrollBar().setValue(0)

    def _choose(self, action: str) -> None:
        """记录供更新管理器读取的动作并接受对话框；无更新时忽略更新和跳过操作。"""
        if action in ("update", "skip") and not self._has_update:
            return
        self.user_action = action
        self.accept()
