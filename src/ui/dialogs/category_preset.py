"""使用可取消服务操作完成分类预设流程。"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from threading import Event

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from src.ui import icons, theme
from src.ui.acrylic import apply_dialog_acrylic
from src.ui.dialog_chrome import center_dialog
from src.ui.dialogs.category_preset_style import stylesheet
from src.ui.dialogs.category_preset_widgets import (
    FILE_FILTER,
    PresetInspector,
    PresetTree,
    button,
    label,
    panel,
    preset_filename,
    preset_name_error,
    stat_strip,
)
from src.ui.worker_lifecycle import WorkerThread
from src.utils.i18n import t as tr
from src.utils.logger import logger


def _discard_plans(service, pending):
    """对话框串行调度任务，清理成功前保留所有权。"""
    while pending:
        service.discard(pending[0])
        del pending[0]


class _PresetTask(WorkerThread):
    """串行执行可取消的预设服务调用，将结果、取消状态和异常留给界面线程处理。"""

    progress = Signal(str, int)

    def __init__(self, work, parent):
        """保存接受 cancelled 与 progress 关键字的工作函数，并由父窗口持有线程。"""
        super().__init__(parent)
        self.work = work
        self.cancel_event = Event()
        self.result = None
        self.error = None
        self.was_cancelled = False

    def run(self):
        """在后台注入取消检查和进度信号；区分服务主动取消与一般异常，异常记录日志供界面展示。"""
        from src.core.category_preset import CategoryPresetCancelled

        try:
            self.result = self.work(cancelled=self.cancel_event.is_set, progress=self.progress.emit)
        except CategoryPresetCancelled:
            self.was_cancelled = True
        except Exception as error:
            self.error = error
            logger.exception("分类预设操作失败")

    def request_cancel(self):
        """设置线程安全的取消标记，由服务在允许取消的检查点响应。"""
        self.cancel_event.set()


class CategoryPresetDialog(QDialog):
    """管理分类预设的预览、导入、导出及暂存资源，关闭前串行完成取消和清理。"""

    import_completed = Signal(str)
    view_categories_requested = Signal(str)
    games_requested = Signal()

    def __init__(self, manager, game_id, parent=None, *, start_import=False, start_export=False):
        """固定目标游戏并读取分类数量，按入口选择页面；窗口持有计划、暂存清理队列和后台任务。"""
        super().__init__(parent)
        self.setObjectName("categoryPresetDialog")
        self.setModal(True)
        self.setAcceptDrops(True)
        self._manager = manager
        self.game_id = game_id
        self._service_instance = None
        self._worker = None
        self._task_kind = ""
        self._task_success = None
        self._close_requested = False
        self._committed = False
        self._import_plan = None
        self._export_plan = None
        self._pending_discards = []
        self._source = ""
        self._destination = ""
        self._include_images = True
        self._omitted_images = set()
        self._page = None
        self._image_waiters = []
        self._mode = ""
        self._theme = theme.current_theme()
        self._game = manager._game_service.get_game(game_id) if game_id else None
        self._game_name = self._game.name if self._game else ""
        self._export_name = tr("{game_name}-分类预设", game_name=self._game_name)
        self._root = manager._index.paths.roots.get(game_id) if self._game else None
        self._category_count = 0
        if self._game:
            self._category_count = (
                manager._index._db().execute("SELECT COUNT(*) FROM nodes WHERE game_id=?", (game_id,)).fetchone()[0]
            )
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        self._outer.setSpacing(0)
        self.setStyleSheet(stylesheet())
        if not self._game:
            self._show_no_game()
        elif start_import:
            self._show_source()
        elif start_export and self._category_count:
            self._prepare_export()
        else:
            self._show_manage()
        # 分类树和检查区使用稳定底色，外壳保留适度透明并安装统一边框。
        apply_dialog_acrylic(self, tint_alpha=0.86)
        self._center_on_parent()

    @property
    def busy(self):
        """返回是否仍持有未结算的后台任务，包括已结束但尚未处理完成信号的线程。"""
        return self._worker is not None

    @property
    def _service(self):
        """首次使用时为当前管理器创建预设服务，并在窗口生命周期内复用。"""
        if self._service_instance is None:
            from src.core.category_preset_service import CategoryPresetService

            self._service_instance = CategoryPresetService(self._manager)
        return self._service_instance

    def _new_page(self, title, mode, *, size=(720, 480), step=None):
        """取消旧详情页的图片读取并延迟销毁页面，构建受屏幕尺寸约束的新页，返回内容布局。"""
        if self._page is not None:
            for inspector in self._page.findChildren(PresetInspector):
                self._image_waiters.extend(inspector.stop_loading())
            self._outer.removeWidget(self._page)
            self._page.hide()
            self._page.deleteLater()
        self._mode = mode
        self.setWindowTitle(title)
        page = QWidget()
        self._page = page
        self._outer.addWidget(page)
        self._page_layout = QVBoxLayout(page)
        self._page_layout.setContentsMargins(0, 0, 0, 0)
        self._page_layout.setSpacing(0)
        header = QHBoxLayout()
        header.setContentsMargins(24, 20, 24, 18)
        symbol = label()
        icon_name = (
            "actions/export" if mode.startswith("export") else "actions/import" if step is not None else "meta/category"
        )
        icons.apply_pixmap(symbol, icon_name, 26, "accent")
        header.addWidget(symbol)
        header.addSpacing(8)
        heading = label(title, role="title")
        heading.setMinimumWidth(0)
        header.addWidget(heading, 1)
        self._page_layout.addLayout(header)
        if step is not None:
            steps = QHBoxLayout()
            steps.setContentsMargins(26, 0, 26, 15)
            for index, text in enumerate((tr("选择来源"), tr("整树预览"), tr("完成"))):
                if index:
                    line = QFrame()
                    line.setFrameShape(QFrame.Shape.HLine)
                    line.setStyleSheet(f"color: {theme.color('stroke.control')};")
                    steps.addWidget(line, 1)
                if index < step:
                    done = label()
                    icons.apply_pixmap(done, "mod-updates/check-circle", 14, "success")
                    steps.addWidget(done)
                item = label(text if index < step else f"{index + 1}  {text}")
                token = "accent" if index == step else "success" if index < step else "text.tertiary"
                item.setStyleSheet(f"color: {theme.color(token)}; font-weight: {'600' if index == step else '400'};")
                steps.addWidget(item)
            self._page_layout.addLayout(steps)
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(24, 0, 24, 20)
        self._content_layout.setSpacing(14)
        self._page_layout.addWidget(self._content, 1)
        parent = self.parentWidget()
        screen = parent.window().screen() if parent is not None else self.screen()
        available = screen.availableGeometry().size() if screen else QSize(1280, 900)
        width, height = min(size[0], available.width() - 40), min(size[1], available.height() - 60)
        self.setMinimumSize(min(700, width), min(430, height))
        self.resize(width, height)
        # 立即显示新页面控件，避免快速完成任务仍等待后续显示事件。
        if self.isVisible():
            page.show()
        return self._content_layout

    def _footer(self, copy="", actions=()):
        """为当前页添加说明与操作按钮，布局完成后重新居中窗口。"""
        footer = QFrame()
        footer.setProperty("presetRole", "footer")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(24, 14, 24, 14)
        layout.setSpacing(8)
        self._footer_copy = label(copy, role="muted", wrap=True)
        self._footer_copy.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._footer_copy, 1)
        for action in actions:
            layout.addWidget(action)
        self._page_layout.addWidget(footer)
        self._center_on_parent()

    def _center_on_parent(self):
        """先落实内容布局，再将对话框居中到父窗口，适应页面切换造成的尺寸变化。"""
        # 文件选择或导出准备改变内容尺寸，先完成布局再对齐应用窗口。
        self._page_layout.activate()
        self._outer.activate()
        center_dialog(self)

    def _context(self):
        """创建显示目标游戏及完整分类目录数量的上下文面板。"""
        frame = panel()
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        icon = label()
        icons.apply_pixmap(icon, "actions/game", 28, "accent")
        layout.addWidget(icon)
        text = QVBoxLayout()
        text.addWidget(label(self._game_name, role="strong"))
        text.addWidget(label(tr("{category_count} 个分类目录", category_count=self._category_count), role="muted"))
        layout.addLayout(text, 1)
        layout.addWidget(label(tr("完整分类树"), role="muted"))
        return frame

    def _notice(self, text, *, tone="info", action=None):
        """创建按提示等级着色的可复制说明面板，可附带一个操作控件。"""
        frame = QFrame()
        frame.setProperty("presetRole", "notice")
        frame.setProperty("presetTone", tone)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        symbol = label()
        token = tone if tone in {"warning", "danger", "success"} else "accent"
        icons.apply_pixmap(symbol, "status/warning" if tone in {"warning", "danger"} else "status/info", 18, token)
        layout.addWidget(symbol, 0, Qt.AlignmentFlag.AlignTop)
        message = label(text, wrap=True)
        message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(message, 1)
        if action is not None:
            layout.addWidget(action)
        return frame

    def _show_manage(self):
        """展示当前游戏的导入和导出入口，没有分类目录时禁用导出。"""
        layout = self._new_page(tr("游戏分类预设"), "manage", size=(760, 420))
        layout.addWidget(self._context())
        operations = QHBoxLayout()
        operations.setSpacing(14)
        for title, description, icon_name, callback, enabled in (
            (tr("导入分类预设"), tr("补充分类和目录图片。"), "actions/import", self._show_source, True),
            (
                tr("导出分类预设"),
                tr("分享完整分类树及目录图片。") if self._category_count else tr("暂无可导出的分类。"),
                "actions/export",
                self._prepare_export,
                bool(self._category_count),
            ),
        ):
            card = button("", callback)
            card.setProperty("presetRole", "operation")
            card.setMinimumHeight(138)
            card.setAccessibleName(title)
            card.setAccessibleDescription(description)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(20, 20, 20, 20)
            card_layout.setSpacing(12)
            top = QHBoxLayout()
            symbol = label()
            icons.apply_pixmap(symbol, icon_name, 24, "accent")
            heading = label(title, role="subtitle")
            description_label = label(description, role="muted", wrap=True)
            for child in (symbol, heading, description_label):
                child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            top.addWidget(symbol)
            top.addSpacing(6)
            top.addWidget(heading, 1)
            card_layout.addLayout(top)
            card_layout.addWidget(description_label)
            card.setEnabled(enabled)
            operations.addWidget(card, 1)
        layout.addLayout(operations)
        layout.addStretch()
        self._footer(tr("当前游戏：{game_name}", game_name=self._game_name), (button(tr("关闭"), self.reject),))

    def _show_source(self):
        """展示预设文件选择及拖放入口，保留固定目标游戏的说明。"""
        layout = self._new_page(tr("导入分类预设"), "source", size=(720, 520), step=0)
        layout.addWidget(self._context())
        drop = QFrame()
        drop.setProperty("presetRole", "drop")
        box = QVBoxLayout(drop)
        box.setContentsMargins(24, 24, 24, 24)
        box.setSpacing(14)
        symbol = label()
        icons.apply_pixmap(symbol, "meta/category", 42, "accent")
        box.addWidget(symbol, 0, Qt.AlignmentFlag.AlignHCenter)
        box.addWidget(label(tr("选择分类预设文件"), role="subtitle"), 0, Qt.AlignmentFlag.AlignHCenter)
        box.addWidget(
            button(
                tr("选择预设文件"),
                self._pick_source,
                icon="actions/open",
                primary=True,
                name="categoryPresetChooseFile",
            ),
            0,
            Qt.AlignmentFlag.AlignHCenter,
        )
        box.addWidget(label(".d3dxcp", role="muted"), 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(drop, 1)
        self._footer(tr("分类根目录 · {game_name}", game_name=self._game_name), (button(tr("取消"), self.reject),))

    def _show_no_game(self):
        """提示先添加并选择游戏，提供前往游戏页的导航操作。"""
        layout = self._new_page(tr("请先添加游戏"), "no_game", size=(640, 330))
        layout.addWidget(self._notice(tr("请在游戏页添加并选择游戏。")))
        layout.addStretch()
        self._footer(
            actions=(
                button(tr("取消"), self.reject),
                button(tr("前往游戏页"), self._go_games, primary=True, name="categoryPresetGoGames"),
            )
        )

    def _go_games(self):
        """发出游戏页导航请求，再通过统一清理路径关闭窗口。"""
        self.games_requested.emit()
        self.reject()

    def _pick_source(self):
        """打开预设文件选择器，只有选定路径后才启动读取和比较。"""
        path, _ = QFileDialog.getOpenFileName(self, tr("选择预设文件"), self._source, tr(FILE_FILTER))
        if path:
            self._load_source(path)

    def _load_source(self, path):
        """空闲且有目标游戏时替换导入来源，将旧计划移交清理队列并提交后台预览任务。"""
        if self.busy or not self._game:
            return
        service = self._service
        old = self._import_plan
        self._import_plan = None
        if old is not None:
            self._pending_discards.append(old)
        pending = self._pending_discards
        self._source = str(path)

        def read(**kwargs):
            """先清理不再使用的计划，再以任务传入的取消和进度回调读取新来源。"""
            _discard_plans(service, pending)
            return service.preview_import(path, self.game_id, **kwargs)

        self._run_task("read", read, self.show_import_plan)

    def show_import_plan(self, plan):
        """接管导入计划并展示整树比较、图片策略与冲突；已请求关闭时仅保存计划供清理。"""
        self._import_plan = plan
        if self._close_requested:
            return
        layout = self._new_page(tr("导入分类预设"), "preview", size=(1160, 780), step=1)
        context = panel()
        context_layout = QHBoxLayout(context)
        context_layout.setContentsMargins(16, 12, 16, 12)
        source = QVBoxLayout()
        source.addWidget(label(tr("预设文件"), role="muted"))
        file_row = QHBoxLayout()
        filename = label(plan.source_name, role="strong", wrap=True)
        file_row.addWidget(filename, 1)
        file_row.addWidget(button(tr("更换"), self._pick_source, quiet=True))
        source.addLayout(file_row)
        if plan.suggested_game_name:
            source.addWidget(
                label(
                    tr("建议游戏：{suggested_game_name}", suggested_game_name=plan.suggested_game_name),
                    role="muted",
                    wrap=True,
                )
            )
        context_layout.addLayout(source, 1)
        target = QVBoxLayout()
        target.addWidget(label(tr("目标游戏 · 分类根目录"), role="muted"))
        target.addWidget(label(plan.target.name, role="strong", wrap=True))
        root = label(str(plan.target.root), role="muted", wrap=True)
        root.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        target.addWidget(root)
        context_layout.addLayout(target, 1)
        layout.addWidget(context)
        layout.addWidget(
            stat_strip(
                [
                    (plan.new_count, tr("新增目录"), "success"),
                    (plan.existing_count, tr("已存在"), ""),
                    (plan.fill_count, tr("补充图片"), "accent"),
                    (plan.conflict_count, tr("路径冲突"), "danger" if plan.conflict_count else ""),
                ]
            )
        )
        if plan.suggested_game_name and plan.suggested_game_name != plan.target.name:
            layout.addWidget(
                self._notice(
                    tr(
                        "预设建议游戏为“{suggested_game_name}”，当前为“{name}”。请确认目标游戏。",
                        suggested_game_name=plan.suggested_game_name,
                        name=plan.target.name,
                    ),
                    tone="warning",
                )
            )
        self._tree = PresetTree()
        if plan.conflict_count:
            layout.addWidget(
                self._notice(
                    tr("{conflict_count} 处路径冲突，暂不能导入。", conflict_count=plan.conflict_count),
                    tone="danger",
                    action=button(tr("定位问题"), self._tree.next_issue, quiet=True),
                )
            )
        elif not plan.new_count and not plan.fill_count:
            layout.addWidget(self._notice(tr("全部目录已存在，无需导入。"), tone="success"))
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        self._inspector = PresetInspector()
        inspector_scroll = QScrollArea()
        inspector_scroll.setWidgetResizable(True)
        inspector_scroll.setMinimumWidth(235)
        inspector_scroll.setWidget(self._inspector)
        splitter.addWidget(self._tree)
        splitter.addWidget(inspector_scroll)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([790, 270])
        layout.addWidget(splitter, 1)
        self._tree.node_selected.connect(self._inspector.set_row)
        self._tree.set_rows(plan.rows)
        start = button(tr("开始导入"), self._start_import, primary=True, name="categoryPresetStartImport")
        start.setEnabled(plan.can_import)
        copy = (
            tr("处理冲突后点击“重新比较”。")
            if plan.conflict_count
            else (
                tr(
                    "本次新增 {new_count} 个目录，补充 {fill_count} 张图片。",
                    new_count=plan.new_count,
                    fill_count=plan.fill_count,
                )
                if plan.can_import
                else tr("无需导入。")
            )
        )
        self._footer(
            copy,
            (
                button(tr("重新比较"), self._recompare, icon="actions/refresh", quiet=True),
                button(tr("取消"), self.reject),
                start,
            ),
        )

    def _recompare(self):
        """使用当前导入计划重新比较目标分类状态，并在完成后替换预览页面。"""
        if self._import_plan is not None and not self.busy:
            self._run_task("compare", partial(self._service.recompare, self._import_plan), self.show_import_plan)

    def _start_import(self):
        """仅在计划允许导入且窗口空闲时，将实际导入交给后台服务执行。"""
        plan = self._import_plan
        if plan is not None and plan.can_import and not self.busy:
            self._run_task("import", partial(self._service.execute_import, plan), self._import_success)

    def _prepare_export(self):
        """保存图片选项快照并排队清理旧导出计划，然后后台准备完整分类树。"""
        if self.busy:
            return
        service = self._service
        old = self._export_plan
        self._export_plan = None
        if old is not None:
            self._pending_discards.append(old)
        pending = self._pending_discards
        include_images, omitted_images = self._include_images, tuple(self._omitted_images)

        def prepare(**kwargs):
            """清理待释放计划后，使用固定图片选项生成目标游戏的导出预览。"""
            _discard_plans(service, pending)
            return service.preview_export(
                self.game_id, include_images=include_images, omitted_images=omitted_images, **kwargs
            )

        self._run_task("export_preview", prepare, self.show_export_plan)

    def show_export_plan(self, plan):
        """接管导出计划并展示名称、目录、图片问题与分类树；关闭请求期间仅保留清理所需引用。"""
        self._export_plan = plan
        if self._close_requested:
            return
        layout = self._new_page(tr("导出分类预设"), "export", size=(1080, 740))
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        form_widget = QWidget()
        form = QVBoxLayout(form_widget)
        form.setContentsMargins(0, 0, 14, 0)
        form.setSpacing(12)
        form.addWidget(label(tr("预设名称"), role="strong"))
        self._name_input = QLineEdit(self._export_name)
        self._name_input.setObjectName("categoryPresetName")
        self._name_input.setMaxLength(512)
        self._name_input.setAccessibleName(tr("预设名称"))
        form.addWidget(self._name_input)
        self._name_error = label(wrap=True)
        self._name_error.setStyleSheet(f"color: {theme.color('danger')};")
        form.addWidget(self._name_error)
        form.addWidget(label(tr("导出范围"), role="strong"))
        form.addWidget(
            label(tr("{name}\n完整分类树 · {count} 个目录", name=plan.target.name, count=len(plan.rows)), wrap=True)
        )
        self._images_check = QCheckBox(tr("包含目录图片"))
        self._images_check.setChecked(self._include_images)
        self._images_check.setObjectName("categoryPresetIncludeImages")
        form.addWidget(self._images_check)
        form.addWidget(
            stat_strip(
                [
                    (len(plan.rows), tr("分类目录"), ""),
                    (plan.image_count, tr("目录图片"), ""),
                    (f"{plan.image_bytes / 1024 / 1024:.1f}", tr("图片 / MiB"), ""),
                ]
            )
        )
        for path, reason in plan.image_issues.items():
            omit = button(
                tr("取消这张图片"), partial(self._omit_image, path), quiet=True, name="categoryPresetOmitImage"
            )
            form.addWidget(self._notice(f"{path}\n{reason}", tone="warning", action=omit))
        form.addWidget(label(tr("文件类型"), role="strong"))
        form.addWidget(label(tr("分类预设文件（.d3dxcp）")))
        form.addWidget(label(tr("输出位置"), role="strong"))
        destination = QHBoxLayout()
        self._destination_input = QLineEdit(self._destination)
        self._destination_input.setReadOnly(True)
        self._destination_input.setAccessibleName(tr("输出位置"))
        destination.addWidget(self._destination_input, 1)
        destination.addWidget(button(tr("选择…"), self._pick_destination))
        form.addLayout(destination)
        form.addWidget(label(tr("输出文件"), role="strong"))
        self._filename_label = label(preset_filename(self._export_name), wrap=True)
        self._filename_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addWidget(self._filename_label)
        form.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(285)
        scroll.setWidget(form_widget)
        splitter.addWidget(scroll)
        self._export_tree = PresetTree(export_mode=True)
        self._export_tree.set_rows(plan.rows, image_issues=plan.image_issues)
        splitter.addWidget(self._export_tree)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([345, 670])
        layout.addWidget(splitter, 1)
        self._export_button = button(tr("开始导出"), self._start_export, primary=True, name="categoryPresetStartExport")
        self._footer(actions=(button(tr("取消"), self.reject), self._export_button))
        self._name_input.textChanged.connect(self._update_export_name)
        self._images_check.toggled.connect(self._toggle_images)
        self._update_export_name()

    def _update_export_name(self):
        """校验名称并同步输出文件名；仅在有分类、目的目录且无名称或图片问题时启用导出。"""
        self._export_name = self._name_input.text()
        error = preset_name_error(self._export_name)
        self._name_error.setText(error)
        self._name_error.setVisible(bool(error))
        self._name_input.setProperty("presetInvalid", bool(error))
        self._name_input.style().unpolish(self._name_input)
        self._name_input.style().polish(self._name_input)
        self._filename_label.setText(preset_filename(self._export_name))
        issues = bool(self._export_plan.image_issues)
        self._footer_copy.setText(
            tr("请先处理无法读取的目录图片。") if issues else tr("请检查预设名称。") if error else ""
        )
        self._export_button.setEnabled(bool(self._export_plan.rows and self._destination and not error and not issues))

    def _toggle_images(self, checked):
        """保存是否包含目录图片的选择并重新准备导出计划。"""
        self._include_images = checked
        self._prepare_export()

    def _omit_image(self, path):
        """记录需省略图片的分类路径，再重新生成导出预览以消除对应图片问题。"""
        self._omitted_images.add(path)
        self._prepare_export()

    def _pick_destination(self):
        """选择输出目录并重新计算导出操作是否可用，取消选择时保留原目录。"""
        path = QFileDialog.getExistingDirectory(self, tr("选择输出位置"), self._destination)
        if path:
            self._destination = path
            self._destination_input.setText(path)
            self._update_export_name()

    def _start_export(self):
        """窗口空闲且导出条件满足时，使用当前计划、目的目录和名称启动后台导出。"""
        if self.busy or not self._export_button.isEnabled():
            return
        self._run_task(
            "export",
            partial(self._service.execute_export, self._export_plan, self._destination, self._export_name),
            self._export_success,
        )

    def _run_task(self, kind, work, success, *, silent=False):
        """串行启动后台工作并保存成功回调。

        任务执行前等待已取消的图片读取释放源文件；静默清理保留页面但禁用交互，其他任务展示进度页。
        """
        if self.busy:
            return
        self._task_kind, self._task_success = kind, success
        self._committed = False
        titles = {
            "read": tr("正在读取分类预设"),
            "compare": tr("正在比较分类树"),
            "import": tr("正在导入分类预设"),
            "export_preview": tr("正在准备导出"),
            "export": tr("正在导出分类预设"),
            "cleanup": tr("正在关闭"),
        }
        if silent:
            # 清理期间保留当前页面和几何，避免关闭时出现短暂的额外任务页面。
            for inspector in self._page.findChildren(PresetInspector):
                self._image_waiters.extend(inspector.stop_loading())
            self._page.setEnabled(False)
        else:
            layout = self._new_page(titles[kind], "job", size=(700, 440))
            layout.addStretch()
            layout.addWidget(label(tr("目标游戏：{game_name}", game_name=self._game_name), role="muted"))
            self._progress_title = label(tr("准备中…"), role="subtitle", wrap=True)
            layout.addWidget(self._progress_title)
            self._progress_bar = QProgressBar()
            self._progress_bar.setRange(0, 0)
            self._progress_bar.setTextVisible(False)
            layout.addWidget(self._progress_bar)
            self._progress_number = label("", role="muted")
            self._progress_number.setAlignment(Qt.AlignmentFlag.AlignRight)
            layout.addWidget(self._progress_number)
            layout.addStretch()
            self._cancel_button = button(tr("取消"), self._cancel_task, name="categoryPresetCancelTask")
            self._footer(actions=(self._cancel_button,))
        image_waiters, self._image_waiters = self._image_waiters, []

        def settled_work(**kwargs):
            """在工作线程等待旧页面的图片解码结束，再调用可能删除暂存源文件的服务操作。"""
            for done in image_waiters:
                done.wait()
            return work(**kwargs)

        worker = _PresetTask(settled_work, self)
        self._worker = worker
        worker.progress.connect(self._task_progress)
        worker.finished.connect(self._task_finished)
        worker.start()

    def _task_progress(self, phase, percent):
        """在界面线程翻译任务阶段并约束百分比；收到提交阶段后禁用取消操作。"""
        captions = {
            "校验文件": tr("读取分类预设"),
            "校验图片": tr("读取目录图片"),
            "读取完成": tr("读取分类预设"),
            "准备图片": tr("准备目录图片"),
            "写入文件": tr("生成预设文件"),
            "写入完成": tr("生成预设文件"),
            "准备完成": tr("准备导出"),
            "已提交": tr("正在完成…"),
        }
        self._progress_title.setText(captions.get(phase, tr(phase)))
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(max(0, min(percent, 100)))
        self._progress_number.setText(f"{max(0, min(percent, 100))}%")
        if phase == "已提交":
            self._committed = True
            self._cancel_button.setEnabled(False)
            self._cancel_button.setText(tr("正在完成…"))

    def _cancel_task(self):
        """仅对未提交且非清理的任务请求协作取消，并禁用重复取消操作。"""
        if self._worker is not None and not self._committed and self._task_kind != "cleanup":
            self._worker.request_cancel()
            self._cancel_button.setEnabled(False)
            self._cancel_button.setText(tr("正在取消…"))

    def _task_finished(self):
        """结算线程并延迟释放 Qt 对象，再按错误、取消或成功分派页面更新及后续关闭清理。"""
        worker = self._worker
        if worker is None:
            return
        self._worker = None
        self._page.setEnabled(True)
        worker.deleteLater()
        kind, callback = self._task_kind, self._task_success
        if worker.error is not None:
            self._close_requested = False
            self._show_error(worker.error, file_error=kind == "read")
        elif worker.was_cancelled:
            if self._close_requested:
                self._finish_close()
            else:
                self._show_cancelled(kind)
        else:
            callback(worker.result)
            if self._close_requested and not self.busy:
                self._finish_close()

    def _show_error(self, message, *, file_error=False):
        """区分来源文件错误与执行错误并提供重试入口；清理失败时允许重试或明确保留暂存文件。"""
        from src.core.category_preset import CategoryPresetFileAccessError, CategoryPresetFormatError

        detail = str(message)
        if file_error and isinstance(message, CategoryPresetFormatError):
            message = tr("分类预设文件格式不正确，请重新获取或请分享者重新导出。")
        elif file_error and isinstance(message, CategoryPresetFileAccessError):
            message = tr("无法读取分类预设文件，请确认文件仍存在且可以访问。")
        else:
            message = detail
        title = tr("无法读取分类预设") if file_error else tr("操作未完成")
        layout = self._new_page(
            title, "file_error" if file_error else "error", size=(760, 450), step=0 if file_error else None
        )
        layout.addStretch()
        layout.addWidget(self._notice(message, tone="danger"))
        if file_error:
            layout.addWidget(label(Path(self._source).name, role="strong", wrap=True))
            if message != detail:
                details = QPlainTextEdit(detail)
                details.setReadOnly(True)
                details.setMaximumHeight(120)
                details.hide()
                toggle = button(tr("详细信息"), lambda: details.setVisible(not details.isVisible()), quiet=True)
                layout.addWidget(toggle, 0, Qt.AlignmentFlag.AlignLeft)
                layout.addWidget(details)
        layout.addStretch()
        if self._task_kind == "cleanup":
            self._footer(
                tr("可重试清理，或保留当前文件后关闭。"),
                (
                    button(tr("保留文件并关闭"), self._keep_files_and_close),
                    button(tr("重试清理"), self.request_close, primary=True),
                ),
            )
            return
        if file_error:
            retry = button(tr("重新选择文件"), self._pick_source, primary=True)
        elif self._import_plan is not None and not self._task_kind.startswith("export"):
            retry = button(tr("重新比较"), self._recompare, primary=True)
        else:
            retry = button(tr("返回导出"), self._prepare_export, primary=True)
        self._footer(tr("目标游戏：{game_name}", game_name=self._game_name), (button(tr("关闭"), self.reject), retry))

    def _show_cancelled(self, kind):
        """按被取消任务类型提供返回预览或关闭操作，尚无预览的导出准备只提供关闭。"""
        layout = self._new_page(tr("操作已取消"), "cancelled", size=(680, 380))
        layout.addStretch()
        layout.addStretch()
        if kind == "export_preview":
            self._footer(actions=(button(tr("关闭"), self.reject),))
            return
        back = (
            self._prepare_export
            if kind.startswith("export")
            else self._recompare
            if self._import_plan
            else self._show_source
        )
        self._footer(actions=(button(tr("关闭"), self.reject), button(tr("返回预览"), back, primary=True)))

    def _import_success(self, result):
        """更新分类数量并通知宿主刷新；展示导入统计或提交后仍需清理的恢复路径。"""
        self._category_count += result.new_count
        self.import_completed.emit(result.game_id)
        if self._close_requested:
            return
        pending = getattr(result, "recovery_paths", ())
        if pending:
            layout = self._new_page(tr("分类已导入，清理待完成"), "recovery", size=(800, 520))
            layout.addWidget(
                self._notice(
                    tr("以下路径尚未完成清理，已保留恢复记录。重启软件后会重试；相关目录暂不自动纳管。"), tone="warning"
                )
            )
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            paths = label("\n".join(pending), wrap=True)
            paths.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            scroll.setWidget(paths)
            layout.addWidget(scroll, 1)
            self._footer(
                tr("目标游戏：{game_name}", game_name=self._game_name),
                (
                    button(tr("关闭"), self.reject),
                    button(tr("查看分类"), partial(self._view_categories, result.game_id), primary=True),
                ),
            )
            return
        layout = self._new_page(tr("导入分类预设"), "result", size=(720, 500), step=2)
        layout.addStretch()
        symbol = label()
        icons.apply_pixmap(symbol, "mod-updates/check-circle", 52, "success")
        layout.addWidget(symbol, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(label(tr("导入完成"), role="title"), 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(
            stat_strip(
                [
                    (result.new_count, tr("新增目录"), "success"),
                    (result.existing_count, tr("已存在"), ""),
                    (result.fill_count, tr("补充图片"), "accent"),
                ]
            )
        )
        if result.new_image_count:
            layout.addWidget(
                self._notice(
                    tr("新增目录包含 {new_image_count} 张图片。", new_image_count=result.new_image_count),
                    tone="success",
                )
            )
        layout.addStretch()
        self._footer(
            tr("目标游戏：{game_name}", game_name=self._game_name),
            (
                button(tr("再次导入此文件"), self._recompare),
                button(tr("查看分类"), partial(self._view_categories, result.game_id), primary=True),
            ),
        )

    def _export_success(self, result):
        """展示导出分类和图片数量及最终文件路径；关闭请求期间跳过结果页。"""
        if self._close_requested:
            return
        layout = self._new_page(tr("导出分类预设"), "export_result", size=(740, 520))
        layout.addStretch()
        symbol = label()
        icons.apply_pixmap(symbol, "mod-updates/check-circle", 52, "success")
        layout.addWidget(symbol, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(label(tr("导出完成"), role="title"), 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(
            stat_strip(
                [
                    (result.node_count, tr("分类目录"), ""),
                    (result.image_count, tr("目录图片"), ""),
                    (1, tr("预设文件"), "accent"),
                ]
            )
        )
        output = label(str(result.path), role="strong", wrap=True)
        output.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(output)
        layout.addStretch()
        self._footer(
            tr("当前游戏：{game_name}", game_name=self._game_name),
            (button(tr("返回导出"), self._prepare_export), button(tr("完成"), self.reject, primary=True)),
        )

    def _view_categories(self, game_id):
        """请求宿主导航到指定游戏分类，并通过统一清理路径关闭窗口。"""
        self.view_categories_requested.emit(game_id)
        self.reject()

    def request_close(self):
        """取消和清理完成前保留窗口与服务。"""
        self._close_requested = True
        if self.busy:
            self._cancel_task()
        else:
            self._finish_close()

    def _finish_close(self):
        """将持有计划移交后台清理，清理成功后才拒绝对话框；无待清理资源时直接关闭。"""
        plans = [plan for plan in (self._import_plan, self._export_plan) if plan is not None]
        self._import_plan = self._export_plan = None
        self._pending_discards.extend(plans)
        pending = self._pending_discards
        if pending and self._service_instance is not None:
            service = self._service_instance

            def cleanup(**kwargs):
                """在后台逐项释放计划暂存资源，失败项留在共享队列供重试。"""
                _discard_plans(service, pending)

            self._close_requested = False
            self._run_task("cleanup", cleanup, lambda result: super(CategoryPresetDialog, self).reject(), silent=True)
        else:
            pending.clear()
            super().reject()

    def _keep_files_and_close(self):
        """记录用户选择保留的暂存路径，释放窗口对清理队列的引用并关闭，不删除文件。"""
        logger.warning(
            "分类预设暂存清理未完成，用户选择保留文件：%s",
            "、".join(str(plan.staging) for plan in self._pending_discards),
        )
        self._pending_discards.clear()
        super().reject()

    def reject(self):
        """将取消按钮和 Esc 的关闭统一转入取消任务、等待清理的流程。"""
        self.request_close()

    def closeEvent(self, event):
        """存在后台任务或待释放计划时暂缓系统关闭，并启动统一关闭流程。"""
        if self.busy or self._import_plan is not None or self._export_plan is not None or self._pending_discards:
            event.ignore()
            self.request_close()
        else:
            super().closeEvent(event)

    def dragEnterEvent(self, event):
        """仅在可选择来源的空闲页面接受全部为本地路径的拖放进入事件。"""
        if self._mode in {"source", "preview", "file_error"} and not self.busy and event.mimeData().hasUrls():
            if all(url.isLocalFile() for url in event.mimeData().urls()):
                event.acceptProposedAction()

    def dropEvent(self, event):
        """在来源页面读取单个本地文件，多文件拖放显示可恢复的来源错误。"""
        if self.busy or self._mode not in {"source", "preview", "file_error"}:
            return
        urls = event.mimeData().urls()
        if len(urls) != 1:
            self._show_error(tr("请一次选择一个分类预设文件（.d3dxcp）。"), file_error=True)
        elif urls[0].isLocalFile():
            self._load_source(urls[0].toLocalFile())
        event.acceptProposedAction()

    def changeEvent(self, event):
        """主题实际变化时重建局部样式并刷新图标，避免重复响应调色板事件。"""
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "_theme"):
            current = theme.current_theme()
            if current != self._theme:
                self._theme = current
                self.setStyleSheet(stylesheet())
                icons.retheme_icons(self)
        super().changeEvent(event)
