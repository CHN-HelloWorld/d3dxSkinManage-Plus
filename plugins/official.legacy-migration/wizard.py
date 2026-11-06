"""使用宿主样式绑定构建旧版迁移向导，不维护独立调色盘或后台 Qt 线程。"""

from __future__ import annotations

from PySide6.QtCore import QSize, QSortFilterProxyModel, Qt
from PySide6.QtGui import QImageReader, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStyle,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from src.plugin_sdk import DisposableStore, PluginContext

from .controller import MigrationController
from .preview import MigrationTreeModel, format_size


class MigrationWizard(QWidget):
    """展示来源、完整预览和结果，在显式确认后启动本地复制。"""

    def __init__(self, ctx: PluginContext) -> None:
        """样式与任务分别由宿主绑定和窗口所有的控制器管理。"""
        super().__init__()
        self.ctx = ctx
        self._closed = False
        self._styles = DisposableStore()
        self._environments = ()
        self._seen_scan = None
        self._step = 0
        self._painted_step = -1
        self._step_bindings = []
        self._continue_after_discovery = False
        self._details_open = False
        self._diagnostics_open = False
        self._selected_item = -1
        self._selected_category = None
        self._selected_issue = -1
        self._preview_path = ""
        self._window_size_key = None
        self.setObjectName("legacyMigrationWizard")
        self.setMinimumSize(600, 360)
        self.controller = MigrationController(ctx, self)
        self._build()
        self.controller.changed.connect(self._render)
        self.destroyed.connect(self.controller.shutdown)
        self._render()

    def _available_geometry(self, window):
        """按窗口所在屏幕的工作区限制默认大小，避开任务栏。"""
        parent = window.parentWidget()
        return (parent.screen() if parent is not None else window.screen()).availableGeometry()

    def _preferred_window_size(self, window):
        """采用分类预设确认页的紧凑尺寸，屏幕只限制上界。"""
        available = self._available_geometry(window)
        width, height = (1160, 780) if self._step == 1 or self._details_open else (720, 480)
        return QSize(
            min(width, available.width() - 40),
            min(height, available.height() - 60),
        )

    def sizeHint(self):
        """在宿主第一次显示前提供首选内容尺寸，不依赖内部 resize。"""
        return self._preferred_window_size(self.window()) - QSize(32, 32)

    def configure_window(self, window) -> None:
        """设置实际宿主窗口而非内部控件的默认大小，并居中显示。"""
        self._window_size_key = self._step, self._details_open
        available = self._available_geometry(window)
        window.resize(self._preferred_window_size(window))
        frame = window.frameGeometry()
        frame.moveCenter(available.center())
        window.move(frame.topLeft())

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
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return self._style(label, role)

    def _button(self, text: str, callback, *, primary: bool = False) -> QPushButton:
        """建立统一的主次按钮，并显式关闭对话框默认按钮行为。"""
        button = QPushButton(text)
        button.setAutoDefault(False)
        button.clicked.connect(callback)
        return self._style(button, "primary_button" if primary else "button")

    def _card(self, layout: QVBoxLayout) -> tuple[QFrame, QVBoxLayout]:
        """给内容分组添加与宿主一致的卡片和留白。"""
        frame = self._style(QFrame(), "card")
        content = QVBoxLayout(frame)
        content.setContentsMargins(16, 14, 16, 14)
        content.setSpacing(10)
        layout.addWidget(frame)
        return frame, content

    def _build(self) -> None:
        """固定步骤和操作栏，只显示当前步骤需要的内容。"""
        tr = self.ctx.i18n.t
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)
        steps = QHBoxLayout()
        self.step_labels = []
        for text in (tr("1  选择文件夹"), tr("2  确认内容"), tr("3  迁移结果")):
            label = self._label(text)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.step_labels.append(label)
            steps.addWidget(label, 1)
        outer.addLayout(steps)
        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("legacyMigrationScroll")
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.viewport().setAutoFillBackground(False)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(10)
        self._content_layout = layout
        self.scroll_area.setWidget(content)
        content.setAutoFillBackground(False)
        outer.addWidget(self.scroll_area, 1)
        heading = QHBoxLayout()
        self.page_title = self._label("", "title", wrap=True)
        heading.addWidget(self.page_title, 2)
        self.target_label = self._label("", "subtitle", wrap=True)
        self.target_label.setObjectName("legacyMigrationTarget")
        self.target_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        heading.addWidget(self.target_label, 1)
        layout.addLayout(heading)
        self.page_hint = self._label("", wrap=True)
        layout.addWidget(self.page_hint)
        self.target_hint = self._label(tr("先关闭此窗口，在游戏页添加或选择游戏，再打开迁移。"), wrap=True)
        layout.addWidget(self.target_hint)
        self._build_source(layout)
        self._build_result(layout)
        self._preview_index = layout.count()
        self._build_preview(layout)
        self.safety_label = self._label(
            tr("旧版文件不会更改；已存在的 Mod 不会重复导入。新导入的 Mod 暂不启用。"), wrap=True
        )
        outer.addWidget(self.safety_label)
        self.progress_bar = self._style(QProgressBar(), "progress")
        self.progress_bar.setObjectName("legacyMigrationProgress")
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        layout.addWidget(self.progress_bar)
        self.status_label = self._label("", wrap=True)
        self.status_label.setObjectName("legacyMigrationStatus")
        layout.addWidget(self.status_label)
        self.diagnostic_button = self._button(tr("查看错误详情"), self._toggle_diagnostics)
        layout.addWidget(self.diagnostic_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.diagnostic_label = self._label("", wrap=True)
        self.diagnostic_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.diagnostic_label)
        self._spacer_index = layout.count()
        layout.addStretch(1)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.back_button = self._button(tr("上一步"), self._change_source)
        actions.addWidget(self.back_button)
        self.stop_button = self._button(tr("停止迁移"), self.controller.cancel)
        self.stop_button.setObjectName("legacyMigrationStop")
        actions.addWidget(self.stop_button)
        actions.addStretch(1)
        self.retry_button = self._button(tr("重试未完成"), self.controller.retry_import, primary=True)
        self.retry_button.setObjectName("legacyMigrationRetry")
        actions.addWidget(self.retry_button)
        self.report_button = self._button(tr("打开报告"), self.controller.open_report)
        self.report_button.setObjectName("legacyMigrationReport")
        actions.addWidget(self.report_button)
        self.scan_button = self._button(tr("检查迁移内容"), self._scan_source, primary=True)
        self.scan_button.setObjectName("legacyMigrationScan")
        actions.addWidget(self.scan_button)
        self.copy_button = self._button(tr("开始迁移"), self._copy, primary=True)
        self.copy_button.setObjectName("legacyMigrationCopy")
        actions.addWidget(self.copy_button)
        self.close_button = self._button(tr("关闭"), self._close_window)
        actions.addWidget(self.close_button)
        outer.addLayout(actions)

    def _build_source(self, layout: QVBoxLayout) -> None:
        """选目录后自动识别，多份数据必须由用户明确选择。"""
        tr = self.ctx.i18n.t
        self.source_card, source = self._card(layout)
        source.addWidget(self._label(tr("旧版程序所在文件夹"), "heading"))
        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self.path_edit = self._style(QLineEdit(), "input")
        self.path_edit.setObjectName("legacyMigrationSource")
        self.path_edit.setPlaceholderText(tr("选择文件夹，或粘贴文件夹路径"))
        self.path_edit.setMinimumWidth(0)
        self.path_edit.returnPressed.connect(self._read_source)
        self.path_edit.textChanged.connect(self._render)
        path_row.addWidget(self.path_edit, 1)
        self.browse_button = self._button(tr("选择文件夹…"), self._choose_directory)
        path_row.addWidget(self.browse_button)
        source.addLayout(path_row)
        source.addWidget(self._label(tr("选择旧版 d3dxSkinManage.exe 所在的文件夹，不是 Mod 文件夹。"), wrap=True))
        self.environment_label = self._label(tr("选择要迁移的旧版游戏数据"), "heading", wrap=True)
        source.addWidget(self.environment_label)
        self.environment_combo = self._style(QComboBox(), "input")
        self.environment_combo.setObjectName("legacyMigrationEnvironment")
        self.environment_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.environment_combo.setMinimumContentsLength(16)
        self.environment_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.environment_combo.setPlaceholderText(tr("请选择一份旧版游戏数据"))
        self.environment_combo.currentIndexChanged.connect(self._render)
        source.addWidget(self.environment_combo)

    def _build_result(self, layout: QVBoxLayout) -> None:
        """结果先展示完成情况，再按需打开条目与诊断。"""
        self.result_card, result = self._card(layout)
        self.result_label = self._label("", "subtitle", wrap=True)
        self.result_label.setObjectName("legacyMigrationResult")
        result.addWidget(self.result_label)
        self.result_help = self._label("", wrap=True)
        result.addWidget(self.result_help)
        self.details_button = self._button(self.ctx.i18n.t("查看迁移详情"), self._toggle_details)
        result.addWidget(self.details_button, 0, Qt.AlignmentFlag.AlignLeft)

    def _build_preview(self, layout: QVBoxLayout) -> None:
        """分类树、问题和详情同时展示，不用内容类型下拉框隐藏任何区域。"""
        tr = self.ctx.i18n.t
        self.preview_card, preview = self._card(layout)
        preview.setSpacing(8)
        self.stats_layout = QGridLayout()
        self.stats_layout.setContentsMargins(0, 0, 0, 0)
        self.stats_layout.setSpacing(10)
        self.stat_cards = []
        self.stat_values = []
        self.stat_labels = []
        self._stats_columns = 0
        for text in (tr("可迁移 Mod"), tr("可导入对象"), tr("可导入分类"), tr("无法迁移项")):
            card = self._style(QFrame(), "card")
            content = QVBoxLayout(card)
            content.setContentsMargins(12, 10, 12, 10)
            content.setSpacing(2)
            value = self._label("0", "title")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label = self._label(text, wrap=True)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            content.addWidget(value)
            content.addWidget(label)
            self.stat_cards.append(card)
            self.stat_values.append(value)
            self.stat_labels.append(label)
        self.stat_labels[1].setToolTip(tr("角色或对象节点，不包含普通分类和 Mod。"))
        self.stat_labels[2].setToolTip(tr("普通分类节点，不重复计入角色或对象。"))
        preview.addLayout(self.stats_layout)
        self.summary_label = self._label("", "subtitle")
        self.summary_label.setObjectName("legacyMigrationSummary")
        self.summary_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.preview_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.preview_splitter.setChildrenCollapsible(False)
        self.preview_splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.preview_splitter.setObjectName("legacyMigrationPanels")
        tree_panel = QWidget()
        tree_layout = QVBoxLayout(tree_panel)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(8)
        tree_heading = QHBoxLayout()
        tree_heading.addWidget(self._label(tr("迁移后的分类与 Mod"), "heading"), 1)
        tree_heading.addWidget(self.summary_label)
        tree_layout.addLayout(tree_heading)
        search_row = QHBoxLayout()
        self.search_edit = self._style(QLineEdit(), "input")
        self.search_edit.setPlaceholderText(tr("搜索名称或分类，只影响显示"))
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMinimumWidth(0)
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(self._button(tr("展开"), lambda: self.tree.expandAll()))
        search_row.addWidget(self._button(tr("折叠"), lambda: self.tree.collapseAll()))
        tree_layout.addLayout(search_row)
        self.tree_model = MigrationTreeModel(
            self.controller, self, folder_icon=self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        )
        self.tree_proxy = QSortFilterProxyModel(self)
        self.tree_proxy.setDynamicSortFilter(False)
        self.tree_proxy.setSourceModel(self.tree_model)
        self.tree_proxy.setRecursiveFilteringEnabled(True)
        self.tree_proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.tree_proxy.setFilterRole(MigrationTreeModel.SearchRole)
        self.tree_proxy.setFilterKeyColumn(0)
        self.tree = self._style(QTreeView(), "table")
        self.tree.setObjectName("legacyMigrationTree")
        self.tree.setModel(self.tree_proxy)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setUniformRowHeights(True)
        self.tree.setMinimumHeight(180)
        self.tree.setMinimumWidth(0)
        self.tree.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.tree.header().setMinimumSectionSize(64)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(1, 104)
        self.tree.setColumnWidth(2, 112)
        self.tree.setColumnWidth(3, 144)
        tree_layout.addWidget(self.tree, 1)
        self.tree_empty = self._label("", wrap=True)
        self.preview_splitter.addWidget(tree_panel)
        side_panel = QWidget()
        side_panel.setMinimumWidth(0)
        side_panel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        side_layout = QVBoxLayout(side_panel)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(10)
        self.detail_card, detail_layout = self._card(side_layout)
        detail_layout.addWidget(self._label(tr("条目详情"), "heading"))
        self.detail_scroll = QScrollArea()
        self.detail_scroll.setObjectName("legacyMigrationDetailScroll")
        self.detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.detail_scroll.setMinimumHeight(140)
        self.detail_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.detail_scroll.viewport().setAutoFillBackground(False)
        self.detail_scroll.viewport().setObjectName("legacyMigrationDetailViewport")
        detail_content = QWidget()
        detail_content.setObjectName("legacyMigrationDetailContent")
        detail_content.setAutoFillBackground(False)
        self.detail_scroll.setStyleSheet(
            "QScrollArea#legacyMigrationDetailScroll, QWidget#legacyMigrationDetailContent, "
            "QWidget#legacyMigrationDetailViewport { background: transparent; }"
        )
        details = QVBoxLayout(detail_content)
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(10)
        self.detail_scroll.setWidget(detail_content)
        detail_layout.addWidget(self.detail_scroll, 1)
        self.detail_title = self._label("", "subtitle", wrap=True)
        details.addWidget(self.detail_title)
        self.detail_preview = self._label("", wrap=True)
        self.detail_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_preview.setFixedHeight(140)
        details.addWidget(self.detail_preview)
        self.detail_text = self._label("", wrap=True)
        self.detail_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details.addWidget(self.detail_text)
        self.source_label = self._label("", wrap=True)
        details.addWidget(self.source_label)
        self.source_combo = self._style(QComboBox(), "input")
        self.source_combo.setObjectName("legacyMigrationSourceChoice")
        self.source_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.source_combo.setMinimumContentsLength(20)
        details.addWidget(self.source_combo)
        details.removeWidget(self.source_label)
        details.removeWidget(self.source_combo)
        details.insertWidget(1, self.source_label)
        details.insertWidget(2, self.source_combo)
        details.addStretch(1)
        self.detail_card.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        side_layout.setStretch(1, 1)
        self.preview_splitter.addWidget(side_panel)
        self.preview_splitter.setStretchFactor(0, 1)
        self.preview_splitter.setStretchFactor(1, 0)
        self.preview_splitter.setSizes([700, 330])
        preview.addWidget(self.preview_splitter, 1)
        preview.addWidget(self.tree_empty)
        self.tree.selectionModel().currentChanged.connect(self._tree_selected)
        self.source_combo.currentIndexChanged.connect(self._source_changed)
        self.search_edit.textChanged.connect(self._search_changed)

    def _tree_selected(self, index, *_args) -> None:
        """代理行映射回真实 Mod 身份，分类选择不能误改某个 Mod 的来源。"""
        source_index = self.tree_proxy.mapToSource(index)
        node = source_index.internalPointer() if source_index.isValid() else None
        self._selected_item = node.item_index if node is not None else -1
        self._selected_category = node.category if node is not None else None
        self._selected_issue = node.issue_index if node is not None else -1
        self._render_detail()

    def _search_changed(self, text) -> None:
        """搜索保留祖先分类，只影响目录树显示。"""
        self.tree_proxy.setFilterFixedString(text.strip())
        self.tree.expandAll()
        self.tree_empty.setText(self.ctx.i18n.t("没有匹配的内容，清空搜索可查看全部。"))
        self.tree_empty.setVisible(self.tree_proxy.rowCount() == 0)

    def _render_detail(self) -> None:
        """详情和文件版本都由当前真实选择决定，不用可见行号提交修改。"""
        if self._closed or self.controller.busy:
            return
        tr = self.ctx.i18n.t
        preview_path = ""
        if 0 <= self._selected_issue < len(self.tree_model.issues):
            issue = self.tree_model.issues[self._selected_issue]
            self.detail_title.setText(issue.name or tr("问题详情"))
            message = tr(issue.message, name=issue.name, source=issue.source)
            self.detail_text.setText(message)
            self.detail_text.setToolTip("\n".join(value for value in (message, issue.source) if value))
        elif 0 <= self._selected_item < len(self.controller.items):
            item = self.controller.items[self._selected_item]
            source = item.source
            self.detail_title.setText(source.name)
            lines = [" / ".join(self.controller.destination_path(source))]
            if source.author:
                lines.append(tr("作者：{name}", name=source.author))
            if source.description:
                lines.append(source.description)
            if source.tags:
                lines.append(tr("标签：{tags}", tags=" · ".join(source.tags)))
            if source.preview_paths:
                lines.append(tr("包含 {count} 张预览图", count=len(source.preview_paths)))
                preview_path = source.preview_paths[0]
            lines.extend(value for value in (item.error, *item.warnings) if value)
            self.detail_text.setText("\n".join(lines))
            self.detail_text.setToolTip(source.source_path)
        elif self._selected_category is not None:
            category = self._selected_category
            self.detail_title.setText(category.path[-1])
            self.detail_text.setText(
                " / ".join(category.path)
                + "\n"
                + (tr("角色 / 对象") if category.node_type == "character" else tr("分类"))
            )
            self.detail_text.setToolTip(category.thumbnail_path)
            preview_path = category.thumbnail_path
        else:
            self.detail_title.setText(tr("选中条目查看详情"))
            self.detail_text.setText(tr("点击分类或 Mod 可查看信息；有旧版修改时，可在这里选择要保留的文件。"))
            self.detail_text.setToolTip("")
        self._show_preview_image(preview_path)
        self._render_source_choice()
        self.detail_scroll.verticalScrollBar().setValue(0)

    def _show_preview_image(self, path: str) -> None:
        """只解码选中条目的安全尺寸预览，限制图片像素与显示尺寸。"""
        self.detail_preview.setVisible(bool(path))
        if path == self._preview_path:
            return
        self._preview_path = path
        self.detail_preview.clear()
        if not path:
            return
        reader = QImageReader(path)
        reader.setDecideFormatFromContent(True)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and size.width() * size.height() <= 40_000_000:
            reader.setScaledSize(size.scaled(300, 140, Qt.AspectRatioMode.KeepAspectRatio))
            image = reader.read()
            if not image.isNull():
                self.detail_preview.setPixmap(
                    QPixmap.fromImage(image).scaled(
                        300, 140, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                    )
                )
                return
        self.detail_preview.setText(self.ctx.i18n.t("预览图无法显示，迁移内容不受影响。"))

    def _render_source_choice(self, *_args) -> None:
        """只有选中可恢复或有修改的 Mod 时显示版本选择，复制期间冻结。"""
        if self._closed:
            return
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        tr = self.ctx.i18n.t
        visible = False
        if self._step == 1 and 0 <= self._selected_item < len(self.controller.items):
            source = self.controller.items[self._selected_item].source
            visible = bool(source.cache_path and (source.cache_changed or not source.archive_path))
            if source.archive_path:
                self.source_combo.addItem(tr("原始文件（推荐）"), False)
            if source.cache_path:
                self.source_combo.addItem(tr("修改后的文件"), True)
            using_cache = bool(source.cache_path and source.source_path == source.cache_path)
            self.source_combo.setCurrentIndex(self.source_combo.findData(using_cache))
            self.source_combo.setEnabled(self.controller.state == "ready" and self.source_combo.count() > 1)
            self.source_label.setText(
                tr("这份 Mod 的文件有修改。要保留旧版中的修改，请选择“修改后的文件”。")
                if source.archive_path
                else tr("原始文件已丢失，将使用旧版解压后的文件。文件可能不完整，迁移后请检查。")
            )
            self.source_label.setToolTip(source.source_path)
        self.source_label.setVisible(visible)
        self.source_combo.setVisible(visible)
        self.source_combo.blockSignals(False)

    def _source_changed(self, *_args) -> None:
        """按真实 Mod 下标提交版本选择，搜索结果的可见行号不参与写入。"""
        if not self._closed and self.source_combo.currentIndex() >= 0:
            self.controller.select_source(self._selected_item, bool(self.source_combo.currentData()))

    def _choose_directory(self) -> None:
        """原生目录对话框返回后再次检查窗口，避免嵌套事件循环越过关闭边界。"""
        path = QFileDialog.getExistingDirectory(
            self, self.ctx.i18n.t("选择旧版安装目录"), self.path_edit.text().strip()
        )
        if path and not self._closed:
            self.path_edit.setText(path)
            self._read_source()

    def _read_source(self) -> None:
        """读取当前输入路径中的旧版环境。"""
        if not self._closed:
            self._continue_after_discovery = False
            self._diagnostics_open = False
            self.controller.discover(self.path_edit.text())

    def _scan_source(self) -> None:
        """扫描选定环境，向导始终保留当前明确的目标游戏。"""
        if self._closed or self.controller.busy:
            return
        if self.path_edit.text().strip() != self.controller.root or not self.controller.environments:
            self._continue_after_discovery = True
            self._diagnostics_open = False
            self.controller.discover(self.path_edit.text())
            return
        environment = self.environment_combo.currentData()
        if environment is not None:
            self.controller.scan_environment(environment)

    def _copy(self) -> None:
        """确认按钮只提交已显示且来源仍匹配的扫描结果。"""
        if not self._closed and self._step == 1 and self._source_matches():
            self.controller.start_import()

    def _source_matches(self) -> bool:
        """输入目录或环境变化后，旧预览不能继续用于确认复制。"""
        controller = self.controller
        return (
            controller.scan is not None
            and self.path_edit.text().strip() == controller.root
            and self.environment_combo.currentData() == controller.scan.environment
        )

    def _responsive_preview(self) -> None:
        """宽窗口左右呈现，小窗口上下排列，任何区域都不折叠隐藏。"""
        columns = 4 if self.width() >= 900 else 2
        if columns != self._stats_columns:
            for card in self.stat_cards:
                self.stats_layout.removeWidget(card)
            for column in range(4):
                self.stats_layout.setColumnStretch(column, 1 if column < columns else 0)
            for index, card in enumerate(self.stat_cards):
                self.stats_layout.addWidget(card, index // columns, index % columns)
            self._stats_columns = columns
        orientation = Qt.Orientation.Horizontal if self.width() >= 900 else Qt.Orientation.Vertical
        if self.preview_splitter.orientation() != orientation:
            self.preview_splitter.setOrientation(orientation)
            self.preview_splitter.setSizes([700, 330] if orientation == Qt.Orientation.Horizontal else [260, 320])
        self.detail_card.setMinimumHeight(self.detail_card.minimumSizeHint().height())
        self.preview_splitter.setMinimumHeight(self.preview_splitter.minimumSizeHint().height())

    def resizeEvent(self, event) -> None:
        """调整布局方向，保留树选择、搜索及迁移状态。"""
        super().resizeEvent(event)
        if hasattr(self, "preview_splitter"):
            self._responsive_preview()

    def _change_source(self) -> None:
        """返回来源步骤，不丢弃仍匹配的预览，也不自动复制。"""
        if not self.controller.busy:
            self._step = 0
            self._details_open = False
            self._render()

    def _toggle_details(self) -> None:
        """结果详情使用同一延迟模型，避免复制大量条目。"""
        self._details_open = not self._details_open
        self._render()

    def _toggle_diagnostics(self) -> None:
        """完整技术诊断保留供排查，默认提示只说明下一步。"""
        self._diagnostics_open = not self._diagnostics_open
        self._render()

    def _render_step(self) -> None:
        """新预览进入确认页，执行进入结果页，返回操作保留匹配的预览。"""
        controller = self.controller
        tr = self.ctx.i18n.t
        if controller.scan is not self._seen_scan:
            self._seen_scan = controller.scan
            self._step = 1 if controller.scan is not None else 0
            self._details_open = False
            self._selected_item = -1
            self._selected_category = None
            self._selected_issue = -1
            self.search_edit.clear()
            self.tree.expandAll()
            if not controller.busy:
                self._render_detail()
        if controller.state in {"importing", "stopping", "finished"} and controller.scan is not None:
            if self._step != 0 or controller.busy:
                if self._step != 2:
                    self._details_open = False
                self._step = 2
        elif controller.scan is not None and not self._source_matches() and not controller.busy:
            self._step = 0
        if self._painted_step != self._step:
            for binding in self._step_bindings:
                binding.dispose()
            self._step_bindings = [
                self._styles.add(self.ctx.ui.apply_style(label, "heading" if index == self._step else "muted"))
                for index, label in enumerate(self.step_labels)
            ]
            self._painted_step = self._step
            self.scroll_area.verticalScrollBar().setValue(0)
        result_title = tr("正在迁移…")
        if controller.state == "stopping":
            result_title = tr("正在停止…")
        elif not controller.busy:
            summary = controller.summary
            excluded = any(issue.code in {"missing", "invalid"} for issue in controller.issues)
            attention = bool(controller.issues) or any(item.error or item.warnings for item in controller.items)
            if controller.message == tr("复制已停止，已完成的项目会保留。"):
                result_title = tr("迁移已停止")
            elif summary.failed or summary.cancelled:
                result_title = tr("部分内容未完成")
            elif excluded:
                result_title = tr("部分旧版内容未迁移")
            elif attention:
                result_title = tr("迁移完成，请检查提示")
            else:
                result_title = tr("迁移完成")
        titles = (tr("选择旧版文件夹"), tr("确认要迁移的内容"), result_title)
        hints = (
            tr("把旧版 Mod 和整理信息带到当前游戏。"),
            tr("检查列表后点击“开始迁移”。现在还没有导入任何文件。"),
            tr("请保持窗口打开。需要中断时可以停止，已完成的内容会保留。")
            if controller.busy
            else tr("已导入的内容保存在当前游戏中，旧版文件仍保留。"),
        )
        self.page_title.setText(titles[self._step])
        self.page_hint.setText(hints[self._step])
        self.source_card.setVisible(self._step == 0)
        self.result_card.setVisible(self._step == 2)
        self.preview_card.setVisible(self._step == 1 or (self._step == 2 and self._details_open))
        self._content_layout.setStretch(self._preview_index, 1 if self._step == 1 else 0)
        self._content_layout.setStretch(self._spacer_index, 0 if self._step == 1 else 1)

    def _render_status(self) -> None:
        """提示展示影响和下一步，技术错误只在用户展开后显示。"""
        controller = self.controller
        tr = self.ctx.i18n.t
        message = controller.progress_message or controller.message
        if controller.state == "error":
            message = tr("读取失败。请检查所选文件夹及文件访问权限，然后重新尝试。")
        elif controller.scan is not None and not self._source_matches() and not controller.busy:
            message = tr("来源已更改。点击“检查迁移内容”更新列表后再迁移。")
        elif controller.state == "idle":
            message = tr("选择文件夹后会自动识别旧版数据。")
        elif controller.state == "selecting":
            if self.environment_combo.currentData() is not None:
                message = tr("已找到旧版数据。点击“检查迁移内容”继续。")
            elif controller.environments:
                message = tr("找到多份旧版数据，请选择本次要迁移的一份。")
            else:
                message = tr("没有找到旧版数据。请选择旧版程序所在的文件夹，不是 Mod 文件夹。")
        elif controller.state == "ready" and self._step == 1:
            if not (controller.items or controller.categories):
                message = tr("没有可迁移的内容。可以返回上一步，检查文件夹或选择其他游戏数据。")
            else:
                message = ""
        self.status_label.setText(message)
        self.status_label.setVisible(bool(message) and self._step != 2)
        self.diagnostic_button.setVisible(controller.state == "error")
        self.diagnostic_label.setText(controller.message if controller.state == "error" else "")
        self.diagnostic_label.setVisible(controller.state == "error" and self._diagnostics_open)

    def _render(self, *_args) -> None:
        """从控制器快照更新控件，不查询宿主资源库或重建已有输入。"""
        if self._closed or not hasattr(self, "copy_button"):
            return
        self.tree_model.refresh()
        controller = self.controller
        tr = self.ctx.i18n.t
        if self._environments != controller.environments:
            self._environments = controller.environments
            self.environment_combo.blockSignals(True)
            self.environment_combo.clear()
            for environment in self._environments:
                self.environment_combo.addItem(environment.name, environment)
            self.environment_combo.setCurrentIndex(0 if len(self._environments) == 1 else -1)
            self.environment_combo.blockSignals(False)
        self._render_step()
        name = controller.target_game_name or controller.target_game_id
        self.target_label.setText(tr("导入到：{name}", name=name) if name else tr("尚未选择游戏"))
        self.target_label.setToolTip(tr("要更换游戏，请关闭此窗口并在游戏页切换。"))
        self.target_hint.setVisible(not bool(name))
        environment = self.environment_combo.currentData()
        self.environment_combo.setToolTip(environment.path if environment is not None else "")
        busy = controller.busy
        self.path_edit.setEnabled(not busy)
        self.browse_button.setEnabled(not busy)
        self.environment_label.setVisible(bool(controller.environments))
        self.environment_combo.setVisible(bool(controller.environments))
        self.environment_combo.setEnabled(not busy and bool(controller.environments))
        known_root = self.path_edit.text().strip() == controller.root and bool(controller.environments)
        self.scan_button.setVisible(self._step == 0 and not busy)
        self.scan_button.setEnabled(
            not busy
            and bool(controller.target_game_id)
            and bool(self.path_edit.text().strip())
            and (not known_root or environment is not None)
        )
        self.copy_button.setVisible(self._step == 1)
        self.copy_button.setEnabled(
            controller.state == "ready" and self._source_matches() and bool(controller.items or controller.categories)
        )
        summary = controller.summary
        unfinished = summary.failed + summary.cancelled
        self.retry_button.setVisible(self._step == 2 and controller.state == "finished" and bool(unfinished))
        self.retry_button.setEnabled(not busy and self._source_matches())
        self.report_button.setVisible(self._step == 2 and controller.state == "finished")
        self.report_button.setEnabled(not busy)
        self.report_button.setToolTip(controller.report_path)
        self.back_button.setVisible(self._step != 0 and not busy)
        self.back_button.setText(tr("重新选择") if self._step == 2 else tr("上一步"))
        self.stop_button.setVisible(busy)
        self.stop_button.setText(tr("停止迁移") if self._step == 2 else tr("停止检查"))
        self.stop_button.setEnabled(controller.state != "stopping")
        self.close_button.setText(tr("关闭并停止") if busy else tr("完成") if self._step == 2 else tr("关闭"))
        self.progress_bar.setVisible(busy)
        self.progress_bar.setRange(0, 0 if controller.state == "discovering" else 1000)
        self.progress_bar.setValue(round(controller.progress * 1000))
        total_bytes = self.tree_model.total_bytes
        for label, count in zip(
            self.stat_values,
            (
                summary.total,
                self.tree_model.object_count,
                self.tree_model.category_count,
                self.tree_model.excluded_count,
            ),
            strict=True,
        ):
            label.setText(str(count))
        self.summary_label.setText(tr("预计迁移文件：约 {size}", size=format_size(total_bytes)))
        issue_count = len(controller.scan.issues) if controller.scan is not None else 0
        issue_count += len(controller.migration_issues)
        self.tree_empty.setText(
            tr("没有匹配的内容，清空搜索可查看全部。") if self.search_edit.text().strip() else tr("没有可迁移的内容。")
        )
        self.tree_empty.setVisible(self.tree_proxy.rowCount() == 0)
        self.safety_label.setVisible(self._step != 2)
        self._render_status()
        self.result_label.setText(
            tr(
                "已导入 {succeeded} · 已存在 {skipped} · 未完成 {unfinished}",
                succeeded=summary.succeeded,
                skipped=summary.skipped,
                unfinished=unfinished,
            )
        )
        if busy:
            help_text = controller.progress_message or controller.message
        elif controller.message == tr("复制已停止，已完成的项目会保留。"):
            help_text = tr("迁移已停止，已完成的内容会保留。仍有分类或 Mod 未处理，可重新检查后继续迁移。")
        elif unfinished:
            help_text = tr("部分内容未完成。可重试未完成项；缺少文件等问题需修复后重新检查。")
        elif issue_count or any(item.error or item.warnings for item in controller.items):
            help_text = tr("本次可迁移内容已处理。仍有注意事项，请查看详情；未迁移条目不会自动补导入。")
        else:
            help_text = tr("本次内容已处理完成。到游戏的 Mod 列表中检查并启用需要的 Mod。")
        self.result_help.setText(help_text)
        self.details_button.setVisible(not busy)
        self.details_button.setText(tr("收起迁移详情") if self._details_open else tr("查看迁移详情"))
        if not busy:
            self._render_detail()
        self._responsive_preview()
        if self._window_size_key is not None and self._window_size_key != (self._step, self._details_open):
            self.configure_window(self.window())
        if self._continue_after_discovery and controller.state != "discovering":
            self._continue_after_discovery = False
            if controller.state == "selecting" and environment is not None and controller.target_game_id:
                self.controller.scan_environment(environment)

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
