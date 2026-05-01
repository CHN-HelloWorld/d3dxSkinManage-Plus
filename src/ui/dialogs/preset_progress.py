"""显示预设操作的真实进度，由所有者控制关闭。"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from src.ui import acrylic, icons, theme
from src.ui.widgets.rounded_progress import RoundedProgressBar
from src.utils.i18n import t as tr


def confirm_preset_delete(parent, presets, current_id):
    """破坏性操作必须显式选择，回车和退出键保留预设。"""
    box = QMessageBox(parent)
    box.setWindowTitle(tr("删除预设"))
    box.setIcon(QMessageBox.Icon.Warning)
    box.setTextFormat(Qt.TextFormat.PlainText)
    names = "\n".join(f"• {preset.name}" for preset in presets[:6])
    if len(presets) > 6:
        names += tr("\n…共 {count} 个预设", count=len(presets))
    box.setText(tr("确定删除以下预设？\n\n{names}", names=names))
    detail = tr("删除后无法恢复；Mod 资源仍会保留。")
    if any(preset.id == current_id for preset in presets):
        detail = tr("正在使用的预设将先切换到默认预设。\n") + detail
    box.setInformativeText(detail)
    remove = box.addButton(tr("删除预设"), QMessageBox.ButtonRole.DestructiveRole)
    cancel = box.addButton(tr("取消"), QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    box.exec()
    return box.clickedButton() is remove


class PresetProgressDialog(QDialog):
    """提交或回滚完成后才由所有者隐藏窗口。"""

    canceled = Signal()

    def __init__(self, target_name: str, parent=None, *, deleting=False) -> None:
        """创建切换或删除预设的非模态进度窗口，初始允许取消，最终隐藏由操作所有者负责。"""
        super().__init__(parent)
        self._deleting = deleting
        self.setObjectName("presetProgress")
        self.setWindowTitle(tr("删除预设") if deleting else tr("切换预设"))
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setMinimumWidth(360)
        self.resize(480, 322)
        self._phase = "prepare"
        self._can_cancel = True
        self._cancel_requested = False
        self._recovering = False
        self._setup_ui(target_name)
        self.retheme()
        self.show_phase("prepare", tr("正在准备删除预设…") if deleting else tr("正在检查部署并保存当前变体…"))
        acrylic.apply_dialog_acrylic(self, tint_alpha=0.76, animate=False)

    @staticmethod
    def _label(text: str = "", name: str = "") -> QLabel:
        """创建纯文本标签并设置可选对象名，避免预设名称被解释为富文本。"""
        label = QLabel(text)
        label.setTextFormat(Qt.TextFormat.PlainText)
        if name:
            label.setObjectName(name)
        return label

    def _setup_ui(self, target_name: str) -> None:
        """构建目标预设、三阶段指示、进度和取消操作，取消按钮只发送请求而不直接关闭。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 16, 22, 18)
        layout.setSpacing(16)

        target = QFrame()
        target.setObjectName("presetProgressTarget")
        target_layout = QHBoxLayout(target)
        target_layout.setContentsMargins(12, 10, 12, 10)
        target_layout.setSpacing(12)
        icon = self._label()
        icons.apply_pixmap(icon, "actions/remove" if self._deleting else "actions/refresh-circle", 28, "accent")
        icon.setFixedSize(32, 32)
        target_layout.addWidget(icon)
        target_text = QVBoxLayout()
        target_text.setSpacing(3)
        target_text.addWidget(self._label(tr("删除预设") if self._deleting else tr("切换到"), "presetProgressMuted"))
        self.target_label = self._label(target_name, "presetProgressName")
        self.target_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.target_label.setWordWrap(True)
        self.target_label.setToolTip(target_name)
        target_text.addWidget(self.target_label)
        target_layout.addLayout(target_text, 1)
        layout.addWidget(target)

        steps = QHBoxLayout()
        steps.setSpacing(7)
        self._step_labels = []
        for title in (tr("准备"), tr("删除"), tr("清理")) if self._deleting else (tr("检查"), tr("切换"), tr("完成")):
            step = QFrame()
            step.setObjectName("presetProgressStep")
            row = QHBoxLayout(step)
            row.setContentsMargins(8, 5, 8, 5)
            row.setSpacing(5)
            row.addStretch()
            number = self._label()
            number.setAlignment(Qt.AlignmentFlag.AlignCenter)
            number.setFixedSize(14, 16)
            row.addWidget(number)
            label = self._label(title)
            row.addWidget(label)
            row.addStretch()
            self._step_labels.append((step, number))
            steps.addWidget(step, 1)
        layout.addLayout(steps)

        progress = QVBoxLayout()
        progress.setSpacing(9)
        status = QHBoxLayout()
        self._phase_label = self._label("", "presetProgressPhase")
        status.addWidget(self._phase_label, 1)
        self.progress_label = self._label("", "presetProgressCount")
        status.addWidget(self.progress_label)
        progress.addLayout(status)
        self._bar = RoundedProgressBar()
        self._bar.setObjectName("presetProgressBar")
        self._bar.setAccessibleName(tr("预设切换进度"))
        self._bar.setRange(0, 0)
        self._bar.setFixedHeight(6)
        self._bar.setTextVisible(False)
        progress.addWidget(self._bar)
        self._detail_label = self._label("", "presetProgressMuted")
        self._detail_label.setWordWrap(True)
        self._detail_label.setMinimumHeight(34)
        self._detail_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        progress.addWidget(self._detail_label)
        layout.addLayout(progress)

        footer = QHBoxLayout()
        self._footer_label = self._label(tr("完成后自动关闭"), "presetProgressMuted")
        footer.addWidget(self._footer_label, 1)
        self.cancel_button = QPushButton(tr("取消删除") if self._deleting else tr("取消切换"))
        self.cancel_button.setObjectName("presetProgressCancel")
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.setFixedHeight(32)
        self.cancel_button.setMinimumWidth(88)
        self.cancel_button.clicked.connect(self._request_cancel)
        footer.addWidget(self.cancel_button)
        layout.addLayout(footer)

    def retheme(self) -> None:
        """更新局部样式及图标，并重新绘制当前阶段的完成和活动状态。"""
        self._theme_name = theme.current_theme()
        self.setStyleSheet(f"""
            QDialog#presetProgress {{ background: transparent; }}
            QDialog QLabel {{ background: transparent; border: 0; padding: 0;
                                            color: {theme.color("text.primary")}; }}
            QFrame#presetProgressTarget {{ background: {theme.rgba("accent", 0.065)};
                border: 1px solid {theme.rgba("accent", 0.12)}; border-radius: 8px; }}
            QLabel#presetProgressName {{ font-size: 17px; font-weight: 600; }}
            QLabel#presetProgressMuted, QLabel#presetProgressCount {{
                color: {theme.color("text.secondary")}; font-size: 12px; }}
            QLabel#presetProgressPhase {{ font-size: 14px; font-weight: 600; }}
            QFrame#presetProgressStep {{ border: 0; border-radius: 5px;
                background: {theme.rgba("bg.subtle", 0.6)}; }}
            QFrame#presetProgressStep QLabel {{ font-size: 12px; color: {theme.color("text.tertiary")}; }}
            QFrame#presetProgressStep[stepState="active"] {{ background: {theme.color("accent.subtle")}; }}
            QFrame#presetProgressStep[stepState="active"] QLabel {{
                font-weight: 600; color: {theme.color("accent")}; }}
            QFrame#presetProgressStep[stepState="complete"] QLabel {{ color: {theme.color("text.secondary")}; }}
            QProgressBar#presetProgressBar {{ border: 0; border-radius: 3px;
                background: {theme.color("bg.subtle")}; min-height: 6px; max-height: 6px; }}
            QProgressBar#presetProgressBar::chunk {{ border-radius: 3px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {theme.color("accent")}, stop:1 {theme.color("accent.hover")}); }}
            QPushButton#presetProgressCancel {{ min-height: 22px; max-height: 22px; padding: 4px 12px;
                color: {theme.color("text.primary")}; background: {theme.rgba("control.bg", 0.7)};
                border: 1px solid {theme.color("stroke.control")}; border-radius: 5px; }}
            QPushButton#presetProgressCancel:hover {{ background: {theme.color("bg.hover")}; }}
            QPushButton#presetProgressCancel:disabled {{ color: {theme.color("text.disabled")};
                background: transparent; border-color: {theme.color("stroke.card")}; }}
        """)
        icons.retheme_icons(self)
        self._refresh_steps()

    def changeEvent(self, event) -> None:
        """响应样式或调色板变化，在控件已初始化且主题发生变化时刷新外观。"""
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            if hasattr(self, "_step_labels") and getattr(self, "_theme_name", None) != theme.current_theme():
                self.retheme()

    def _refresh_steps(self) -> None:
        """将 prepare、deploy 或 finalize 映射到三阶段指示，并刷新动态属性样式。"""
        current = {"prepare": 0, "deploy": 1, "finalize": 2}[self._phase]
        for index, (step, number) in enumerate(self._step_labels):
            state = "complete" if index < current else "active" if index == current else "pending"
            if index < current:
                number.setPixmap(icons.pixmap("meta/check", 14, "accent"))
            else:
                number.setText(str(index + 1))
            step.setProperty("stepState", state)
            for widget in (step, *step.findChildren(QLabel)):
                widget.style().unpolish(widget)
                widget.style().polish(widget)
                widget.update()

    def show_phase(self, phase: str, detail: str) -> None:
        """显示指定任务阶段并重置为不确定进度；恢复期间忽略更新，未知阶段键抛出 KeyError。"""
        if self._recovering:
            return
        self._phase = phase
        titles = (
            {"prepare": tr("准备删除"), "deploy": tr("保存预设变更"), "finalize": tr("清理不再使用的缓存")}
            if self._deleting
            else {"prepare": tr("检查当前部署"), "deploy": tr("应用预设搭配"), "finalize": tr("完成同步")}
        )
        self._phase_label.setText(titles[phase])
        self.setRange(0, 0)
        self.setLabelText(detail)
        self._refresh_steps()

    def set_item_progress(self, index: int, total: int, verb: str, name: str, fraction: float) -> None:
        """用从一开始的项目序号和单项比例计算整体进度，最高显示 999/1000 以留出最终提交阶段。"""
        if self._recovering:
            return
        if self._phase != "deploy":
            self.show_phase("deploy", "")
        self.setLabelText(f"{verb} · {name}")
        if total <= 0:
            self.setRange(0, 0)
            return
        index = max(1, min(index, total))
        overall = ((index - 1) + max(0.0, min(1.0, fraction))) / total
        self.setRange(0, 1000)
        self.setValue(min(999, int(overall * 1000)))
        self.progress_label.setText(
            tr("{index} / {total} 项 · {value}%", index=index, total=total, value=self.value() // 10)
        )

    def setLabelText(self, text: str) -> None:
        """更新任务详情及工具提示；进入恢复状态后拒绝迟到的正常进度说明。"""
        if not self._recovering:
            self._detail_label.setText(text)
            self._detail_label.setToolTip(text)

    def labelText(self) -> str:
        """返回当前显示的详情文本，包括取消或回滚阶段的提示。"""
        return self._detail_label.text()

    def setRange(self, minimum: int, maximum: int) -> None:
        """设置进度条量程，相等端点清空计数文字；恢复期间保持不确定进度。"""
        if self._recovering:
            return
        self._bar.setRange(minimum, maximum)
        if minimum == maximum:
            self.progress_label.clear()
