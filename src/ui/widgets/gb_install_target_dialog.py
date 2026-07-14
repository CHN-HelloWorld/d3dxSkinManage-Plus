"""安装目标对话框"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from src.services.gamebanana.install_resolver import ScoredCandidate
from src.ui import acrylic, anim
from src.utils.i18n import t as tr


class GbInstallTargetDialog(QDialog):
    """在下载内容无法唯一匹配分类时，提供目标目录选择与英文分类名记忆选项。"""

    def __init__(
        self,
        mod_name: str,
        candidates: list[ScoredCandidate],
        *,
        category_name: str = "",
        parent=None,
    ) -> None:
        """展示候选分类目录并支持文本筛选，存在来源分类名时提供记忆英文名选项。

        候选节点标识保存在列表项数据中，由确认后的访问方法交给安装流程使用。
        """
        super().__init__(parent)
        anim.install_intro_fade(self)
        self.setWindowTitle(tr("选择安装目标"))
        self.resize(420, 520)
        self._candidates = candidates
        self._by_id = {c.node_id: c for c in candidates}
        self._category_name = (category_name or "").strip()

        layout = QVBoxLayout(self)
        if self._category_name:
            # 说明在线分类无法唯一匹配本地目录的原因。
            lead = QLabel(
                tr(
                    "此 Mod 在 GameBanana 的分类标签是「{category_name}」，未能唯一确定安装目录，请选择：",
                    category_name=self._category_name,
                )
            )
        else:
            lead = QLabel(tr('无法自动匹配 "{mod_name}" 的目标，请手动选择：', mod_name=mod_name))
        lead.setWordWrap(True)
        layout.addWidget(lead)

        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("搜索分类…"))
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget, 1)
        for c in candidates:
            item = QListWidgetItem(c.category_path or c.display_name)
            item.setToolTip(item.text())
            item.setData(Qt.ItemDataRole.UserRole, c.node_id)
            self.list_widget.addItem(item)
        if candidates:
            self.list_widget.setCurrentRow(0)

        # 允许用户将分类名保存为所选目录的英文名，供后续精确匹配。
        # 默认不选中，因为分类可能表示通用分区，直接保存会影响后续自动安装。
        self.remember: QCheckBox | None = None
        if self._category_name:
            self.remember = QCheckBox(
                tr("把「{category_name}」记为所选目录的英文名（下次自动匹配）", category_name=self._category_name)
            )
            self.remember.setChecked(False)
            layout.addWidget(self.remember)
            self.list_widget.currentItemChanged.connect(lambda *_: self._sync_remember())
            self._sync_remember()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        acrylic.apply_dialog_acrylic(self)

    def _filter(self, text: str) -> None:
        """按不区分大小写的子串匹配隐藏不相关候选目录。"""
        t = text.strip().lower()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            item.setHidden(bool(t) and t not in item.text().lower())

    def _sync_remember(self) -> None:
        """仅在目录英文名为空时允许回填，保留用户已有名称。"""
        cb = self.remember
        if cb is None:
            return
        cand = self._current_candidate()
        ok = cand is not None and not (cand.name_en or "").strip()
        cb.setEnabled(ok)
        if not ok:
            cb.setChecked(False)
        cb.setToolTip("" if ok else tr("该目录已有英文名"))

    def _current_candidate(self) -> ScoredCandidate | None:
        """按当前列表项节点标识返回完整匹配候选，没有选中项时返回 None。"""
        item = self.list_widget.currentItem()
        if item is None:
            return None
        return self._by_id.get(item.data(Qt.ItemDataRole.UserRole))

    def selected_character_id(self) -> str | None:
        """返回当前安装目标的节点标识，没有选中项时返回 None。"""
        item = self.list_widget.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def remember_name_en(self) -> bool:
        """仅在记忆英文名复选框存在、可用且已勾选时返回 True。"""
        cb = self.remember
        return bool(cb is not None and cb.isEnabled() and cb.isChecked())
