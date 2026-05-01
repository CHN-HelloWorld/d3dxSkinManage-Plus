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
