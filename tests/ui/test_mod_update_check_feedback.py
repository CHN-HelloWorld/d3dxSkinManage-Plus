from __future__ import annotations

from time import monotonic

import pytest
from PySide6.QtCore import QPoint, QRect, QSize
from PySide6.QtWidgets import QLabel, QProgressBar, QPushButton, QWidget

from src.ui.dialogs.mod_updates_dialog import ModUpdatesDialog
from tests.ui.test_mod_updates_dialog import _info, keep_dialog


def snapshot(**changes):
    now = monotonic()
    result = dict(
        checking=True,
        phase="connecting",
        started_at=now,
        last_progress_at=now,
        finished_at=0.0,
        total=8,
        done=0,
        found=0,
        failed=0,
        retry_only=False,
    )
    result.update(changes)
    return result


def show(qtbot):
    dialog = ModUpdatesDialog([_info("old")], enable_actions=True)
    keep_dialog(qtbot, dialog)
    dialog.show()
    return dialog


def title(dialog):
    widget = dialog.findChild(QLabel, "updateCheckTitle")
    assert widget is not None, "A+1 检测时需要独立状态条"
    return widget


def detail(dialog):
    return dialog.findChild(QLabel, "updateCheckDetail")


def action(dialog):
    return dialog.findChild(QPushButton, "updateCheckAction")


def progress(dialog):
    return dialog.findChild(QProgressBar, "updateCheckProgress")


def test_local_preparation_and_failure_have_explicit_feedback(qtbot):
    dialog = show(qtbot)
    dialog.set_check_snapshot(snapshot(phase="preparing", total=0))
    assert "准备" in title(dialog).text()
    assert action(dialog).text() == "取消检测"
    assert dialog.pending_ids() == ["old"]
    dialog.set_check_snapshot(snapshot(checking=False, phase="failed", error="数据库读取失败"))
    assert "准备失败" in title(dialog).text()
    assert "数据库读取失败" in detail(dialog).text()
    assert "最新" not in title(dialog).text()
    assert dialog.pending_ids() == ["old"]
    assert dialog._recheck_btn.isEnabled()


def test_checking_opens_a_separate_panel_without_replacing_the_update_count(qtbot):
    dialog = show(qtbot)
    dialog.set_checking(True)
    assert title(dialog).isVisibleTo(dialog)
    assert "连接" in title(dialog).text()
    assert "1 个可用更新" in dialog._subtitle.text()
    assert dialog.findChild(QLabel, "updateCollectionLabel").text() == "上次检测结果"
    assert action(dialog).text() == "取消检测"
    assert not dialog._recheck_btn.isEnabled()


def test_checking_blocks_single_updates_as_well_as_bulk_updates(qtbot):
    dialog = show(qtbot)
    dialog.set_checking(True)
    requested = []
    dialog.update_requested.connect(requested.append)
    dialog._rows_by_id["old"].update_button.click()
    assert requested == [], "检测中的旧条目不能发起下载"
    assert not dialog._update_all_btn.isEnabled()
    dialog.set_checking(False)
    assert dialog._rows_by_id["old"].update_button.isEnabled()


def test_actual_progress_keeps_old_rows_until_the_check_finishes(qtbot):
    dialog = show(qtbot)
    state = snapshot(phase="checking", done=3, found=2)
    dialog.set_check_snapshot(state)
    dialog.set_updates([_info("new")])
    assert dialog.pending_ids() == ["old"]
    assert "3 / 8" in title(dialog).text()
    assert "2 个可用更新" in detail(dialog).text()
    assert (progress(dialog).value(), progress(dialog).maximum()) == (3, 8)
    dialog.set_check_snapshot({**state, "checking": False, "phase": "done", "done": 8, "finished_at": monotonic()})
    assert dialog.pending_ids() == ["new"]
    assert "1 个可用更新" in title(dialog).text()
    assert dialog._rows_by_id["new"].update_button.isEnabled()


def test_slow_connection_has_elapsed_time_and_cancel_without_fake_progress(qtbot, monkeypatch):
    import src.ui.dialogs.mod_update_check_panel as panel_module

    monkeypatch.setattr(panel_module, "monotonic", lambda: 104.0)
    dialog = show(qtbot)
    dialog.set_check_snapshot(snapshot(started_at=100.0, last_progress_at=100.0))
    assert "响应较慢" in title(dialog).text()
    assert progress(dialog).maximum() == 0
    assert "4 秒" in dialog.findChild(QLabel, "updateCheckElapsed").text()
    assert dialog.findChild(QLabel, "updateCheckHint").isVisibleTo(dialog)
    requested = []
    dialog.cancel_check_requested.connect(lambda: requested.append("cancel"))
    action(dialog).click()
    assert requested == ["cancel"]
    assert dialog._checking, "点击请求取消后要等待服务确认，不能伪造成功"


