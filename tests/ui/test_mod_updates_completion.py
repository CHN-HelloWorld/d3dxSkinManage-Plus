from __future__ import annotations

from dataclasses import replace
from time import monotonic

import pytest
from PySide6.QtCore import QEvent, QSequentialAnimationGroup, Qt
from PySide6.QtWidgets import QApplication

from src.ui import anim, theme
from src.ui.dialogs.mod_updates_dialog import ModUpdatesDialog
from tests.ui.test_mod_updates_dialog import _info, keep_dialog


def show_dialog(qtbot, infos=None):
    dialog = ModUpdatesDialog(infos if infos is not None else [_info("m1")], enable_actions=True)
    keep_dialog(qtbot, dialog)
    dialog._sized_for_screen = True
    dialog.show()
    qtbot.waitUntil(dialog.isVisible)
    return dialog


def test_success_receipt_holds_then_retires_without_losing_completed_count(qtbot):
    dialog = show_dialog(qtbot, [_info("m1"), _info("m2", "示例角色午西装")])
    dialog.set_task_snapshot("m2", {"phase": "paused"})
    dialog.set_task_snapshot("m1", {"phase": "done", "error": ""})
    receipt = dialog._rows_by_id["m1"]

    assert receipt.state == "done" and receipt.isVisibleTo(dialog)
    assert dialog.pending_ids() == ["m2"]
    qtbot.wait(1500)
    assert dialog.row_count() == 2, "成功提示需要留出阅读时间"
    dialog.set_task_snapshot("m1", {"phase": "done"})
    qtbot.waitUntil(lambda: dialog.row_count() == 1, timeout=1300)

    assert dialog._task_snapshots["m1"]["phase"] == "done"
    assert "已更新 1" in dialog._batch_counts.text()
    assert dialog.pending_ids() == ["m2"]
    assert not dialog.is_batch_complete()


@pytest.mark.parametrize("motion", [False, True])
@pytest.mark.parametrize("snapshot_first", [False, True])
def test_reopening_background_completed_task_does_not_replay_its_receipt(qtbot, motion, snapshot_first):
    info = _info("m1")
    dialog = show_dialog(qtbot, [] if snapshot_first else [info])
    previous = anim.is_enabled()
    anim.set_enabled(motion)
    try:
        dialog.set_task_snapshot("m1", {"phase": "done", "completed_at": monotonic() - 5})
        if snapshot_first:
            dialog.set_updates([info])

        assert dialog.row_count() == 0, "后台已退场的成功项不能重开后再次闪现"
        assert dialog.pending_ids() == [] and dialog.is_batch_complete()
        assert dialog._empty_title.text() == "全部更新完成"
        assert "已更新 1" in dialog._batch_counts.text()
        assert dialog._update_all_btn.isEnabled()
        dialog.set_updates([info])
        assert dialog.row_count() == 0
    finally:
        anim.set_enabled(previous)


def test_reopening_recent_success_holds_only_the_remaining_reading_time(qtbot):
    dialog = show_dialog(qtbot)
    dialog.set_task_snapshot("m1", {"phase": "done", "completed_at": monotonic() - 1.2})
    assert dialog.row_count() == 1
    qtbot.wait(300)
    assert dialog._rows_by_id["m1"].isVisibleTo(dialog)
    qtbot.waitUntil(lambda: dialog.row_count() == 0, timeout=1000)
    assert dialog.is_batch_complete()
    assert "已更新 1" in dialog._batch_counts.text()


def test_success_receipt_replaces_the_previous_install_detail(qtbot):
    dialog = show_dialog(qtbot)
    dialog.set_task_snapshot(
        "m1",
        {"phase": "installing", "detail": "正在复制旧的阶段文件", "file_name": "costume.zip", "stage_progress": 0.8},
    )
    dialog.set_task_snapshot("m1", {"phase": "done", "error": ""})
    receipt = dialog._rows_by_id["m1"]

    assert receipt.task_detail.text() == "新版已安装 · 即将移出列表"
    assert receipt.task_detail.isVisibleTo(dialog)
    assert not receipt.task_file.isVisibleTo(dialog)
    assert not receipt.progress.isVisibleTo(dialog)
    assert not receipt.task_icon.pixmap().isNull()


