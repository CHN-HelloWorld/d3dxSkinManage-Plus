from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPoint, QRect
from PySide6.QtWidgets import QApplication

from src.ui import theme
from src.ui.dialogs.mod_updates_dialog import ModUpdatesDialog
from tests.ui.test_mod_updates_dialog import _info, keep_dialog


def snapshot(phase="downloading", **changes):
    value = {
        "phase": phase,
        "resume_phase": "downloading",
        "downloaded_bytes": 2 * 1024 * 1024,
        "total_bytes": 8 * 1024 * 1024,
        "speed_bps": 1024 * 1024,
        "eta_seconds": 6,
        "stage_progress": None,
        "file_name": "costume-v2.zip",
        "detail": "",
        "error": "",
    }
    value.update(changes)
    return value


def show_dialog(qtbot, infos=None):
    dialog = ModUpdatesDialog(
        infos if infos is not None else [_info("m1"), _info("m2", "示例角色午西装")], enable_actions=True
    )
    keep_dialog(qtbot, dialog)
    dialog._sized_for_screen = True
    dialog.show()
    assert callable(getattr(dialog, "set_task_snapshot", None)), "更新弹窗尚未接收真实任务快照"
    return dialog


def test_download_snapshot_displays_measured_bytes_speed_and_eta(qtbot):
    dialog = show_dialog(qtbot)
    dialog.set_task_snapshot("m1", snapshot())
    row = dialog._rows_by_id["m1"]

    assert row.phase == "downloading" and row.state == "updating"
    assert "下载" in row.task_phase.text()
    assert row.progress.maximum() == 1000 and row.progress.value() == 250
    assert row.task_percent.text() == "25%"
    assert "2.0 MB / 8.0 MB" in row.task_metrics.text()
    assert "1.0 MB/s" in row.task_metrics.text() and "6 秒" in row.task_metrics.text()
    assert row.task_file.text() == "costume-v2.zip"
    assert not row.update_button.isEnabled()
    assert not row.update_button.isVisibleTo(dialog)
    assert row.pause_button.isVisibleTo(dialog) and row.stop_button.isVisibleTo(dialog)
    assert not row.task_icon.pixmap().isNull() and not row.pause_button._pix.isNull()


def test_download_without_total_never_invents_a_percentage(qtbot):
    dialog = show_dialog(qtbot)
    dialog.set_task_snapshot("m1", snapshot(total_bytes=0, eta_seconds=None, stage_progress=0.9))
    row = dialog._rows_by_id["m1"]

    assert row.progress.maximum() == 0
    assert row.task_percent.text() == ""
    assert "2.0 MB" in row.task_metrics.text()
    assert "/ 0" not in row.task_metrics.text() and "秒" not in row.task_metrics.text()
    assert row.pause_button._indeterminate


@pytest.mark.parametrize("phase,label", [("verifying", "校验"), ("extracting", "解压"), ("installing", "安装")])
def test_file_stage_does_not_reuse_the_completed_download_percentage(qtbot, phase, label):
    dialog = show_dialog(qtbot)
    dialog.set_task_snapshot(
        "m1", snapshot(phase, downloaded_bytes=8 * 1024 * 1024, stage_progress=None, detail="正在处理 <texture.dds>")
    )
    row = dialog._rows_by_id["m1"]

    assert label in row.task_phase.text()
    assert row.progress.maximum() == 0 and row.task_percent.text() == ""
    assert row.task_detail.text() == "正在处理 <texture.dds>"
    assert row.task_metrics.text() == ""

    dialog.set_task_snapshot("m1", snapshot(phase, stage_progress=0.4, completed_files=4, total_files=10))
    assert row.progress.maximum() == 1000 and row.progress.value() == 400
    assert row.task_percent.text() == "40%"
    assert "4 / 10" in row.task_metrics.text()
    assert "/s" not in row.task_metrics.text()


@pytest.mark.parametrize(
    "phase,label,controls,retry",
    [
        ("queued", "排队", True, False),
        ("preparing", "准备下载", True, False),
        ("downloading", "下载", True, False),
        ("verifying", "校验", True, False),
        ("extracting", "解压", True, False),
        ("installing", "安装", True, False),
        ("paused", "已暂停", True, False),
        ("stopping", "正在终止", True, False),
        ("canceled", "已终止", False, True),
        ("deferred", "等待继续", False, True),
        ("done", "已更新", False, False),
        ("failed", "更新失败", False, True),
    ],
)
def test_each_phase_exposes_only_valid_row_actions(qtbot, phase, label, controls, retry):
    dialog = show_dialog(qtbot)
    dialog.set_task_snapshot("m1", snapshot(phase, error="连接中断" if phase == "failed" else ""))
    row = dialog._rows_by_id["m1"]

    assert row.phase == phase and label in row.task_phase.text()
    assert row.pause_button.isVisibleTo(dialog) == controls
    assert row.stop_button.isVisibleTo(dialog) == controls
    assert row.update_button.isEnabled() == retry
    if controls:
        assert row.pause_button.isEnabled() == (phase != "stopping")
        assert row.stop_button.isEnabled() == (phase != "stopping")
    if phase == "done":
        assert not row.progress.isVisibleTo(dialog) and dialog.pending_ids() == ["m2"]
    if phase == "failed":
        assert row.task_detail.text() == "连接中断"


