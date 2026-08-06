"""逐项执行删除并显示进度。

通过定时器分派主线程操作，SQLite 访问保持在连接所属线程；
已启用 Mod 的删除由对话框确认后执行。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.ui import acrylic, anim, theme
from src.utils import fs
from src.utils.i18n import t as tr


def delete_mod_with_fallback(
    parent: QWidget,
    mod_manager,
    mod_id: str,
    delete_method: str,
) -> bool:
    """删除单个 Mod，回收站失败时显式确认永久删除。"""
    method = delete_method if delete_method in ("permanent", "recycle_bin") else "permanent"
    try:
        return bool(mod_manager.delete_mod(mod_id, delete_method=method))
    except fs.RecycleBinError as error:
        mod = mod_manager.get_mod(mod_id)
        mod_name = mod.display_name if mod is not None else mod_id
        if error.path_too_long:
            reason = tr(
                "“{mod_name}”的路径或其中的文件路径过长，Windows 回收站不支持该路径，因此无法放入回收站。",
                mod_name=mod_name,
            )
        else:
            reason = tr("系统无法将“{mod_name}”放入回收站。该磁盘或当前位置可能不支持回收站。", mod_name=mod_name)
        reply = QMessageBox.question(
            parent,
            tr("无法放入回收站"),
            reason + tr("\n\n是否改为直接永久删除？此操作无法恢复。"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return False
        return bool(mod_manager.delete_mod(mod_id, delete_method="permanent"))


class DeleteProgressDialog(QDialog):
    """删除进度对话框

    使用 QTimer 在主线程中逐项删除，保证 SQLite 线程安全。
    遇到已启用的 Mod 时弹出决策对话框。
    """

    deletion_finished = Signal(int)  # 删除完成的数量

    def __init__(
        self,
        mod_ids: list[str],
        mod_manager,
        parent: QWidget | None = None,
        *,
        delete_method: str = "permanent",
    ) -> None:
        """保存管理器和删除策略，将未启用项目排在前面，并创建由对话框持有的主线程定时器。"""
        super().__init__(parent)
        anim.install_intro_fade(self)
        self._mod_manager = mod_manager
        self._delete_method = delete_method if delete_method in ("permanent", "recycle_bin") else "permanent"
        self._action_text = tr("移入回收站") if self._delete_method == "recycle_bin" else tr("删除")
        self._deleted_count = 0
        self._is_paused = False
        self._is_cancelled = False
        self._apply_to_all = False
        self._apply_to_all_decision = ""  # 保存 delete 或 keep 决定，空值表示尚未选择。
        self._timer = QTimer(self)
        self._timer.setInterval(10)  # 每项间隔 10 毫秒。

        # 重新排序：未启用的在前，已启用的在后
        disabled_ids = []
        enabled_ids = []
        for mid in mod_ids:
            mod = mod_manager.get_mod(mid)
            if mod and mod.is_enabled:
                enabled_ids.append(mid)
            else:
                disabled_ids.append(mid)
        self._mod_ids = disabled_ids + enabled_ids
        self._enabled_start_index = len(disabled_ids)  # 已启用 Mod 在列表中的起始索引

        self._current_index = 0
        self._timer.timeout.connect(self._process_next)
        self._setup_ui()
        acrylic.apply_dialog_acrylic(self)

    def _setup_ui(self):
        """构建批量删除进度与暂停操作，取消按钮仅在暂停后显示。"""
        self.setWindowTitle(f"{self._action_text} Mod")
        self.setFixedSize(500, 200)
        self.setModal(True)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowCloseButtonHint)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 16, 20, 16)

        # 标题
        self._title_label = QLabel(
            tr("正在{action_text} {count} 个 Mod...", action_text=self._action_text, count=len(self._mod_ids))
        )
        self._title_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        layout.addWidget(self._title_label)

        # 进度条
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, len(self._mod_ids))
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        layout.addWidget(self._progress_bar)

        # 细节标签
        self._detail_label = QLabel(tr("准备中..."))
        self._detail_label.setStyleSheet(f"color: {theme.color('text.tertiary')}; font-size: 11px;")
        self._detail_label.setWordWrap(True)
        layout.addWidget(self._detail_label)

        # 按钮区
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self._pause_btn = QPushButton(tr("暂停"))
        self._pause_btn.setFixedSize(80, 32)
        self._pause_btn.clicked.connect(self._on_pause)
        btn_layout.addWidget(self._pause_btn)

        self._resume_btn = QPushButton(tr("继续"))
        self._resume_btn.setFixedSize(80, 32)
        self._resume_btn.setProperty("accent", True)
        self._resume_btn.clicked.connect(self._on_resume)
        self._resume_btn.setVisible(False)
        btn_layout.addWidget(self._resume_btn)

        self._cancel_btn = QPushButton(tr("取消"))
        self._cancel_btn.setFixedSize(80, 32)
        self._cancel_btn.setStyleSheet(
            f"QPushButton {{ background-color: {theme.color('danger')}; color: white; "
            f"border: none; border-radius: {theme.radius('control')}px; font-size: 12px; }}"
        )
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._cancel_btn.setVisible(False)
        btn_layout.addWidget(self._cancel_btn)

        layout.addLayout(btn_layout)

    def start_deletion(self):
        """启动主线程定时器，逐项处理待删除列表。"""
        self._timer.start()

    def _process_next(self):
        """在管理器所属线程处理一项并推进索引；启用项先征求决定，列表耗尽后报告成功数。"""
        if self._is_paused or self._is_cancelled:
            return

        if self._current_index >= len(self._mod_ids):
            self._timer.stop()
            self._detail_label.setText(tr("{action_text}完成", action_text=self._action_text))
            self._title_label.setText(
                tr(
                    "{action_text}完成 {deleted_count}/{count}",
                    action_text=self._action_text,
                    deleted_count=self._deleted_count,
                    count=len(self._mod_ids),
                )
            )
            self.deletion_finished.emit(self._deleted_count)
            self.accept()
            return

        mod_id = self._mod_ids[self._current_index]
        total = len(self._mod_ids)
        i = self._current_index

        mod = self._mod_manager.get_mod(mod_id)
        if not mod:
            self._current_index += 1
            return

        # 更新进度 UI
        self._progress_bar.setValue(i)
        pct = int(i / total * 100) if total > 0 else 0
        self._detail_label.setText(
            tr(
                "正在{action_text}: {display_name} ({value}/{total})",
                action_text=self._action_text,
                display_name=mod.display_name,
                value=i + 1,
                total=total,
            )
        )
        self._title_label.setText(
            tr("正在{action_text} {i}/{total} ({pct}%)", action_text=self._action_text, i=i, total=total, pct=pct)
        )

        # 到达已启用 Mod 区段，且尚未获得用户决策
        if mod.is_enabled and not self._apply_to_all:
            self._timer.stop()
            self._ask_enabled_mod_decision(mod_id, mod.display_name)
            return

        # 已启用的 Mod 且用户选择"保留"
        if mod.is_enabled and self._apply_to_all_decision == "keep":
            self._current_index += 1
            return

        # 执行删除
        if self._delete_mod(mod_id):
            self._deleted_count += 1

        self._current_index += 1

    def _ask_enabled_mod_decision(self, mod_id: str, mod_name: str):
        """询问当前启用项是否删除，可复用决定到后续项目；处理当前项后恢复定时器。"""
        # 计算剩余已启用的 Mod 数量
        remaining_enabled = len(self._mod_ids) - self._enabled_start_index
        # 减去已经跳过/处理的
        remaining_enabled = max(0, remaining_enabled - (self._current_index - self._enabled_start_index))

        dialog = QMessageBox(self)
        dialog.setWindowTitle(tr("确认{action_text}", action_text=self._action_text))
        dialog.setIcon(QMessageBox.Icon.Question)
        # 仅剩一个启用项时使用单项提示，避免数量含义不明确。
        if remaining_enabled <= 1:
            text = tr("「{mod_name}」是已启用的 Mod，尚未处理，您希望如何做？", mod_name=mod_name)
        else:
            text = tr(
                "还有「{mod_name}」等 {remaining_enabled} 个已启用的 Mod 未处理，您希望如何做？",
                mod_name=mod_name,
                remaining_enabled=remaining_enabled,
            )
        dialog.setText(text)

        delete_btn = dialog.addButton(self._action_text, QMessageBox.ButtonRole.DestructiveRole)
        keep_btn = dialog.addButton(tr("保留"), QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(keep_btn)

        apply_all_cb = QCheckBox(tr("为后续项执行相同选择"))
        dialog.setCheckBox(apply_all_cb)

        dialog.exec()

        decision = "delete" if dialog.clickedButton() == delete_btn else "keep"
        apply_to_all = apply_all_cb.isChecked()

        if apply_to_all:
            self._apply_to_all = True
            self._apply_to_all_decision = decision

        if decision == "delete":
            if self._delete_mod(mod_id):
                self._deleted_count += 1
        # 保留并跳过当前项。

        self._current_index += 1
        # 恢复逐项调度。
        self._timer.start()

    def _delete_mod(self, mod_id: str) -> bool:
        """暂停批处理定时器以避免确认框重入，返回删除结果，并在未取消时恢复原调度状态。"""
        # 确认框运行嵌套事件循环，先暂停批处理定时器，避免同一 Mod 重入并重复提示。
        resume_timer = self._timer.isActive()
        if resume_timer:
            self._timer.stop()
        try:
            return delete_mod_with_fallback(
                self,
                self._mod_manager,
                mod_id,
                self._delete_method,
            )
        finally:
            if resume_timer and not self._is_cancelled:
                self._timer.start()

    def _on_pause(self):
        """停止后续项目的定时调度并显示继续和取消操作，不回滚已完成删除。"""
        self._is_paused = True
        self._timer.stop()
        self._pause_btn.setVisible(False)
        self._resume_btn.setVisible(True)
        self._cancel_btn.setVisible(True)
        self._detail_label.setText(
            tr('已暂停 - 点击"继续"恢复{action_text}，或点击"取消"停止', action_text=self._action_text)
        )

    def _on_resume(self):
        """清除暂停状态并恢复逐项删除，隐藏仅在暂停期间提供的取消按钮。"""
        self._is_paused = False
        self._pause_btn.setVisible(True)
        self._resume_btn.setVisible(False)
        self._cancel_btn.setVisible(False)
        self._timer.start()

    def _on_cancel(self):
        """确认后停止批处理、报告已删除数量并拒绝对话框，已删除项目保持原处置结果。"""
        consequence = (
            tr("已移入回收站的 Mod 可从系统回收站恢复。")
            if self._delete_method == "recycle_bin"
            else tr("已删除的 Mod 无法恢复。")
        )
        reply = QMessageBox.question(
            self,
            tr("确认取消"),
            tr("确定要取消吗？{consequence}", consequence=consequence),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )

        if reply == QMessageBox.StandardButton.Yes:
            self._is_cancelled = True
            self._timer.stop()
            self._detail_label.setText(tr("{action_text}已取消", action_text=self._action_text))
            self.deletion_finished.emit(self._deleted_count)
            self.reject()

    def closeEvent(self, event):
        """定时器仍在运行时拒绝窗口关闭，停止调度后交由 Qt 处理。"""
        if self._timer.isActive():
            event.ignore()
        else:
            super().closeEvent(event)
