"""使用宿主样式绑定构建旧版迁移向导，不维护独立调色盘或后台 Qt 线程。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.plugin_sdk import DisposableStore, PluginContext

from .controller import MigrationController
from .preview import MigrationPreviewModel, format_size


class MigrationWizard(QWidget):
    """展示来源、完整预览和结果，在显式确认后启动本地复制。"""

    def __init__(self, ctx: PluginContext) -> None:
        """样式与任务分别由宿主绑定和窗口所有的控制器管理。"""
        super().__init__()
        self.ctx = ctx
        self._closed = False
        self._styles = DisposableStore()
        self._environments = ()
        self.setObjectName("legacyMigrationWizard")
        self.setMinimumSize(700, 600)
        self.resize(840, 700)
        self.controller = MigrationController(ctx, self)
        self._build()
        self.controller.changed.connect(self._render)
        self.destroyed.connect(self.controller.shutdown)
        self._render()

    def _style(self, widget, role: str):
        """保存宿主样式订阅，窗口关闭后立即释放。"""
        self._styles.add(self.ctx.ui.apply_style(widget, role))
        return widget

    def _label(self, text: str, role: str = "muted", *, wrap: bool = False) -> QLabel:
        """统一文本格式和字体层级，源文件名不会被解释成富文本。"""
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(wrap)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        return self._style(label, role)

    def _button(self, text: str, callback, *, primary: bool = False) -> QPushButton:
        """建立统一的主次按钮，并显式关闭对话框默认按钮行为。"""
        button = QPushButton(text)
        button.setAutoDefault(False)
        button.clicked.connect(callback)
        return self._style(button, "primary_button" if primary else "button")

    def _card(self, layout: QVBoxLayout) -> QVBoxLayout:
        """给内容分组添加与宿主一致的卡片和留白。"""
        frame = self._style(QFrame(), "card")
        content = QVBoxLayout(frame)
        content.setContentsMargins(16, 14, 16, 14)
        content.setSpacing(10)
        layout.addWidget(frame)
        return content

    def _build(self) -> None:
        """按来源、预览、执行结果构造单页流程，长列表由模型延迟显示。"""
        tr = self.ctx.i18n.t
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(14)
        layout.addWidget(self._label(tr("从旧版迁移"), "title"))
        layout.addWidget(self._label(tr("将旧版的本地 Mod、分类和整理信息复制到当前游戏。"), wrap=True))
        self._build_source(layout)
        self._build_preview(layout)
        layout.addWidget(
            self._label(
                tr("只复制本地资源，保留旧版文件。新导入的 Mod 保持停用，部署目录、快捷键和旧版程序设置不会迁移。"),
                wrap=True,
            )
        )
        self.progress_bar = self._style(QProgressBar(), "progress")
        self.progress_bar.setObjectName("legacyMigrationProgress")
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        layout.addWidget(self.progress_bar)
        self.status_label = self._label("", wrap=True)
        self.status_label.setObjectName("legacyMigrationStatus")
        layout.addWidget(self.status_label)
        self.result_label = self._label("", "subtitle", wrap=True)
        self.result_label.setObjectName("legacyMigrationResult")
        layout.addWidget(self.result_label)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.stop_button = self._button(tr("停止"), self.controller.cancel)
        self.stop_button.setObjectName("legacyMigrationStop")
        actions.addWidget(self.stop_button)
        actions.addStretch(1)
        self.retry_button = self._button(tr("重试失败和取消项"), self.controller.retry_import)
        self.retry_button.setObjectName("legacyMigrationRetry")
        actions.addWidget(self.retry_button)
        self.copy_button = self._button(tr("确认并复制"), self._copy, primary=True)
        self.copy_button.setObjectName("legacyMigrationCopy")
        actions.addWidget(self.copy_button)
        actions.addWidget(self._button(tr("关闭"), self._close_window))
        layout.addLayout(actions)

    def _build_source(self, layout: QVBoxLayout) -> None:
        """来源读取与扫描分别由用户触发，目标游戏始终可见。"""
        tr = self.ctx.i18n.t
        source = self._card(layout)
        source.addWidget(self._label(tr("1  选择来源与目标"), "heading"))
        self.target_label = self._label("", "subtitle", wrap=True)
        self.target_label.setObjectName("legacyMigrationTarget")
        source.addWidget(self.target_label)
        source.addWidget(self._label(tr("目标为游戏页当前选择的游戏；可先在游戏页创建或切换目标。"), wrap=True))
        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self.path_edit = self._style(QLineEdit(), "input")
        self.path_edit.setObjectName("legacyMigrationSource")
        self.path_edit.setPlaceholderText(tr("旧版安装目录（包含 home 和 resources）"))
        self.path_edit.setMinimumWidth(0)
        self.path_edit.returnPressed.connect(self._read_source)
        self.path_edit.textChanged.connect(self._render)
        path_row.addWidget(self.path_edit, 1)
        self.browse_button = self._button(tr("浏览…"), self._choose_directory)
        path_row.addWidget(self.browse_button)
        self.read_button = self._button(tr("读取环境"), self._read_source)
        path_row.addWidget(self.read_button)
        source.addLayout(path_row)
        environment_row = QHBoxLayout()
        environment_row.setSpacing(8)
        self.environment_combo = self._style(QComboBox(), "input")
        self.environment_combo.setObjectName("legacyMigrationEnvironment")
        self.environment_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.environment_combo.setMinimumContentsLength(16)
        self.environment_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.environment_combo.setPlaceholderText(tr("读取后选择旧版用户环境"))
        self.environment_combo.currentIndexChanged.connect(self._render)
        environment_row.addWidget(self.environment_combo, 1)
        self.scan_button = self._button(tr("扫描预览"), self._scan_source)
        self.scan_button.setObjectName("legacyMigrationScan")
        environment_row.addWidget(self.scan_button)
        source.addLayout(environment_row)

    def _build_preview(self, layout: QVBoxLayout) -> None:
        """预览可导入项、分类与异常，扫描问题有独立且持续可见的入口。"""
        tr = self.ctx.i18n.t
        preview = self._card(layout)
        layout.setStretch(layout.count() - 1, 1)
        title_row = QHBoxLayout()
        title_row.addWidget(self._label(tr("2  检查预览"), "heading"), 1)
        self.filter_combo = self._style(QComboBox(), "input")
        self.filter_combo.addItem(tr("可导入 Mod"), "mods")
        self.filter_combo.addItem(tr("分类"), "categories")
        self.filter_combo.addItem(tr("扫描与复制问题"), "issues")
        self.filter_combo.currentIndexChanged.connect(self._filter_changed)
        title_row.addWidget(self.filter_combo)
        preview.addLayout(title_row)
        self.summary_label = self._label("", "subtitle", wrap=True)
        self.summary_label.setObjectName("legacyMigrationSummary")
        preview.addWidget(self.summary_label)
        self.issues_button = self._button("", self._show_issues)
        self.issues_button.setObjectName("legacyMigrationIssues")
        preview.addWidget(self.issues_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.preview_model = MigrationPreviewModel(self.controller, self)
        self.table = self._style(QTableView(), "table")
        self.table.setObjectName("legacyMigrationPreview")
        self.table.setModel(self.preview_model)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.horizontalHeader().setMinimumSectionSize(64)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(2, 94)
        self.table.setMinimumHeight(160)
        preview.addWidget(self.table, 1)

    def _choose_directory(self) -> None:
        """原生目录对话框返回后再次检查窗口，避免嵌套事件循环越过关闭边界。"""
        path = QFileDialog.getExistingDirectory(
            self, self.ctx.i18n.t("选择旧版安装目录"), self.path_edit.text().strip()
        )
        if path and not self._closed:
            self.path_edit.setText(path)
            self.controller.discover(path)

    def _read_source(self) -> None:
        """读取当前输入路径中的旧版环境。"""
        if not self._closed:
            self.controller.discover(self.path_edit.text())

    def _scan_source(self) -> None:
        """扫描选定环境，向导始终保留当前明确的目标游戏。"""
        environment = self.environment_combo.currentData()
        if not self._closed and environment is not None:
            self.controller.scan_environment(environment)

    def _copy(self) -> None:
        """确认按钮只提交已显示且来源仍匹配的扫描结果。"""
        if not self._closed and self._source_matches():
            self.controller.start_import()

    def _source_matches(self) -> bool:
        """输入目录或环境变化后，旧预览不能继续用于确认复制。"""
        controller = self.controller
        return (
            controller.scan is not None
            and self.path_edit.text().strip() == controller.root
            and self.environment_combo.currentData() == controller.scan.environment
        )

    def _filter_changed(self, _index: int) -> None:
        """切换表格视图时为问题来源分配可伸缩列宽。"""
        mode = self.filter_combo.currentData()
        self.preview_model.set_mode(mode)
        resize = QHeaderView.ResizeMode.Stretch if mode == "issues" else QHeaderView.ResizeMode.Fixed
        self.table.horizontalHeader().setSectionResizeMode(2, resize)
        if mode != "issues":
            self.table.setColumnWidth(2, 94)

    def _show_issues(self) -> None:
        """直接切换到完整诊断列表，不隐藏缺失或无效条目。"""
        self.filter_combo.setCurrentIndex(self.filter_combo.findData("issues"))

    def _render(self, *_args) -> None:
        """从控制器快照更新控件，不查询宿主资源库或重建已有输入。"""
        if self._closed or not hasattr(self, "copy_button"):
            return
        controller = self.controller
        tr = self.ctx.i18n.t
        if self._environments != controller.environments:
            self._environments = controller.environments
            self.environment_combo.blockSignals(True)
            self.environment_combo.clear()
            for environment in self._environments:
                self.environment_combo.addItem(environment.name, environment)
            if len(self._environments) == 1:
                self.environment_combo.setCurrentIndex(0)
            self.environment_combo.blockSignals(False)
        name = controller.target_game_name or controller.target_game_id
        self.target_label.setText(tr("目标游戏：{name}", name=name) if name else tr("尚未选择目标游戏"))
        self.target_label.setToolTip(controller.target_game_id)
        environment = self.environment_combo.currentData()
        self.environment_combo.setToolTip(environment.path if environment is not None else "")
        busy = controller.busy
        self.path_edit.setEnabled(not busy)
        self.browse_button.setEnabled(not busy)
        self.read_button.setEnabled(not busy and bool(self.path_edit.text().strip()))
        self.environment_combo.setEnabled(not busy and bool(controller.environments))
        self.scan_button.setEnabled(
            not busy
            and environment is not None
            and bool(controller.target_game_id)
            and self.path_edit.text().strip() == controller.root
        )
        self.copy_button.setVisible(controller.state != "finished")
        self.copy_button.setEnabled(
            controller.state == "ready" and self._source_matches() and bool(controller.items or controller.categories)
        )
        summary = controller.summary
        self.retry_button.setVisible(controller.state == "finished" and bool(summary.failed or summary.cancelled))
        self.retry_button.setEnabled(not busy and self._source_matches())
        self.stop_button.setVisible(busy)
        self.stop_button.setEnabled(controller.state != "stopping")
        self.progress_bar.setVisible(busy or controller.state == "finished")
        self.progress_bar.setRange(0, 0 if controller.state == "discovering" else 1000)
        self.progress_bar.setValue(round(controller.progress * 1000))
        total_bytes = controller.scan.total_bytes if controller.scan is not None else 0
        self.summary_label.setText(
            tr(
                "可导入 {mods} 项 · 分类 {categories} 项 · 约 {size}",
                mods=summary.total,
                categories=len(controller.categories),
                size=format_size(total_bytes),
            )
        )
        issue_count = len(controller.scan.issues) if controller.scan is not None else 0
        issue_count += len(controller.migration_issues)
        self.issues_button.setVisible(bool(issue_count))
        self.issues_button.setText(tr("查看 {count} 项扫描与复制问题", count=issue_count))
        self.status_label.setText(
            tr("来源已更改，请重新扫描。")
            if controller.scan is not None and not self._source_matches() and not busy
            else controller.progress_message or controller.message
        )
        self.result_label.setVisible(
            controller.state in {"importing", "stopping", "finished"} and controller.scan is not None
        )
        self.result_label.setText(
            tr(
                "成功 {succeeded} · 跳过 {skipped} · 失败 {failed} · 取消 {cancelled}",
                succeeded=summary.succeeded,
                skipped=summary.skipped,
                failed=summary.failed,
                cancelled=summary.cancelled,
            )
        )
        self.preview_model.refresh()

    def _close_window(self) -> None:
        """让宿主对话框关闭流程统一取消会话和释放窗口。"""
        self.window().close()

    def closeEvent(self, event) -> None:
        """直接展示控件时也遵守关闭即停止的约定。"""
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self) -> None:
        """停止控制器并解除所有主题订阅，重复清理保持无副作用。"""
        if self._closed:
            return
        self._closed = True
        self.controller.shutdown()
        self._styles.dispose()
