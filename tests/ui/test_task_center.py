"""验证任务中心交互、状态展示及收起策略。

成功通知定时收起；失败和停止通知在失焦后收起，悬停时保留。
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from src.ui.widgets.task_center import (
    ST_DONE,
    ST_FAILED,
    ST_QUEUED,
    ST_RUNNING,
    TaskCenterPanel,
)


@pytest.fixture()
def panel(qtbot):
    # 使用 yield 保持父窗口存活；qtbot 仅持有弱引用，提前回收会级联销毁子控件及定时器。
    host = QWidget()
    host.resize(1200, 700)
    qtbot.addWidget(host)
    host.show()
    yield TaskCenterPanel(host)


def test_panel_switches_between_indeterminate_and_real_progress(panel):
    panel.start_batch(2, "正在纳管 2 个新增项")
    panel.update_progress(None, "正在纳管 1/2", "校验内容: Skin")
    assert panel._ring._indeterminate is True
    assert panel._ring._spin_timer.isActive()
    assert panel._detail.full_text() == "校验内容: Skin"

    panel.update_progress(62.5, "正在纳管 1/2", "转为 ZIP: Skin (25%)")
    assert panel._ring._indeterminate is False
    assert panel._ring._progress == pytest.approx(62.5)


def test_stop_button_emits_signal(panel, qtbot):
    panel.start_batch(2, "正在纳管")
    with qtbot.waitSignal(panel.stop_requested, timeout=1000):
        qtbot.mouseClick(panel._stop_btn, Qt.MouseButton.LeftButton)


def test_cancelled_state_persists_until_dismissed(panel):
    panel.start_batch(2, "正在纳管")
    panel.finish_cancelled("未提交内容已清理，已恢复 1 个原文件")

    assert panel._title.full_text() == "已停止纳管"
    assert panel._detail.full_text() == "未提交内容已清理，已恢复 1 个原文件"
    assert not panel._stop_btn.isVisible()
    # 设计反转：旧面板此处 _hide_timer.isActive 为真；新面板终态失败不自动消失。
    assert not panel._hide_timer.isActive()
    assert panel._dismiss_wired is True


def test_finish_with_failures_persists_and_reports_counts(panel):
    panel.start_batch(3, "正在纳管 3 个新增项")
    panel.finish(
        "纳管完成，但有失败项",
        "已纳管 1 个，2 个失败；已恢复 2 个原文件",
        ok=False,
    )

    assert panel._title.full_text() == "纳管完成，但有失败项"
    assert panel._detail.full_text() == "已纳管 1 个，2 个失败；已恢复 2 个原文件"
    assert panel._ring._progress == pytest.approx(100)
    assert not panel._stop_btn.isVisible()
    assert not panel._hide_timer.isActive()  # 同上：失败驻留


def test_finish_success_auto_hides(panel):
    panel.start_batch(2, "正在纳管")
    panel.finish("Mod 纳管完成", "已纳管 2 个 Mod")
    assert panel._hide_timer.isActive()
    assert panel._ring._ring_color == "success"


def test_item_rows_track_per_item_state(panel):
    panel.start_batch(3, "正在纳管 3 个新增项")
    for i, name in enumerate(["a.7z", "b.zip", "c.rar"]):
        panel.update_item(i, name, ST_QUEUED)
    assert len(panel._items) == 3
    assert panel._items[0]._state_label.text() == "排队中"

    panel.update_item(0, "a.7z", ST_DONE)
    panel.update_item(1, "b.zip", ST_RUNNING, "解压 62%")
    panel.update_item(2, "c.rar", ST_FAILED, "压缩包损坏")
    assert panel._items[0]._state_label.text() == "已纳管"
    assert panel._items[1]._state_label.text() == "解压 62%"
    assert panel._items[2]._state_label.text() == "压缩包损坏"


def test_expand_grows_panel_and_stays_in_viewport(panel):
    panel.start_batch(2, "正在纳管")
    panel.update_item(0, "a.7z", ST_QUEUED)
    panel.update_item(1, "b.zip", ST_QUEUED)
    collapsed = panel.height()
    panel._toggle_expand()
    assert panel.height() > collapsed
    assert panel._list_scroll.isVisible()
    host = panel.parent()
    assert panel.y() + panel.height() <= host.height()
    assert panel.x() + panel.width() <= host.width()


def test_long_names_elide_instead_of_widening_panel(panel, qtbot):
    """回归守卫：_ItemRow 若给 _ElideLabel 覆写 Expanding 策略，QLabel 的全文
    sizeHint 会把列表容器撑到远超面板固定宽（实测 612px > 340px），省略号永不触发。
    """
    long_name = "Epsilon_Kimono_" + "超长名字" * 8 + "_ABCDEFGHIJK.7z"
    panel.start_batch(1, "正在纳管")
    panel.update_item(0, long_name, ST_RUNNING, "解压中")
    panel._toggle_expand()
    qtbot.wait(10)

    label = panel._items[0]._name
    assert panel.width() == 340
    assert label.width() < 340
    assert label.text() != label.full_text()
    assert label.text().endswith("…")


def test_reuse_after_failure_restores_expand_button(panel):
    panel.start_batch(1, "第一批")
    panel.finish("失败", "有失败项", ok=False)
    assert panel._dismiss_wired is True

    panel.start_batch(1, "第二批")
    assert panel._dismiss_wired is False
    assert panel._is_expanded is False
    assert not panel._list_scroll.isVisible()


def test_pause_resume_only_when_pausable(panel, qtbot):
    panel.start_batch(2, "正在导入", has_pause=True)
    assert panel._ring.isEnabled()
    with qtbot.waitSignal(panel.pause_requested, timeout=1000):
        panel._ring.click()
    with qtbot.waitSignal(panel.resume_requested, timeout=1000):
        panel._ring.click()

    # 投放纳管不支持暂停：环退化为纯展示，不可点。
    panel.start_batch(2, "正在纳管", has_pause=False)
    assert not panel._ring.isEnabled()


def test_queue_info_appends_without_eliding_stored_text(panel):
    panel.start_batch(2, "正在纳管")
    panel.update_progress(50.0, "正在纳管 1/2", "解压 " + "长" * 60)
    panel.queue_info(1)
    assert "…" not in panel._detail.full_text()
    assert panel._detail.full_text().endswith("另 1 组排队")