def test_successful_batch_has_enabled_svg_finish_action_after_rows_leave(qtbot):
    dialog = show_dialog(qtbot)
    updates = []
    requests = []
    dialog.update_all_requested.connect(lambda: updates.append(True))
    dialog.set_close_handler(lambda: requests.append(dialog.is_batch_complete()) or True)
    dialog.set_task_snapshot("m1", {"phase": "done", "error": ""})

    assert dialog.is_batch_complete()
    assert dialog._update_all_btn.isEnabled()
    assert dialog._update_all_btn.accessibleName() == "完成"
    assert dialog._update_all_btn.property("_themed_icon_name") == "mod-updates/check"
    assert not dialog._update_all_btn.icon().isNull()
    assert dialog._update_all_btn._count.isHidden() and dialog._update_all_btn._divider.isHidden()
    qtbot.waitUntil(lambda: dialog.row_count() == 0, timeout=2800)

    assert dialog._stack.currentIndex() == 1
    assert dialog._empty_title.text() == "全部更新完成"
    assert "1" in dialog._empty_description.text()
    assert "完成" in dialog._footer_count.text()
    assert "已更新 1" in dialog._batch_counts.text()
    assert dialog._update_all_btn.isEnabled()
    dialog._update_all_btn.click()
    assert requests == [True] and updates == []
    assert not dialog.isVisible()


@pytest.mark.parametrize("entry", ["accept", "reject", "done", "close", "title", "later", "escape"])
def test_each_user_close_entry_synchronously_honors_one_close_decision(qtbot, entry):
    dialog = show_dialog(qtbot)
    dialog.set_task_snapshot("m1", {"phase": "downloading"})
    decisions = []
    finished = []
    permitted = False

    def decide():
        decisions.append(dialog.isVisible())
        return permitted

    dialog.set_close_handler(decide)
    dialog.finished.connect(finished.append)

    def request():
        if entry == "title":
            dialog._close_btn.click()
        elif entry == "later":
            dialog._later.click()
        elif entry == "escape":
            qtbot.keyClick(dialog, Qt.Key.Key_Escape)
        elif entry == "done":
            dialog.done(dialog.DialogCode.Accepted)
        else:
            getattr(dialog, entry)()

    request()
    assert decisions == [True], "关闭处理必须同步发生且每次用户请求只调用一次"
    assert dialog.isVisible() and finished == []
    permitted = True
    request()
    assert decisions == [True, True]
    assert not dialog.isVisible() and len(finished) == 1


def test_forced_teardown_bypasses_the_close_handler_and_stops_retirement(qtbot):
    dialog = show_dialog(qtbot)
    requests = []
    dialog.set_close_handler(lambda: requests.append(True) or False)
    dialog.set_task_snapshot("m1", {"phase": "done", "error": ""})
    dialog.force_close()

    assert requests == [] and not dialog.isVisible()
    assert dialog.row_count() == 0
    assert dialog._task_snapshots["m1"]["phase"] == "done"


def test_forced_close_during_a_close_decision_emits_finished_only_once(qtbot):
    dialog = show_dialog(qtbot)
    finished = []
    decisions = []

    def decide():
        decisions.append(True)
        dialog.force_close()
        return True

    dialog.set_close_handler(decide)
    dialog.finished.connect(finished.append)
    dialog.reject()

    assert decisions == [True]
    assert finished == [dialog.DialogCode.Rejected]
    assert not dialog.isVisible()


def test_refresh_does_not_resurrect_a_completed_file_during_or_after_retirement(qtbot):
    info = _info("m1")
    dialog = show_dialog(qtbot, [info])
    dialog.set_task_snapshot("m1", {"phase": "done", "error": ""})
    dialog.set_updates([replace(info, local_name="新名称")])

    assert dialog.pending_ids() == []
    assert dialog._rows_by_id["m1"].state == "done"
    qtbot.waitUntil(lambda: dialog.row_count() == 0, timeout=2800)
    dialog.set_updates([info])
    dialog.set_task_snapshot("m1", {"phase": "done"})
    assert dialog.row_count() == 0 and dialog.is_batch_complete()
    assert "已更新 1" in dialog._batch_counts.text()


def test_completed_search_result_retires_without_hiding_other_pending_work(qtbot):
    dialog = show_dialog(qtbot, [_info("m1"), _info("m2", "示例角色午西装")])
    dialog._search.setText("示例角色丁")
    dialog.set_task_snapshot("m1", {"phase": "done", "error": ""})
    qtbot.waitUntil(lambda: dialog.row_count() == 1, timeout=2800)

    assert dialog.pending_ids() == ["m2"] and not dialog.is_batch_complete()
    assert dialog._search.text() == "示例角色丁"
    assert dialog._empty_title.text() == "没有找到匹配的 Mod"
    assert dialog._update_all_btn.isEnabled()
    dialog._empty_action.click()
    assert dialog._search.text() == ""
    assert dialog._rows_by_id["m2"].isVisibleTo(dialog)