def test_pause_resume_and_stop_emit_the_selected_mod_without_predicting_state(qtbot):
    dialog = show_dialog(qtbot)
    received = []
    dialog.pause_update_requested.connect(lambda mid: received.append(("pause", mid)))
    dialog.resume_update_requested.connect(lambda mid: received.append(("resume", mid)))
    dialog.stop_update_requested.connect(lambda mid: received.append(("stop", mid)))
    dialog.set_task_snapshot("m2", snapshot())
    row = dialog._rows_by_id["m2"]

    row.pause_button.click()
    row.stop_button.click()
    assert received == [("pause", "m2"), ("stop", "m2")]
    assert row.phase == "downloading", "UI 必须等待服务确认，不能自行把任务标为暂停或终止"

    dialog.set_task_snapshot("m2", snapshot("paused"))
    row.pause_button.click()
    assert received[-1] == ("resume", "m2")
    assert row.phase == "paused"


def test_paused_transfer_retains_measurement_and_stops_live_feedback(qtbot):
    dialog = show_dialog(qtbot)
    dialog.set_task_snapshot("m1", snapshot("paused"))
    row = dialog._rows_by_id["m1"]

    assert row.progress.value() == 250
    assert "2.0 MB / 8.0 MB" in row.task_metrics.text()
    assert "/s" not in row.task_metrics.text() and "秒" not in row.task_metrics.text()
    assert not row.pause_button._indeterminate
    assert "继续" in row.pause_button.accessibleName()
    assert "下载" in row.task_phase.text()

    dialog.set_task_snapshot("m1", snapshot("paused", resume_phase="extracting", stage_progress=None))
    assert "解压" in row.task_phase.text()
    assert row.progress.maximum() == 0 and not row.progress.is_animating()
    assert row.task_percent.text() == ""


def test_batch_controls_include_hidden_tasks_and_emit_independent_actions(qtbot):
    dialog = show_dialog(
        qtbot, [_info("m1"), _info("m2", "示例角色午西装"), _info("m3", "已完成"), _info("m4", "未开始")]
    )
    dialog.set_task_snapshot("m1", snapshot())
    dialog.set_task_snapshot("m2", snapshot("paused"))
    dialog.set_task_snapshot("m3", snapshot("done"))
    dialog._search.setText("未开始")
    received = []
    dialog.pause_all_requested.connect(lambda: received.append("pause"))
    dialog.resume_all_requested.connect(lambda: received.append("resume"))
    dialog.stop_all_requested.connect(lambda: received.append("stop"))

    assert dialog._rows_by_id["m1"].isHidden() and dialog._rows_by_id["m2"].isHidden()
    assert dialog._batch_panel.isVisibleTo(dialog)
    assert "下载 1" in dialog._batch_counts.text()
    assert "暂停 1" in dialog._batch_counts.text()
    assert "已更新 1" in dialog._batch_counts.text()
    for control in (dialog._pause_all_btn, dialog._resume_all_btn, dialog._stop_all_btn):
        assert control.isEnabled()
        control.click()
    assert received == ["pause", "resume", "stop"]
    assert dialog._rows_by_id["m1"].phase == "downloading"


def test_batch_buttons_follow_paused_and_terminal_snapshots(qtbot):
    dialog = show_dialog(qtbot)
    dialog.set_task_snapshot("m1", snapshot("paused"))
    dialog.set_task_snapshot("m2", snapshot("done"))
    assert not dialog._pause_all_btn.isEnabled()
    assert dialog._resume_all_btn.isEnabled() and dialog._stop_all_btn.isEnabled()

    controls = (dialog._pause_all_btn, dialog._resume_all_btn, dialog._stop_all_btn)
    dialog.set_task_snapshot("m1", snapshot("stopping"))
    assert not any(button.isEnabled() for button in controls)
    assert not dialog._update_all_btn.isEnabled()

    dialog.set_task_snapshot("m1", snapshot("canceled"))
    assert not any(button.isEnabled() for button in controls)
    assert dialog._update_all_btn.isEnabled()
    assert "已终止 1" in dialog._batch_counts.text()


def test_deferred_task_keeps_its_reason_and_can_restart_without_blocking_batch(qtbot):
    dialog = show_dialog(qtbot)
    dialog.set_task_snapshot("m1", snapshot("deferred", error="游戏已切换，下载文件已保留"))
    row = dialog._rows_by_id["m1"]
    requested = []
    dialog.update_requested.connect(requested.append)

    assert "等待继续" in row.task_phase.text()
    assert row.task_detail.text() == "游戏已切换，下载文件已保留"
    assert row.update_button.isEnabled() and row.update_button.isVisibleTo(dialog)
    assert dialog._update_all_btn.isEnabled()
    assert not dialog._pause_all_btn.isEnabled() and not dialog._resume_all_btn.isEnabled()
    assert not dialog._stop_all_btn.isEnabled()
    assert "等待继续 1" in dialog._batch_counts.text()
    row.update_button.click()
    assert requested == ["m1"]
    assert row.phase == "deferred"


