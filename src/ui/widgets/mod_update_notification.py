"""更新进度与导入、导出共用通知容器。"""

from __future__ import annotations

from collections import Counter

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from src.ui import icons, theme
from src.ui.dialogs.mod_update_row import format_size
from src.ui.dialogs.mod_update_task_progress import (
    ACTIVE_PHASES,
    PHASE_ICONS,
    PHASE_LABELS,
    TaskProgressBar,
    finite_number,
    progress_fraction,
)
from src.ui.widgets.task_center import (
    ST_DONE,
    ST_FAILED,
    ST_QUEUED,
    ST_ROLLED_BACK,
    ST_RUNNING,
    TaskCenterPanel,
    TaskCenterStack,
    _ElideLabel,
)
from src.utils.i18n import t as tr


class ModUpdateNotification(TaskCenterPanel):
    """在任务中心展示后台 Mod 更新汇总、下载进度和查看更新入口。"""

    view_requested = Signal()
    dismiss_requested = Signal()

    def __init__(self, parent: QWidget, *, stack: TaskCenterStack) -> None:
        """初始化更新任务归属与结束状态，在任务通知卡片中添加汇总进度和查看、完成操作。"""
        self.game_id = ""
        self._task_keys: tuple = ()
        self._terminal = False
        self._dismissing = False
        super().__init__(parent, stack=stack)
        self.setAccessibleName(tr("后台 Mod 更新通知"))
        self._ring.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._summary_area = QWidget()
        self._summary_area.setObjectName("updateNoticeSummary")
        summary = QVBoxLayout(self._summary_area)
        summary.setContentsMargins(12, 0, 12, 10)
        summary.setSpacing(5)
        self._counts = _ElideLabel()
        self._transfer = _ElideLabel()
        self._download_bar = TaskProgressBar()
        summary.addWidget(self._counts)
        summary.addWidget(self._transfer)
        summary.addWidget(self._download_bar)
        self.layout().insertWidget(1, self._summary_area)
        footer = QFrame()
        footer.setObjectName("updateNoticeFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(10, 6, 10, 7)
        self._view_btn = QPushButton(tr("查看更新"))
        self._view_btn.setFlat(True)
        self._view_btn.setIconSize(QSize(13, 13))
        self._view_btn.clicked.connect(self.view_requested)
        footer_layout.addWidget(self._view_btn)
        footer_layout.addStretch()
        self._finish_btn = QPushButton(tr("完成"))
        self._finish_btn.setIconSize(QSize(13, 13))
        self._finish_btn.clicked.connect(self.dismiss_requested)
        self._finish_btn.hide()
        footer_layout.addWidget(self._finish_btn)
        self.layout().addWidget(footer)
        self.retheme()

    def present(self, game_id: str, snapshots: dict[str, dict], names: dict[str, str]) -> None:
        """显示服务快照，主窗口控制通知可见性。"""
        if not snapshots:
            self.dismiss(animate=False)
            return
        active = [s for s in snapshots.values() if s.get("phase") in ACTIVE_PHASES]
        keys = tuple((mid, snapshot.get("file_name", "")) for mid, snapshot in snapshots.items())
        if self.game_id != game_id or self._task_keys != keys or (active and self._terminal):
            self.game_id = game_id
            self._task_keys = keys
            self._terminal = False
            self.start_batch(
                len(snapshots),
                tr("正在后台更新"),
                has_pause=True,
                stop_tooltip=tr("终止全部更新"),
                cancelled_title=tr("更新已终止"),
                done_text=tr("已更新"),
            )
        elif self._dismissing or not self.isVisible():
            self._slide_in()
        self._dismissing = False
        counts = Counter(snapshot.get("phase", "queued") for snapshot in snapshots.values())
        done, total = counts["done"], len(snapshots)
        for index, (mid, snapshot) in enumerate(snapshots.items()):
            phase = snapshot.get("phase", "queued")
            status = (
                ST_DONE
                if phase == "done"
                else ST_FAILED
                if phase == "failed"
                else ST_ROLLED_BACK
                if phase in {"canceled", "deferred"}
                else ST_QUEUED
                if phase in {"queued", "paused"}
                else ST_RUNNING
            )
            detail = PHASE_LABELS.get(phase, tr("正在处理"))
            fraction = progress_fraction(snapshot)
            if phase in {"downloading", "verifying", "extracting", "installing"} and fraction is not None:
                detail += f" {fraction:.0%}"
            self.update_item(index, names.get(mid, mid), status, detail, icon=PHASE_ICONS.get(phase, "mod-updates/box"))
            self._items[index].setToolTip(snapshot.get("error") or snapshot.get("detail") or detail)
        self._counts.setText(" · ".join(f"{PHASE_LABELS.get(phase, phase)} {count}" for phase, count in counts.items()))
        self._counts.setToolTip(self._counts.full_text())
        complete = done == total
        if active:
            pausable = [s for s in active if s.get("phase") != "stopping"]
            paused = bool(pausable) and all(s.get("phase") == "paused" for s in pausable)
            title = tr("更新已暂停") if paused else tr("正在后台更新") if pausable else tr("正在终止更新")
            # 圆环统计已完成项，不用下载字节伪造文件处理进度。
            self.update_progress(
                done / total * 100, title, tr("已更新 {done} / {total} 个 Mod", done=done, total=total)
            )
            self.set_paused(paused)
            self._ring.setEnabled(bool(pausable))
            self._ring.setAccessibleName(tr("继续全部更新") if paused else tr("暂停全部更新"))
            self.set_stop_enabled(bool(pausable))
            self._stop_btn.show()
            self._finish_btn.hide()
            self._transfer.show()
            self._download_bar.show()
            downloaded = sum(max(0, finite_number(s.get("downloaded_bytes"))) for s in snapshots.values())
            sizes = [max(0, finite_number(s.get("total_bytes"))) for s in snapshots.values()]
            size = sum(sizes) if all(sizes) else 0
            speed = sum(max(0, finite_number(s.get("speed_bps"))) for s in active)
            transfer = (
                tr("总下载 {value} / {value_2}", value=format_size(downloaded), value_2=format_size(size))
                if size
                else tr("已下载 {value} · 总大小未知", value=format_size(downloaded))
            )
            if speed:
                transfer += f" · {format_size(speed)}/s"
            self._transfer.setText(transfer)
            self._transfer.setToolTip(transfer)
            self._download_bar.set_measurement(
                min(1.0, downloaded / size) if size else None,
                running=any(s.get("phase") == "downloading" for s in active),
                tone="muted" if paused else "accent",
            )
        elif not self._terminal:
            self._terminal = True
            title = (
                tr("全部更新完成")
                if complete
                else tr("部分更新未完成")
                if counts["failed"] or counts["deferred"]
                else tr("更新已终止")
            )
            self.finish(title, tr("已更新 {done} / {total} 个 Mod", done=done, total=total), ok=complete)
            # 结果保留到用户查看或明确关闭。
            self._hide_timer.stop()
            self._disarm_focus_dismiss()
            self._has_pause = False
            self._restore_expand_btn()
            self._expand_btn.set_icon(
                "actions/chevron-up" if self._is_expanded else "actions/chevron-down", "text.secondary"
            )
            self._expand_btn.setToolTip(tr("收起明细") if self._is_expanded else tr("展开明细"))
            self._finish_btn.setText(tr("完成") if complete else tr("关闭通知"))
            icons.apply_icon(
                self._finish_btn,
                "mod-updates/check" if complete else "mod-updates/close",
                "success" if complete else "text.secondary",
            )
            self._finish_btn.show()
            self._transfer.hide()
            self._download_bar.hide()
        self._view_btn.setText(tr("查看结果") if not active else tr("查看更新"))
        self._on_list_anim_frame()
        self._stack.fit_details(animate=False)

    def dismiss(self, *, animate: bool = True) -> None:
        """隐藏通知，不修改任务或确认结果。"""
        self._hide_timer.stop()
        self._disarm_focus_dismiss()
        self._stop_list_anim()
        self._dismissing = True
        if animate and self.isVisible():
            self._slide_out()
        else:
            self._stop_current_anim()
            self._on_slide_out_done()

    def set_paused(self, paused: bool) -> None:
        """同步基础任务暂停状态，并在支持暂停时更新进度环图标及颜色。"""
        super().set_paused(paused)
        if self._has_pause:
            token = "text.secondary" if paused else "accent"
            self._ring.set_icon("actions/play" if paused else "actions/pause", token)
            self._ring.set_ring_color(token)

    def _on_ring_clicked(self) -> None:
        """在任务支持暂停时，按当前暂停状态发出恢复或暂停请求。"""
        if self._has_pause:
            # 服务同步发布实际状态，包括部分暂停失败，不先显示乐观结果。
            if self._paused:
                self.resume_requested.emit()
            else:
                self.pause_requested.emit()

    def _apply_style(self) -> None:
        """在任务卡片基础样式上补充更新汇总区和底部操作区的透明背景及分隔线。"""
        super()._apply_style()
        self.setStyleSheet(
            self.styleSheet()
            + (
                "#updateNoticeSummary { background:transparent; }"
                f"#updateNoticeFooter "
                f"{{ background:transparent; "
                f"border-top:1px solid {theme.color('stroke.divider')}; "
                f"}}"
            )
        )

    def retheme(self) -> None:
        """同步基础任务卡片主题，再刷新汇总文字、查看入口和底部操作按钮外观。"""
        super().retheme()
        for label in (self._counts, self._transfer):
            label.setStyleSheet(f"color:{theme.color('text.secondary')}; font-size:11px; background:transparent;")
        icons.apply_icon(self._view_btn, "actions/list-view", "accent")
        self._view_btn.setStyleSheet(f"color:{theme.color('accent')}; font-size:11px; padding:3px;")
        self._finish_btn.setStyleSheet("font-size:11px; padding:3px 9px;")
        self._stop_btn.retheme()
        self._expand_btn.retheme()