def test_completed_batch_remains_completed_when_search_is_nonempty(qtbot):
    dialog = show_dialog(qtbot)
    dialog._search.setText("示例角色丁")
    dialog.set_task_snapshot("m1", {"phase": "done", "error": ""})
    qtbot.waitUntil(lambda: dialog.row_count() == 0, timeout=2800)
    assert dialog._empty_title.text() == "全部更新完成"
    assert dialog._update_all_btn.accessibleName() == "完成"


def test_a_new_task_snapshot_can_arrive_before_its_newer_file_row(qtbot):
    info = _info("m1")
    dialog = show_dialog(qtbot, [info])
    dialog.set_task_snapshot("m1", {"phase": "done", "error": ""})
    qtbot.waitUntil(lambda: dialog.row_count() == 0, timeout=2800)
    dialog.set_task_snapshot("m1", {"phase": "paused", "resume_phase": "installing", "stage_progress": 0.6})
    newer = replace(info, latest_file=replace(info.latest_file, file_id=12, date_added_ts=3000, md5_checksum="ccc"))
    dialog.set_updates([newer])

    row = dialog._rows_by_id["m1"]
    assert row.phase == "paused"
    assert row.progress.value() == 600
    assert dialog.pending_ids() == ["m1"] and not dialog.is_batch_complete()


@pytest.mark.parametrize(
    "file_changes,ready",
    [
        ({"file_id": 12, "date_added_ts": 3000, "md5_checksum": "ccc"}, True),
        ({"file_id": 12, "date_added_ts": 1500, "md5_checksum": "ccc"}, False),
        ({"file_id": 12, "date_added_ts": 3000, "md5_checksum": "bbb"}, False),
        ({"file_id": 11, "date_added_ts": 3000, "md5_checksum": "bbb"}, False),
    ],
)
def test_only_a_newer_different_file_can_replace_a_success_receipt(qtbot, file_changes, ready):
    info = _info("m1")
    dialog = show_dialog(qtbot, [info])
    dialog.set_task_snapshot("m1", {"phase": "done", "error": ""})
    refreshed = replace(info, latest_file=replace(info.latest_file, **file_changes), latest_version="v2.0")
    dialog.set_updates([refreshed])

    assert dialog.pending_ids() == (["m1"] if ready else [])
    assert dialog.is_batch_complete() is not ready
    if ready:
        qtbot.wait(2200)
        row = dialog._rows_by_id["m1"]
        assert row.state == "ready" and row.update_button.isEnabled()
        assert row.graphicsEffect() is None and row.maximumHeight() > 1000
    else:
        assert dialog._rows_by_id["m1"].state == "done"


def test_success_receipt_fades_before_smoothly_reflowing_the_following_row(qtbot):
    previous = anim.is_enabled()
    anim.set_enabled(True)
    try:
        dialog = show_dialog(qtbot, [_info("m1"), _info("m2", "示例角色午西装")])
        dialog.set_task_snapshot("m1", {"phase": "done", "error": ""})
        receipt = dialog._rows_by_id["m1"]
        following = dialog._rows_by_id["m2"]
        qtbot.waitUntil(lambda: receipt.graphicsEffect() is not None, timeout=2500)
        sequence = dialog.findChild(QSequentialAnimationGroup)
        assert sequence is not None
        sequence.pause()
        sequence.setCurrentTime(100)
        QApplication.processEvents()
        full_height, old_top = receipt.height(), following.y()

        assert 0 < receipt.graphicsEffect().opacity() < 1
        sequence.setCurrentTime(220 + 180)
        QApplication.processEvents()
        assert 0 < receipt.height() < full_height
        assert old_top - full_height < following.y() < old_top
        assert receipt.graphicsEffect().opacity() == 0
        assert dialog.pending_ids() == ["m2"]
        sequence.setCurrentTime(sequence.duration())
        QApplication.processEvents()
        assert dialog.row_count() == 1
        assert following.y() < old_top
        assert dialog._task_snapshots["m1"]["phase"] == "done"
    finally:
        anim.set_enabled(previous)


def test_finish_action_and_completed_empty_state_retheme_without_losing_history(qtbot):
    previous = theme.current_theme()
    try:
        theme.set_theme("light")
        dialog = show_dialog(qtbot)
        dialog.set_task_snapshot("m1", {"phase": "done", "error": ""})
        original = dialog._update_all_btn._icon.pixmap().toImage()
        theme.set_theme("dark")
        QApplication.sendEvent(dialog, QEvent(QEvent.Type.PaletteChange))
        assert dialog._update_all_btn._icon.pixmap().toImage() != original
        assert dialog._update_all_btn.accessibleName() == "完成"
        assert dialog._update_all_btn.isEnabled()
        assert dialog.is_batch_complete()
    finally:
        theme.set_theme(previous)
