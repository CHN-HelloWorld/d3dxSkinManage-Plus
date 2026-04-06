"""使用可取消服务操作完成分类预设流程。"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from threading import Event

from PySide6.QtCore import QEvent, QSize, Qt, QThread, Signal
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
from src.utils.i18n import t as tr
from src.utils.logger import logger


def _discard_plans(service, pending):
    """对话框串行调度任务，清理成功前保留所有权。"""
    while pending:
        service.discard(pending[0])
        del pending[0]


class _PresetTask(QThread):
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
