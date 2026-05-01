"""单个 Mod 命名变体的对话框与命令路由。"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.ui import theme
from src.ui.dialog_chrome import decorate_dialog
from src.ui.variant_worker import VariantWorker
from src.ui.worker_lifecycle import manage_page_worker
from src.utils.i18n import t as tr


class _VariantDialog(QDialog):
    """承载变体命名、引用说明、错误提示和默认值补齐操作的非阻塞窗口模态对话框。"""

    def __init__(
        self, parent, title, description, confirm, *, name=None, references=None, summary="", destructive=False
    ):
        """依据操作模式构建变体说明、名称、引用与确认控件，关闭时自动删除并按内容限制尺寸。"""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(400)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setMinimumWidth(0)
        body.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(22, 18, 22, 18)
        body_layout.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("variantDialogHeading")
        heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setWordWrap(True)
        body_layout.addWidget(heading)
        self.description = QLabel(description)
        self.description.setTextFormat(Qt.TextFormat.PlainText)
        self.description.setWordWrap(True)
        body_layout.addWidget(self.description)
        if summary:
            label = QLabel(summary)
            label.setObjectName("variantSnapshotSummary")
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            body_layout.addWidget(label)
        self.name_input = None
        if name is not None:
            body_layout.addWidget(QLabel(tr("变体名称")))
            self.name_input = QLineEdit(name)
            self.name_input.setObjectName("variantNameInput")
            self.name_input.setMaxLength(40)
            self.name_input.setPlaceholderText(tr("输入变体名称"))
            self.name_input.setAccessibleName(tr("变体名称"))
            body_layout.addWidget(self.name_input)
            helper = QLabel(tr("同一个 Mod 内名称不可重复，最多 40 个字符。"))
            helper.setObjectName("variantDialogHelper")
            body_layout.addWidget(helper)
        if references is not None:
            body_layout.addWidget(QLabel(tr("引用此变体的组合")))
            if references:
                for name in references:
                    label = QLabel(name)
                    label.setTextFormat(Qt.TextFormat.PlainText)
                    label.setWordWrap(True)
                    label.setObjectName("variantReference")
                    body_layout.addWidget(label)
            else:
                body_layout.addWidget(QLabel(tr("当前没有组合引用这个变体。")))
            helper = QLabel(tr("如需让不同组合保留独立配置，可以先创建副本。"))
            helper.setWordWrap(True)
            helper.setObjectName("variantDialogHelper")
            body_layout.addWidget(helper)
        self.error_label = QLabel()
        self.error_label.setObjectName("variantDialogError")
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        body_layout.addWidget(self.error_label)
        self.defaults_helper = QLabel(
            tr("默认值可能与当前游戏状态不同。建议先在游戏中保存配置，再重新读取；也可使用默认值补齐后保存。")
        )
        self.defaults_helper.setObjectName("variantDialogHelper")
        self.defaults_helper.setWordWrap(True)
        self.defaults_helper.hide()
        body_layout.addWidget(self.defaults_helper)
        self.defaults_button = QPushButton(tr("使用默认值补齐并保存"))
        self.defaults_button.setObjectName("variantFillDefaults")
        self.defaults_button.setAutoDefault(False)
        self.defaults_button.hide()
        body_layout.addWidget(self.defaults_button)
        body_layout.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        footer = QWidget()
        footer.setObjectName("variantDialogFooter")
        buttons = QHBoxLayout(footer)
        buttons.setContentsMargins(22, 14, 22, 14)
        buttons.addStretch()
        cancel = QPushButton(tr("取消"))
        cancel.setMinimumHeight(34)
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.confirm_button = QPushButton(confirm)
        self.confirm_button.setObjectName("variantConfirm")
        self.confirm_button.setDefault(True)
        self.confirm_button.setMinimumWidth(90)
        self.confirm_button.setMinimumHeight(34)
        buttons.addWidget(self.confirm_button)
        outer.addWidget(footer)
        accent = theme.color("danger" if destructive else "accent")
        self.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget { background: transparent; }"
            f"QLabel {{ color: {theme.color('text.primary')}; background: transparent;"
            f" font-size: {theme.font('size_body')}px; }}"
            f"QLineEdit, QPushButton {{ font-size: {theme.font('size_body')}px; }}"
            "#variantDialogHeading { font-size: 20px; font-weight: 600; }"
            f"#variantDialogHelper {{ color: {theme.color('text.secondary')};"
            f" font-size: {theme.font('size_secondary')}px; }}"
            f"#variantSnapshotSummary {{ padding: 10px; background: {theme.rgba('accent', 0.07)};"
            " border-radius: 5px; }"
            f"#variantReference {{ padding: 5px 8px; background: {theme.rgba('accent', 0.05)}; border-radius: 4px; }}"
            f"#variantDialogError {{ color: {theme.color('danger')}; }}"
            f"#variantDialogFooter {{ border-top: 1px solid {theme.color('stroke.divider')};"
            " background: transparent; }"
            f"#variantConfirm {{ background: {accent}; color: white; border: 1px solid {accent}; }}"
        )
        decorate_dialog(self)
        self._body, self._footer = body, footer
        self._resize_to_contents()

    def _resize_to_contents(self):
        """按正文换行高度计算对话框尺寸，同时限制在屏幕可用高度内。"""
        self.ensurePolished()
        margins = self.contentsMargins()
        width = 470
        body_layout = self._body.layout()
        height = body_layout.totalHeightForWidth(width - margins.left() - margins.right())
        if height < 0:
            height = self._body.sizeHint().height()
        height += self._footer.sizeHint().height() + margins.top() + margins.bottom()
        screen = self.screen()
        available = screen.availableGeometry().height() if screen else 800
        self.resize(width, min(max(190, height), available - 80, 650))

    def set_error(self, message, *, allow_defaults=False):
        """显示提交错误并按需提供默认值补齐操作，重新布局后聚焦名称输入。"""
        self.error_label.setText(message)
        self.error_label.show()
        self.defaults_helper.setVisible(allow_defaults)
        self.defaults_button.setVisible(allow_defaults)
        self._resize_to_contents()
        if self.name_input is not None:
            self.name_input.setFocus()


class ModVariantController(QObject):
    """业务服务独立于总览页面，对话框绑定原始目标。"""

    notification = Signal(str, bool)
    activity_changed = Signal(bool)
    stopped = Signal()

    def __init__(self, page, mod_manager, *, enable_callback=None, notify=None, parent=None):
        """连接变体面板与核心服务，初始化选择上下文、后台任务和对话框状态。"""
        super().__init__(parent or page)
        self.page = page
        self.manager = mod_manager
        self.panel = page._variant_panel
        self._enable = enable_callback or page.mod_toggle_requested.emit
        self._context = ("", "")
        self._generation = 0
        self._dialog = None
        self._busy = False
        self._state = {}
        self._variants = []
        self._worker = None
        self._refresh_pending = False
        self._closing = False
        self._publishing = False
        if notify is not None:
            self.notification.connect(notify)
        page.variant_context_changed.connect(self._set_context)
        self.panel.apply_requested.connect(self.apply)
        self.panel.enable_requested.connect(self.enable_and_apply)
        self.panel.save_requested.connect(lambda mid: self.open_dialog("save", mid))
        for mode, signal in (
            ("update", self.panel.update_requested),
            ("rename", self.panel.rename_requested),
            ("copy", self.panel.copy_requested),
            ("delete", self.panel.delete_requested),
        ):
            signal.connect(lambda mid, vid, action=mode: self.open_dialog(action, mid, vid))

    @property
    def busy(self):
        """返回当前后台变体任务是否会修改持久化状态。"""
        return self._worker is not None and self._worker.task.mutating

    @property
    def running(self):
        """返回是否仍有读取或修改变体的后台任务。"""
        return self._worker is not None

    @property
    def game_id(self):
        """返回当前任务所属游戏；空闲时返回面板上下文的游戏。"""
        return self._worker.task.game_id if self._worker is not None else self._context[0]

    def close(self):
        """禁止后续刷新，关闭编辑对话框并请求任务停止，空闲时立即报告停止。"""
        self._closing = True
        self._refresh_pending = False
        self.panel.set_switch_progress()
        self._close_dialog()
        if self._worker is not None:
            self._worker.stop()
        else:
            self.stopped.emit()

    def resume(self):
        """解除关闭标记，并刷新当前选中 Mod 的变体状态。"""
        self._closing = False
        if self._context[1]:
            self.refresh(self._context[1])

    def _set_context(self, mod_id):
        """切换 Mod 时增加上下文代次、关闭旧对话框并清空状态，再读取新选择。"""
        context = (str(self.manager.current_game_id or ""), mod_id)
        if context != self._context:
            self._generation += 1
            self.panel.set_switch_progress()
            self._close_dialog()
            self._state, self._variants = {}, []
            self._context = context
        if mod_id:
            self.refresh(mod_id)

    def _valid(self, mod_id):
        """核验控制器空闲、当前游戏和选中 Mod 与面板上下文一致。"""
        selected = self.page._selected_mod
        return (
            not self._busy
            and not self._closing
            and bool(mod_id)
            and self._context == (self.manager.current_game_id, mod_id)
            and self.page._panel_mode == "mod"
            and selected is not None
            and selected.mod_id == mod_id
        )

    def refresh(self, mod_id, *, game_id=None):
        """为当前 Mod 准备后台读取；已有任务时合并一次后续刷新，读取准备失败时保留错误状态。"""
        current_game, selected = self._context
        if (
            self._closing
            or not mod_id
            or mod_id != selected
            or current_game != self.manager.current_game_id
            or (game_id is not None and game_id != current_game)
        ):
            return
        if self._worker is not None:
            self._refresh_pending = True
            self.panel.set_busy(True)
            return
        try:
            task = self.manager.variants.prepare_task(mod_id)
        except (ValueError, OSError, RuntimeError) as error:
            self._show_error(str(error), read_failure=True)
            return
        self._start_task(task)

    def handle_core_event(self, event, **payload):
        """游戏切换时清空上下文，变体或部署状态变化时局部刷新，忽略自身发布的变体通知。"""
        if event == "game_changed":
            self._generation += 1
            self._context = ("", "")
            self._close_dialog()
            self.panel.clear_context()
        elif event in ("variant_state_changed", "mod_enabled", "mod_disabled"):
            if event == "variant_state_changed" and self._publishing:
                return
            self.refresh(payload.get("mod_id", ""), game_id=payload.get("game_id"))

    def _close_dialog(self):
        """解除当前对话框引用并拒绝窗口，避免旧选择继续提交。"""
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            dialog.reject()

    def _show_error(self, message, *, read_failure=False):
        """为仍选中的 Mod 更新错误原因，读取失败时撤销保存能力，操作失败另发通知。"""
        game_id, mod_id = self._context
        selected = self.page._selected_mod
        if selected is not None and selected.mod_id == mod_id and game_id == self.manager.current_game_id:
            state = dict(
                self._state
                or dict(
                    mod_id=mod_id,
                    enabled=selected.is_enabled,
                    selected_variant_id=None,
                    status="unconfirmed",
                    can_save=False,
                    captured_at="",
                    variable_count=0,
                    confirmed_count=0,
                )
            )
            state["reason"] = message
            if read_failure:
                state.update(can_save=False, status="unconfirmed")
            self.page.set_variant_state(mod_id, state, self._variants)
        if not read_failure:
            self.notification.emit(message, False)

    def _start_task(self, task, *, on_success=None, on_error=None):
        """保存任务的选择代次与完成回调，托管后台线程并更新面板忙碌和切换进度。"""
        worker = VariantWorker(task)
        worker.context = self._context
        worker.generation = self._generation
        worker.on_success, worker.on_error = on_success, on_error
        self._worker = worker
        self._busy = True
        self.panel.set_busy(True)
        if task.command == "apply":
            self.panel.set_switch_progress(task.variant_id, active=True)
        manage_page_worker(self, worker)
        worker.finished.connect(self._task_finished)
        if task.mutating:
            self.activity_changed.emit(True)
        worker.start()

    def _task_finished(self):
        """在界面线程发布并释放任务，仅向当前上下文交付结果，随后恢复面板状态并处理合并刷新。"""
        worker = self.sender()
        if worker is not self._worker:
            return
        task = worker.task
        try:
            self._publishing = True
            task.publish(self.manager)
        except (ValueError, OSError, RuntimeError) as error:
            worker.error = worker.error or error
        finally:
            self._publishing = False
            task.release()
        self._worker = None
        self._busy = False
        self.panel.set_switch_progress()
        current = (
            not self._closing
            and worker.context == self._context
            and worker.generation == self._generation
            and task.game_id == self.manager.current_game_id
        )
        if current:
            if worker.error is None:
                self._state, self._variants = dict(task.state), list(task.variants)
                self.page.set_variant_state(task.mod_id, self._state, self._variants)
                if worker.on_success is not None:
                    worker.on_success(task.result)
            elif worker.on_error is not None:
                worker.on_error(str(worker.error))
            elif not isinstance(worker.error, InterruptedError):
                self._show_error(str(worker.error), read_failure=not task.mutating)
        self.panel.set_busy(self._context[1] in self.page._deploying_frac)
        # 生命周期管理可保留线程包装对象，完成后释放模型、异常堆栈和对话框闭包。
        worker.task = worker.error = worker.on_success = worker.on_error = None
        worker.deleteLater()
        if task.mutating:
            self.activity_changed.emit(False)
        if self._closing:
            self.stopped.emit()
        elif self._refresh_pending:
            self._refresh_pending = False
            self.refresh(self._context[1])

    def _execute(self, mod_id, command, *, on_success=None, on_error=None, **options):
        """校验当前选择后准备指定变体命令，准备失败交给错误回调，成功则启动后台任务。"""
        if not self._valid(mod_id):
            return
        try:
            task = self.manager.variants.prepare_task(mod_id, command=command, **options)
        except (ValueError, OSError, RuntimeError) as error:
            (on_error or self._show_error)(str(error))
            return
        self._start_task(task, on_success=on_success, on_error=on_error)

    def apply(self, mod_id, variant_id):
        """应用指定变体，不额外请求启用当前 Mod。"""
        self._apply(mod_id, variant_id, enable=False)

    def enable_and_apply(self, mod_id, variant_id):
        """应用指定变体，并在需要时继续请求启用当前 Mod。"""
        self._apply(mod_id, variant_id, enable=True)

    def _apply(self, mod_id, variant_id, *, enable):
        """捕获当前启用状态并提交变体应用任务，成功后同步选择与后续启用动作。"""
        if not self._valid(mod_id):
            return
        enabled = bool(self._state.get("enabled"))

        def applied(_result):
            """选中已应用变体，按需触发启用，并根据原启用状态提示实际配置效果。"""
            self.panel.select_variant(variant_id)
            if enable and not enabled:
                self._enable(mod_id)
            self.notification.emit(tr("用户偏好已写入。") if enabled else tr("已设为下次使用的配置。"), True)

        self._execute(mod_id, "apply", variant_id=variant_id, on_success=applied, on_error=self._show_error)

    def _unique_name(self, base):
        """在四十字符限制内生成不区分大小写且不与已有变体重复的名称。"""
        existing = {item.name.casefold() for item in self._variants}
        candidate, index = base[:40], 2
        while candidate.casefold() in existing:
            suffix = f" {index}"
            candidate = base[: 40 - len(suffix)] + suffix
            index += 1
        return candidate

    def open_dialog(self, mode, mod_id, variant_id=None):
        """按保存、更新、重命名、复制或删除模式验证前置状态并打开对应编辑对话框。"""
        if not self._valid(mod_id):
            return
        saved = next((item for item in self._variants if item.variant_id == variant_id), None)
        if mode != "save" and saved is None:
            self._show_error(tr("这个变体已不存在，请刷新后重试。"))
            return
        if mode in ("save", "update") and not self._state.get("enabled"):
            self._show_error(tr("请先启用这个 Mod，再保存或更新变体。"))
            return
        self._close_dialog()
        config = {
            "save": (
                tr("保存当前状态为新变体"),
                tr("为已读取的游戏内配置命名。保存后，当前组合将使用这个变体。"),
                tr("保存并使用"),
            ),
            "rename": (tr("重命名变体"), tr("使用这个变体的组合会自动显示新名称，原有引用继续有效。"), tr("保存名称")),
            "copy": (tr("创建变体副本"), tr("复制一份可独立调整的配置，之后可以在另一个组合中使用。"), tr("创建副本")),
            "update": (
                tr("更新「{value}」？", value=saved.name if saved else ""),
                tr("将已读取的游戏内配置保存到此变体。下列组合会在下次应用时使用更新后的配置。"),
                tr("更新并使用"),
            ),
            "delete": (
                tr("删除「{value}」？", value=saved.name if saved else ""),
                tr("删除后，所有组合中的对应选择都会改为 Mod 默认。如果正在使用此变体，当前配置也会恢复默认。"),
                tr("删除变体"),
            ),
        }
        title, description, confirm = config[mode]
        initial = (
            self._unique_name(tr("新的造型"))
            if mode == "save"
            else saved.name
            if mode == "rename"
            else self._unique_name(tr("{name} 副本", name=saved.name))
            if mode == "copy"
            else None
        )
        try:
            references = (
                [item.name for item in self.manager.variants.references(mod_id, variant_id)]
                if mode == "update"
                else None
            )
        except (ValueError, OSError, RuntimeError) as error:
            self._show_error(str(error))
            return
        count = (
            self._state.get("variable_count", 0) if mode in ("save", "update") else len(saved.variables) if saved else 0
        )
        summary = (
            tr("{display_name} · {count} 项配置", display_name=self.page._selected_mod.display_name, count=count)
            if mode != "delete"
            else ""
        )
        dialog = _VariantDialog(
            self.page.window(),
            title,
            description,
            confirm,
            name=initial,
            references=references,
            summary=summary,
            destructive=mode == "delete",
        )
        self._dialog = dialog
        generation = self._generation

        def submit(use_defaults=False):
            """核验对话框和上下文代次，检查名称后锁定按钮并提交变体操作。"""
            if self._dialog is not dialog or generation != self._generation or not self._valid(mod_id):
                return
            name = dialog.name_input.text().strip() if dialog.name_input is not None else saved.name if saved else ""
            if mode in ("save", "rename", "copy") and not name:
                dialog.set_error(tr("请输入变体名称。"))
                return
            dialog.confirm_button.setEnabled(False)
            dialog.defaults_button.setEnabled(False)

            def failed(message):
                """恢复当前对话框的按钮并显示错误，仅在配置不完整时允许默认值补齐。"""
                if self._dialog is not dialog:
                    return
                allow_defaults = (
                    mode in ("save", "update")
                    and not use_defaults
                    and message == tr("未能读取完整的游戏内配置，请先在游戏中保存配置后重试")
                )
                dialog.confirm_button.setEnabled(True)
                dialog.defaults_button.setEnabled(True)
                dialog.set_error(message, allow_defaults=allow_defaults)

            def succeeded(result):
                """关闭成功提交的对话框，按操作选择结果变体并发送对应完成通知。"""
                if self._dialog is dialog:
                    dialog.accept()
                if mode in ("save", "copy", "update"):
                    self.panel.select_variant(result.variant_id)
                messages = {
                    "save": tr("新变体已保存，当前组合已使用此变体。"),
                    "update": tr("变体已更新，引用它的组合下次应用时使用新内容。"),
                    "rename": tr("变体名称已更新，原有引用继续有效。"),
                    "copy": tr("独立副本已创建，可点击切换。"),
                    "delete": tr("变体已删除，对应组合引用已改为 Mod 默认。"),
                }
                self.notification.emit(messages[mode], True)

            self._execute(
                mod_id,
                mode,
                variant_id=variant_id,
                name=name,
                use_defaults=use_defaults,
                on_success=succeeded,
                on_error=failed,
            )

        dialog.confirm_button.clicked.connect(lambda: submit(False))
        dialog.defaults_button.clicked.connect(lambda: submit(True))
        dialog.finished.connect(lambda _result: self._dialog_finished(dialog))
        dialog.open()
        if dialog.name_input is not None:
            dialog.name_input.setFocus()
            dialog.name_input.selectAll()

    def _dialog_finished(self, dialog):
        """仅在结束的是当前对话框时清除保活引用。"""
        if self._dialog is dialog:
            self._dialog = None
