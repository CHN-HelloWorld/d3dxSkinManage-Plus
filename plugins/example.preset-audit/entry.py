"""检查组合预设中的 Mod 和命名变体引用，并在页面中展示待处理项。"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QHideEvent, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.plugin_sdk import PluginBase, PluginContext
from src.plugin_sdk.events import (
    GAME_CHANGED,
    INDEX_REBUILT,
    MOD_DELETED,
    MOD_IMPORTED,
    MOD_METADATA_CHANGED,
    MOD_UPDATED,
    PRESET_APPLIED,
    VARIANT_STATE_CHANGED,
)
from src.plugin_sdk.facades import ModInfo
from src.plugin_sdk.presets import PresetInfo


@dataclass(frozen=True)
class AuditIssue:
    """记录一个失效引用的稳定身份及展示名称，原因代码与界面语言分离。"""

    preset_id: str
    preset_name: str
    mod_id: str
    mod_name: str
    reason: str


@dataclass
class _AuditScan:
    """持有一次检查的独立快照和未完成查询，页面变化后整批丢弃。"""

    game_id: str
    presets: list[PresetInfo]
    mods: list[ModInfo]
    remaining: list[str]
    variants: dict[str, set[str]] = field(default_factory=dict)


def audit_presets(presets: list[PresetInfo], mods: list[ModInfo], variant_ids: dict[str, set[str]]) -> list[AuditIssue]:
    """按稳定身份检查预设引用，默认配置不要求存在命名变体。"""
    by_id = {mod.mod_id: mod for mod in mods}
    issues = []
    for preset in presets:
        for mod_id in dict.fromkeys((*preset.enabled_mod_ids, *preset.unresolved_mod_ids)):
            mod = by_id.get(mod_id)
            if mod is None or mod.game_id != preset.game_id:
                issues.append(AuditIssue(preset.preset_id, preset.name, mod_id, mod_id, "missing_mod"))
                continue
            variant_id = preset.mod_variants.get(mod_id)
            if variant_id is not None and variant_id not in variant_ids.get(mod_id, set()):
                issues.append(AuditIssue(preset.preset_id, preset.name, mod_id, mod.name, "missing_variant"))
    return issues


class PresetAuditPage(QWidget):
    """持有检查控件与刷新定时器，所有 SDK 查询和界面更新均在主线程完成。"""

    def __init__(self, ctx: PluginContext) -> None:
        """创建只读检查表、持久化选项及用于合并事件的单次定时器。"""
        super().__init__()
        self._ctx = ctx
        self._active = True
        self._scan: _AuditScan | None = None
        self.setObjectName("presetAuditPage")
        tr = ctx.i18n.t
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        title = QLabel(tr("预设检查"))
        title.setProperty("class", "title")
        layout.addWidget(title)
        description = QLabel(tr("检查当前游戏预设中失效的 Mod 和外观变体引用。请在主界面调整对应预设。"))
        description.setWordWrap(True)
        layout.addWidget(description)
        controls = QHBoxLayout()
        self._include_default = QCheckBox(tr("包含默认预设"))
        self._include_default.setChecked(ctx.storage.get("include_default", True) is not False)
        self._include_default.toggled.connect(self._save_filter)
        controls.addWidget(self._include_default)
        controls.addStretch()
        refresh_button = QPushButton(tr("重新检查"))
        refresh_button.clicked.connect(self.refresh)
        controls.addWidget(refresh_button)
        layout.addLayout(controls)
        self._summary = QLabel()
        self._summary.setObjectName("presetAuditSummary")
        self._summary.setTextFormat(Qt.TextFormat.PlainText)
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels([tr("预设"), tr("Mod"), tr("待处理项")])
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().hide()
        layout.addWidget(self._table, 1)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._advance)
        self._summary.setText(tr("尚未检查。"))

    def _save_filter(self, checked: bool) -> None:
        """保存筛选偏好；存储失败时提示用户，同时继续更新当前页面。"""
        if not self._active:
            return
        try:
            self._ctx.storage.set("include_default", checked)
        except OSError as error:
            self._ctx.ui.notify(self._ctx.i18n.t("无法保存检查设置：{error}", error=error), "error")
        self.refresh()

    def schedule_refresh(self, game_id: str = "", **_) -> None:
        """忽略其他游戏的通知，将同一轮事件循环中的多个变化合并为一次查询。"""
        if self._active and (not game_id or game_id == self._ctx.game.current_game_id()):
            self.refresh()

    def refresh(self) -> None:
        """作废旧批次，页面可见时才安排读取，避免隐藏页面拖慢宿主。"""
        if not self._active:
            return
        self._refresh_timer.stop()
        self._scan = None
        self._table.setRowCount(0)
        tr = self._ctx.i18n.t
        if not self.isVisible():
            self._summary.setText(tr("尚未检查。"))
            return
        self._summary.setText(tr("正在检查预设引用…"))
        self._refresh_timer.start(0)

    def _begin_scan(self) -> _AuditScan | None:
        """获取当前游戏快照，对需要检查的命名变体引用去重。"""
        ctx = self._ctx
        game_id = ctx.game.current_game_id()
        if not game_id:
            self._summary.setText(ctx.i18n.t("请先选择一个游戏。"))
            return None
        presets = [
            preset
            for preset in ctx.presets.get_presets()
            if preset.game_id == game_id and (self._include_default.isChecked() or not preset.is_default)
        ]
        mods = [mod for mod in ctx.mods.get_mods() if mod.game_id == game_id]
        known_ids = {mod.mod_id for mod in mods}
        referenced = {
            mod_id
            for preset in presets
            for mod_id in (*preset.enabled_mod_ids, *preset.unresolved_mod_ids)
            if mod_id in known_ids and preset.mod_variants.get(mod_id) is not None
        }
        return _AuditScan(game_id, presets, mods, sorted(referenced))

    def _advance(self) -> None:
        """每轮只读取一个 Mod 的变体，在批次之间交还主线程并检查页面有效性。"""
        if not self._active or not self.isVisible():
            return
        tr = self._ctx.i18n.t
        try:
            if self._scan is None:
                self._scan = self._begin_scan()
            scan = self._scan
            if scan is None:
                return
            if scan.game_id != self._ctx.game.current_game_id():
                self.refresh()
                return
            if scan.remaining:
                mod_id = scan.remaining.pop()
                scan.variants[mod_id] = {variant.variant_id for variant in self._ctx.variants.get_variants(mod_id)}
                if scan is not self._scan:
                    return
                self._summary.setText(
                    tr(
                        "正在检查变体：{done}/{total}",
                        done=len(scan.variants),
                        total=len(scan.variants) + len(scan.remaining),
                    )
                )
                self._refresh_timer.start(0)
                return
            self._show_report(scan)
            self._scan = None
        except (OSError, ValueError) as error:
            self._scan = None
            self._summary.setText(tr("检查未完成：{error}", error=error))

    def _show_report(self, scan: _AuditScan) -> None:
        """完成全部查询后统一展示结果，避免将部分读取误报为检查完成。"""
        tr = self._ctx.i18n.t
        issues = audit_presets(scan.presets, scan.mods, scan.variants)
        reasons = {
            "missing_mod": tr("Mod 不在当前游戏中"),
            "missing_variant": tr("已保存的外观变体不存在"),
        }
        names = {
            preset.preset_id: tr("默认") if preset.is_default and preset.name == "默认" else preset.name
            for preset in scan.presets
        }
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(issues))
        for row, issue in enumerate(issues):
            values = (names[issue.preset_id], issue.mod_name, reasons[issue.reason])
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(
                    tr("预设 ID：{preset_id}\nMod ID：{mod_id}", preset_id=issue.preset_id, mod_id=issue.mod_id)
                )
                self._table.setItem(row, column, item)
        self._table.setSortingEnabled(True)
        self._summary.setText(
            tr("已检查 {presets} 个预设，发现 {issues} 项失效引用。", presets=len(scan.presets), issues=len(issues))
        )

    def showEvent(self, event: QShowEvent) -> None:
        """进入页面时重新查询，覆盖尚未提供独立 SDK 事件的预设增删和改名。"""
        super().showEvent(event)
        self.schedule_refresh()

    def hideEvent(self, event: QHideEvent) -> None:
        """离开页面时停止文件查询，再次显示时重新建立快照。"""
        self._refresh_timer.stop()
        self._scan = None
        super().hideEvent(event)

    def stop(self) -> None:
        """先停止刷新，再由宿主释放页面及其子控件。"""
        self._active = False
        self._refresh_timer.stop()
        self._scan = None


class Plugin(PluginBase):
    """注册预设检查导航页，并将宿主数据变化转交页面合并刷新。"""

    def activate(self, ctx: PluginContext) -> None:
        """注册页面工厂与所需事件，无界面宿主只保留空页面引用。"""
        self._ctx = ctx
        self._page: PresetAuditPage | None = None
        ctx.ui.register_page(
            ctx.plugin_id + ".page", ctx.i18n.t("预设检查"), self._create_page, icon="navigation/tools"
        )
        for event in (
            GAME_CHANGED,
            INDEX_REBUILT,
            MOD_DELETED,
            MOD_IMPORTED,
            MOD_METADATA_CHANGED,
            MOD_UPDATED,
            PRESET_APPLIED,
            VARIANT_STATE_CHANGED,
        ):
            ctx.events.on(event, self._on_changed)

    def _create_page(self) -> QWidget:
        """创建由宿主管理父对象和主题的检查页面。"""
        self._page = PresetAuditPage(self._ctx)
        return self._page

    def _on_changed(self, **payload) -> None:
        """存在图形页面时通知其重新查询快照。"""
        if self._page is not None:
            self._page.schedule_refresh(**payload)

    def deactivate(self) -> None:
        """停止页面自有定时器，事件订阅和 Qt 控件由宿主回收。"""
        if self._page is not None:
            self._page.stop()
            self._page = None