def test_stalled_results_never_advance_the_completed_count(qtbot, monkeypatch):
    import src.ui.dialogs.mod_update_check_panel as panel_module

    monkeypatch.setattr(panel_module, "monotonic", lambda: 110.0)
    dialog = show(qtbot)
    dialog.set_check_snapshot(snapshot(phase="checking", done=3, found=1, started_at=100.0, last_progress_at=105.0))
    assert "3 / 8" in title(dialog).text()
    assert "等待后续结果" in detail(dialog).text()
    assert (progress(dialog).value(), progress(dialog).maximum()) == (3, 8)


def test_partial_results_retry_only_failed_items(qtbot):
    dialog = show(qtbot)
    dialog.set_updates([_info("old")], failures={"m2": "HTTP 503", "m3": "连接超时"})
    dialog.set_check_snapshot(snapshot(checking=False, phase="partial", done=8, failed=2, finished_at=monotonic()))
    assert "6 / 8" in title(dialog).text()
    assert "2 项失败" in title(dialog).text()
    assert "超时" not in detail(dialog).text(), "不能把所有失败都误报为超时"
    assert progress(dialog).isHidden()
    requested = []
    dialog.retry_failed_requested.connect(lambda: requested.append("failed-only"))
    dialog.recheck_requested.connect(lambda: requested.append("all"))
    action(dialog).click()
    assert requested == ["failed-only"]


def test_cancel_restores_previous_results_and_freezes_the_elapsed_time(qtbot, monkeypatch):
    import src.ui.dialogs.mod_update_check_panel as panel_module

    monkeypatch.setattr(panel_module, "monotonic", lambda: 120.0)
    dialog = show(qtbot)
    state = snapshot(started_at=100.0, last_progress_at=100.0)
    dialog.set_check_snapshot(state)
    dialog.set_updates([_info("new")])
    dialog.set_check_snapshot({**state, "checking": False, "phase": "cancelled", "finished_at": 106.0})
    assert dialog.pending_ids() == ["old"]
    assert "已取消" in title(dialog).text()
    assert "6 秒" in dialog.findChild(QLabel, "updateCheckElapsed").text()
    assert progress(dialog).isHidden()
    assert dialog._recheck_btn.isEnabled()
    action(dialog).click()
    assert not title(dialog).isVisibleTo(dialog)


def test_zero_candidates_does_not_claim_every_mod_is_current(qtbot):
    dialog = show(qtbot)
    dialog.set_updates([])
    dialog.set_check_snapshot(snapshot(checking=False, phase="done", total=0, done=0, finished_at=monotonic()))
    assert "全部已是最新" not in title(dialog).text()
    assert "没有" in detail(dialog).text() or "未找到" in detail(dialog).text()


def test_ignoring_all_found_updates_does_not_claim_they_were_installed(qtbot):
    dialog = show(qtbot)
    dialog.set_check_snapshot(snapshot(checking=False, phase="done", done=8, found=1, finished_at=monotonic()))
    dialog.set_updates([])
    assert "全部已是最新" not in title(dialog).text()


def test_compact_checking_window_keeps_the_first_rows_actions_in_view(qtbot):
    dialog = show(qtbot)
    dialog.resize(420, 340)
    dialog.set_checking(True)
    qtbot.wait(20)
    row_action = dialog._rows_by_id["old"].update_button
    viewport = dialog._scroll.viewport()
    assert viewport.rect().contains(QRect(row_action.mapTo(viewport, QPoint()), row_action.size()))


@pytest.mark.parametrize("size", [(848, 626), (620, 430), (420, 340), (848, 340)])
def test_checking_panel_leaves_the_list_and_footer_inside_small_windows(qtbot, size):
    dialog = show(qtbot)
    dialog.resize(*size)
    dialog.set_check_snapshot(snapshot(started_at=monotonic() - 6, last_progress_at=monotonic() - 6))
    qtbot.wait(20)
    assert dialog.size() == QSize(*size)
    for child in (title(dialog), action(dialog), dialog._update_all_btn):
        rect = QRect(child.mapTo(dialog, QPoint()), child.size())
        assert dialog.rect().contains(rect)
    panel = dialog.findChild(QWidget, "updateCheckPanel")
    panel_bottom = panel.mapTo(dialog, panel.rect().bottomLeft()).y()
    list_top = dialog._scroll.mapTo(dialog, QPoint()).y()
    assert panel_bottom < list_top
    assert dialog._scroll.height() >= 64, "小窗口检测态仍应能看到至少一条旧记录"
