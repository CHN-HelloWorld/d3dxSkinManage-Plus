"""用宿主主题展示按需修复进度与结果，目录扫描只在后台任务中执行。"""

from __future__ import annotations

import os
from functools import partial

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout, QWidget

from src.plugin_sdk import (
    BackgroundTask,
    DisposableStore,
    PluginContext,
    PluginError,
    TaskProgress,
    TaskResult,
)

from .permissions import RepairResult, repair_permissions


class RepairPanel(QWidget):
    """只在用户确认后修复旧私有目录，关闭窗口可停止后续工作，失败详情供手动重试。"""

    def __init__(self, ctx: PluginContext) -> None:
        """创建跟随宿主主题的控件，打开窗口时不访问待扫描目录。"""
        super().__init__()
        self.setObjectName("directoryPermissionsRepair")
        self._ctx = ctx
        self._root = ctx.plugin_dir.parent.parent
        self._closed = False
        self._task: BackgroundTask[RepairResult] | None = None
        self._styles = DisposableStore()
        self._task_subscriptions = DisposableStore()
        tr = ctx.i18n.t
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        description = self._style(
            QLabel(tr("仅修复旧版本留下的私有目录权限，不删除文件、不更改自定义访问限制。修复后可停用或卸载此插件。")),
            "muted",
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        root_label = self._style(QLabel(tr("应用数据目录：{path}", path=self._root)), "muted")
        root_label.setWordWrap(True)
        root_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(root_label)
        self.status_label = self._style(QLabel(tr("点击开始修复后才会检查目录。")), "subtitle")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.progress = self._style(QProgressBar(), "progress")
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        layout.addWidget(self.progress)
        self.details = self._style(QPlainTextEdit(), "input")
        self.details.setReadOnly(True)
        self.details.setPlaceholderText(tr("修复结果和失败路径会显示在这里。"))
        layout.addWidget(self.details, 1)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.close_button = self._style(QPushButton(tr("关闭")), "button")
        self.close_button.setAutoDefault(False)
        self.close_button.clicked.connect(self._close)
        buttons.addWidget(self.close_button)
        self.start_button = self._style(QPushButton(tr("开始修复")), "primary_button")
        self.start_button.clicked.connect(self._start)
        buttons.addWidget(self.start_button)
        layout.addLayout(buttons)
        if os.name != "nt":
            self.status_label.setText(tr("此修复仅适用于 Windows。"))
            self.start_button.setEnabled(False)

    def sizeHint(self) -> QSize:
        """为宿主首次显示提供横向首选尺寸，预留外层布局的边距。"""
        return QSize(728, 368)

    def configure_window(self, window) -> None:
        """按屏幕工作区限制尺寸，相对所属应用窗口居中并避免边框移出屏幕。"""
        parent = window.parentWidget()
        owner = parent.window() if parent is not None else None
        available = (owner.screen() if owner is not None else window.screen()).availableGeometry()
        preferred = self.sizeHint() + QSize(32, 32)
        window.resize(min(preferred.width(), available.width() - 40), min(preferred.height(), available.height() - 60))
        frame = window.frameGeometry()
        frame.moveCenter(owner.frameGeometry().center() if owner is not None else available.center())
        frame.moveLeft(max(available.left(), min(frame.left(), available.right() - frame.width() + 1)))
        frame.moveTop(max(available.top(), min(frame.top(), available.bottom() - frame.height() + 1)))
        window.move(frame.topLeft())

    def _style(self, widget, role: str):
        """登记语义样式句柄，由当前窗口在关闭时释放。"""
        self._styles.add(self._ctx.ui.apply_style(widget, role))
        return widget

    def _start(self) -> None:
        """向宿主提交纯路径参数，工作线程不持有插件上下文或 Qt 控件。"""
        if self._closed or os.name != "nt" or self._task is not None:
            return
        self._task_subscriptions.dispose()
        self._task_subscriptions = DisposableStore()
        self.start_button.setEnabled(False)
        self.details.clear()
        self.progress.setRange(0, 0)
        self.status_label.setText(self._ctx.i18n.t("正在检查目录权限…"))
        try:
            self._task = self._ctx.tasks.run(
                partial(repair_permissions, app_dir=self._root), title=self._ctx.i18n.t("目录权限修复")
            )
        except PluginError as error:
            self._completed(TaskResult(error=str(error)))
            return
        self._task_subscriptions.add(self._task.on_progress(self._progressed))
        self._task_subscriptions.add(self._task.on_finished(self._completed))

    def _progressed(self, progress: TaskProgress) -> None:
        """仅在宿主线程更新检查数量，不将未知总量伪装为完成百分比。"""
        if not self._closed and self._task is not None:
            self.status_label.setText(self._ctx.i18n.t("已检查 {count} 个目录…", count=progress.message))

    def _completed(self, result: TaskResult[RepairResult]) -> None:
        """显示完成、取消或失败结果，释放本次订阅并允许用户手动重试。"""
        self._task = None
        self._task_subscriptions.dispose()
        if self._closed:
            return
        tr = self._ctx.i18n.t
        self.start_button.setEnabled(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        if result.cancelled:
            message = tr("修复已取消，已完成的权限修复会保留，可重新运行。")
        elif not result.ok:
            message = tr("修复失败：{error}", error=result.error)
        else:
            value = result.value
            message = tr(
                "已检查 {scanned} 个目录，修复 {repaired} 个，失败 {errors} 个。",
                scanned=value.scanned,
                repaired=value.repaired,
                errors=len(value.errors),
            )
            self.progress.setValue(100)
            for error in value.errors:
                self._ctx.logger.warning("目录权限修复失败: %s", error)
            self.details.setPlainText("\n".join((message, *value.errors)))
        self.status_label.setText(message)
        if not result.ok:
            self.details.setPlainText(message)

    def _close(self) -> None:
        """关闭实际宿主弹窗，复用窗口关闭时的任务停止及资源清理路径。"""
        if not self._closed:
            self.window().close()

    def shutdown(self) -> None:
        """先切断回调，再取消任务和释放样式，重复关闭保持幂等。"""
        if self._closed:
            return
        self._closed = True
        self._task_subscriptions.dispose()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._styles.dispose()
