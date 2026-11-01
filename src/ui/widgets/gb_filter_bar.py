"""GameBanana 搜索、排序、内容策略及下载入口。"""

from __future__ import annotations

from PySide6.QtCore import QSize, QTimer, Signal
from PySide6.QtWidgets import (
    QCompleter,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QToolButton,
)

from src.core.config import Config
from src.ui import icons
from src.ui.widgets.icon_combo import LeadingIconComboBox
from src.ui.widgets.ring_progress import RingProgressButton
from src.utils.i18n import N_, t
from src.utils.i18n import t as tr

SORT_LABEL_TO_API = {
    N_("默认"): "best_match",
    N_("最新"): "newest",
    N_("最近更新"): "latest",
    N_("最多喜欢"): "most_liked",
    N_("下载最多"): "most_downloaded",
    N_("浏览最多"): "most_viewed",
}

NSFW_LABEL_TO_KEY = {
    N_("隐藏 NSFW"): "remove",
    N_("模糊 NSFW"): "blur",
    N_("显示 NSFW"): "show",
}


class GbFilterBar(QFrame):
    """过滤栏"""

    search_submitted = Signal(str)
    sort_changed = Signal(str)
    nsfw_policy_changed = Signal(str)
    refresh_requested = Signal()

    def __init__(self, parent=None) -> None:
        """创建搜索、排序、成人内容策略和下载管理入口，并恢复已保存的筛选偏好。

        搜索输入采用三百毫秒防抖，回车可立即提交并更新近期搜索记录。
        """
        super().__init__(parent)
        self._config = Config()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(tr("搜索 Mod…"))
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._on_search_changed)
        self.search_box.returnPressed.connect(self._on_search_return)
        layout.addWidget(self.search_box, 1)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(300)
        self._debounce.timeout.connect(self._emit_search)

        self._recent_max = int(self._config.get("gamebanana.recent_searches_max", 10))
        self._completer = QCompleter(self.recent_searches(), self)
        self.search_box.setCompleter(self._completer)

        # 排序：下拉框内前置功能图标（图标不进下拉项）
        self.sort_combo = LeadingIconComboBox("actions/sort")
        self.sort_combo.setToolTip(tr("排序方式"))
        for label, api in SORT_LABEL_TO_API.items():
            self.sort_combo.addItem(t(label), api)
        self.sort_combo.currentIndexChanged.connect(
            lambda index: self.sort_changed.emit(self.sort_combo.itemData(index) or "best_match")
        )
        layout.addWidget(self.sort_combo)

        # NSFW 显示模式：下拉框内前置眼睛图标（图标不进下拉项）
        self.nsfw_combo = LeadingIconComboBox("status/visible")
        self.nsfw_combo.setToolTip(tr("显示：正常显示 NSFW Mod 预览\n隐藏：不显示 NSFW Mod\n模糊：模糊预览"))
        for label, policy in NSFW_LABEL_TO_KEY.items():
            self.nsfw_combo.addItem(t(label), policy)
        self.nsfw_combo.currentIndexChanged.connect(
            lambda index: self.nsfw_policy_changed.emit(self.nsfw_combo.itemData(index) or "blur")
        )
        layout.addWidget(self.nsfw_combo)

        # 下载按钮平时显示图标，有活动任务时由商店页驱动环形进度。
        self.dl_manager_btn = RingProgressButton(size=28, line_width=3)
        self.dl_manager_btn.set_icon("actions/download-manager", "text.primary")
        self.dl_manager_btn.set_ring_color("accent")
        self.dl_manager_btn.set_progress(-1)  # 无下载 → 不画环
        self.dl_manager_btn.setToolTip(tr("下载管理"))
        layout.addWidget(self.dl_manager_btn)

        # 保持当前类别，手动重新请求列表数据。
        self.refresh_btn = QToolButton()
        icons.apply_icon(self.refresh_btn, "actions/refresh")
        self.refresh_btn.setIconSize(QSize(16, 16))
        self.refresh_btn.setToolTip(tr("刷新当前列表"))
        self.refresh_btn.clicked.connect(self.refresh_requested.emit)
        layout.addWidget(self.refresh_btn)

        self._restore_state()

    def _restore_state(self) -> None:
        """按配置恢复默认排序与成人内容策略，未知配置值退回首个选项。"""
        sort_api = self._config.get("gamebanana.default_sort", "best_match")
        self.sort_combo.setCurrentIndex(max(0, self.sort_combo.findData(sort_api)))
        nsfw = self._config.get("gamebanana.nsfw_policy", "blur")
        self.nsfw_combo.setCurrentIndex(max(0, self.nsfw_combo.findData(nsfw)))

    def set_nsfw_policy(self, policy: str) -> None:
        """按策略键选择对应的成人内容显示选项，未知策略保持现状。"""
        index = self.nsfw_combo.findData(policy)
        if index >= 0:
            self.nsfw_combo.setCurrentIndex(index)

    def _on_search_changed(self, _t: str) -> None:
        """输入变化时重启搜索防抖计时器。"""
        self._debounce.start()

    def _on_search_return(self) -> None:
        """回车时取消延迟等待并立即提交当前搜索。"""
        self._debounce.stop()
        self._emit_search()

    def _emit_search(self) -> None:
        """去除搜索文本首尾空白，记录非空搜索词后发出搜索请求。"""
        q = self.search_box.text().strip()
        if q:
            self.add_recent_search(q)
        self.search_submitted.emit(q)

    def recent_searches(self) -> list[str]:
        """返回配置中近期搜索词的独立列表。"""
        return list(self._config.get("gamebanana.recent_searches", []) or [])

    def add_recent_search(self, q: str) -> None:
        """将搜索词去重置顶并按数量上限保存，同时更新自动补全候选。"""
        if not q:
            return
        items = self.recent_searches()
        if q in items:
            items.remove(q)
        items.insert(0, q)
        items = items[: self._recent_max]
        self._config.set("gamebanana.recent_searches", items)
        self._completer.model().setStringList(items)