def test_snapshot_arriving_before_its_row_can_restore_a_reopened_dialog(qtbot):
    dialog = show_dialog(qtbot, [])
    state = snapshot("paused", resume_phase="installing", stage_progress=0.6, detail="准备部署")
    dialog.set_task_snapshot("m1", state)
    state["phase"] = "done"
    dialog.set_updates([_info("m1")])
    row = dialog._rows_by_id["m1"]

    assert row.phase == "paused" and row.progress.value() == 600
    assert "安装" in row.task_phase.text()
    assert row.task_detail.text() == "准备部署"
    dialog.set_updates([])
    assert dialog._rows_by_id["m1"] is row, "正在运行或暂停的行必须保留"

    stopped = []
    dialog.stop_all_requested.connect(lambda: stopped.append(True))
    dialog.close()
    assert stopped == []


def test_mixed_stopping_and_paused_tasks_do_not_claim_all_tasks_are_paused(qtbot):
    dialog = show_dialog(qtbot)
    dialog.set_task_snapshot("m1", snapshot("paused"))
    dialog.set_task_snapshot("m2", snapshot("stopping"))

    assert "终止" in dialog._update_all_btn.accessibleName()
    assert dialog._resume_all_btn.isEnabled()
    assert not dialog._pause_all_btn.isEnabled()


def test_task_feedback_replaces_finished_check_feedback_to_keep_small_controls_reachable(qtbot):
    dialog = show_dialog(qtbot)
    dialog.resize(420, 340)
    dialog.set_check_snapshot(dict(phase="done", checking=False, total=10, done=10, found=2, failed=0))
    dialog.set_task_snapshot("m1", snapshot())
    qtbot.wait(20)

    assert dialog._batch_panel.isVisibleTo(dialog)
    assert not dialog._check_panel.isVisibleTo(dialog)
    assert dialog._scroll.viewport().height() >= 100
    row = dialog._rows_by_id["m1"]
    for button in (row.pause_button, row.stop_button):
        rect = QRect(button.mapTo(dialog._scroll.viewport(), QPoint()), button.size())
        assert dialog._scroll.viewport().rect().contains(rect)


def test_theme_change_preserves_paused_stage_and_recolors_controls(qtbot):
    previous = theme.current_theme()
    theme.set_theme("light")
    try:
        dialog = show_dialog(qtbot)
        dialog.set_task_snapshot("m1", snapshot("paused", resume_phase="extracting", stage_progress=0.4))
        row = dialog._rows_by_id["m1"]
        old_icon = row.pause_button._pix.toImage()
        theme.set_theme("dark")
        QApplication.sendEvent(dialog, QEvent(QEvent.Type.PaletteChange))
        assert row.phase == "paused" and "解压" in row.task_phase.text()
        assert row.progress.value() == 400
        assert row.pause_button._pix.toImage() != old_icon
    finally:
        theme.set_theme(previous)


@pytest.mark.parametrize("size", [(420, 340), (620, 430), (848, 626)])
@pytest.mark.parametrize("mode", ["light", "dark"])
def test_live_controls_remain_reachable_at_small_and_normal_sizes(qtbot, size, mode):
    previous = theme.current_theme()
    theme.set_theme(mode)
    try:
        dialog = show_dialog(qtbot, [_info("m1", "LongUnbrokenModName" * 30)] + [_info(str(i)) for i in range(5)])
        dialog.resize(*size)
        dialog.set_task_snapshot(
            "m1", snapshot(file_name="LongUnbrokenFileName" * 30 + ".zip", detail="正在处理" + "UnbrokenDetail" * 40)
        )
        dialog.set_task_snapshot("0", snapshot("paused", resume_phase="extracting", stage_progress=0.6))
        qtbot.wait(30)

        assert (dialog.width(), dialog.height()) == size
        assert dialog._scroll.horizontalScrollBar().maximum() == 0
        assert dialog._scroll.viewport().height() >= 80
        controls = (dialog._pause_all_btn, dialog._resume_all_btn, dialog._stop_all_btn, dialog._update_all_btn)
        rects = [QRect(button.mapTo(dialog, QPoint()), button.size()) for button in controls]
        assert all(dialog.rect().contains(rect) for rect in rects)
        assert all(not first.intersects(second) for i, first in enumerate(rects) for second in rects[i + 1 :])
        row = dialog._rows_by_id["m1"]
        for button in (row.pause_button, row.stop_button):
            rect = QRect(button.mapTo(dialog._scroll.viewport(), QPoint()), button.size())
            assert dialog._scroll.viewport().rect().contains(rect)
        assert row.progress.width() >= 100
        assert not dialog.grab().isNull()
    finally:
        theme.set_theme(previous)
