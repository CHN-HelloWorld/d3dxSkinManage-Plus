"""处理资源库中尚未纳管的遗留项。

主动停止留下的输入允许保留，供稍后继续；失败、重复和结构损坏项提供
回收站或永久删除操作，具体关闭行为由 allow_keep 控制并在界面说明。
这些输入已位于资源库中，保留后会在后续发现流程再次出现。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from src.ui import acrylic, anim, theme
from src.utils.i18n import N_
from src.utils.i18n import t as tr

#: 处置动作
ACTION_RECYCLE = "recycle_bin"
ACTION_DELETE = "permanent"
ACTION_KEEP = "keep"

# 按遗留原因分别说明，避免将重复内容误报为损坏。
REASON_STOPPED = "stopped"  # 用户主动停止，未开始处理
REASON_FAILED = "failed"  # 纳管失败（含损坏内容）
REASON_DUPLICATE = "duplicate"  # 与库中已有内容重复
REASON_BROKEN = "broken"  # 容器标记无法解析，发现时即拒绝纳管。

#: (窗口标题, 概述句式, 说明) —— 概述里的 {n} 由调用处格式化。
_REASON_TEXT = {
    REASON_STOPPED: (
        N_("尚未加入 Mod 库"),
        N_("尚有 {n} 个 Mod 未加入 Mod 库"),
        N_("这些文件仍位于 Mod 库文件夹中。选择保留后，下次刷新时会再次尝试加入 Mod 库。"),
    ),
    REASON_FAILED: (
        N_("无法加入 Mod 库"),
        N_("有 {n} 个 Mod 未能加入 Mod 库"),
        N_("这些文件仍位于 Mod 库文件夹中，刷新时会再次尝试加入。请选择处理方式。"),
    ),
    REASON_DUPLICATE: (
        N_("重复的 Mod"),
        N_("有 {n} 个 Mod 与库中已有内容重复"),
        N_("Mod 库中已有相同内容。请选择如何处理这份重复的副本，库中已有的 Mod 不受影响。"),
    ),
    REASON_BROKEN: (
        N_("无法识别的 Mod"),
        N_("检测到 {n} 个无法识别的 Mod"),
        N_("Mod 信息已损坏，无法识别此 Mod。请选择处理方式。"),
    ),
}


class LeftoverIntakeDialog(QDialog):
    """询问单个遗留项如何处置，可勾选「后续项保持相同选择」。"""

    def __init__(
        self,
        name: str,
        *,
        remaining: int,
        total: int,
        allow_apply_all: bool,
        allow_keep: bool = True,
        reason: str = REASON_STOPPED,
        parent=None,
    ) -> None:
        """构建当前遗留项的处置选择。

        允许保留时关闭默认保留，否则默认永久删除；窗口只记录决定，文件操作由调用方执行。
        """
        super().__init__(parent)
        # 不允许保留的场景中，关闭或按 Esc 均采用界面已说明的永久删除决定。
        self._action = ACTION_KEEP if allow_keep else ACTION_DELETE
        self._apply_all = False
        anim.install_intro_fade(self)
        title, summary, detail = _REASON_TEXT.get(reason, _REASON_TEXT[REASON_STOPPED])
        title, detail = tr(title), tr(detail)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(10)

        # 剩余数量使用 total 减 index，包含当前项，须在提示中明确范围。
        headline = tr(summary, n=remaining)
        if total > 1:
            headline += tr("（含当前项，本批共 {total} 个）", total=total)
        lead = QLabel(f"{headline}。{detail}")
        lead.setWordWrap(True)
        lead.setStyleSheet(f"color:{theme.color('text.primary')}; font-size:13px;")
        root.addWidget(lead)

        current = QLabel(tr("当前项：{name}", name=name))
        current.setTextFormat(Qt.TextFormat.PlainText)
        current.setWordWrap(True)
        current.setStyleSheet(f"color:{theme.color('text.secondary')}; font-size:12px;")
        root.addWidget(current)

        consequence = QLabel(
            tr("永久删除不会移入回收站，无法恢复。")
            + (tr("关闭窗口或按 Esc 将保留当前项。") if allow_keep else tr("关闭窗口或按 Esc 也会永久删除当前项。"))
        )
        consequence.setWordWrap(True)
        consequence.setStyleSheet(f"color:{theme.color('text.secondary')}; font-size:12px;")
        root.addWidget(consequence)

        self._apply_all_box = QCheckBox(tr("对后续各项采用相同处置方式"))
        self._apply_all_box.setStyleSheet(f"color:{theme.color('text.secondary')}; font-size:12px;")
        self._apply_all_box.setVisible(allow_apply_all and remaining > 1)
        root.addWidget(self._apply_all_box)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch()
        choices = [
            (tr("放入回收站"), ACTION_RECYCLE, False),
            (tr("永久删除"), ACTION_DELETE, True),
        ]
        if allow_keep:
            choices.append((tr("保留"), ACTION_KEEP, False))
        for label, action, danger in choices:
            btn = QPushButton(label)
            btn.setFixedHeight(32)
            btn.setMinimumWidth(92)
            if danger:
                btn.setProperty("btnColor", "danger")
            elif action == ACTION_RECYCLE:
                btn.setProperty("accent", True)
            btn.clicked.connect(lambda _checked=False, a=action: self._choose(a))
            row.addWidget(btn)
        root.addLayout(row)

        acrylic.apply_dialog_acrylic(self)

    def _choose(self, action: str) -> None:
        """保存显式选择及是否应用到后续项目，然后接受对话框。"""
        self._action = action
        self._apply_all = self._apply_all_box.isChecked()
        self.accept()

    @property
    def action(self) -> str:
        """返回选定动作；直接关闭时返回构造阶段设定的默认动作。"""
        return self._action

    @property
    def apply_to_all(self) -> bool:
        """返回是否显式要求后续项目沿用本次选择；直接关闭时保持为假。"""
        return self._apply_all


def resolve_leftover_paths(
    paths: list[Path],
    parent=None,
    *,
    allow_keep: bool = True,
    reason: str = REASON_STOPPED,
) -> dict[str, str]:
    """逐项收集处置决定，返回输入路径的字符串与动作的映射。

    勾选应用到后续项目后复用该动作；关闭时按 allow_keep 决定保留或永久删除。
    本函数不执行删除，也不解析或规范化路径。
    """
    decisions: dict[str, str] = {}
    sticky: str | None = None
    total = len(paths)
    for index, path in enumerate(paths):
        remaining = total - index
        if sticky is not None:
            decisions[str(path)] = sticky
            continue
        dlg = LeftoverIntakeDialog(
            Path(path).name,
            remaining=remaining,
            total=total,
            allow_apply_all=True,
            allow_keep=allow_keep,
            reason=reason,
            parent=parent,
        )
        dlg.exec()
        decisions[str(path)] = dlg.action
        if dlg.apply_to_all:
            sticky = dlg.action
    return decisions
