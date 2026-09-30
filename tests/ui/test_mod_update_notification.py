import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget

from src.ui import anim
from src.ui.widgets.mod_update_notification import ModUpdateNotification
from src.ui.widgets.task_center import ST_QUEUED, TaskCenterPanel, TaskCenterStack


def _snapshot(phase="downloading", *, total=100, downloaded=25, progress=None):
    return dict(
        phase=phase,
        resume_phase=phase,
        game_id="game-a",
        file_name="body.zip",
        downloaded_bytes=downloaded,
        total_bytes=total,
        speed_bps=0,
        eta_seconds=-1,
        stage_progress=progress,
        detail="",
        error="",
    )


def _settle(*panels):
    for panel in panels:
        panel._stop_current_anim()
        panel.reposition()


def _host(qtbot, height=900):
    host = QWidget()
    host.resize(1100, height)
    qtbot.addWidget(host)
    host.show()
    return host, TaskCenterStack()


@pytest.mark.parametrize(
    "order", [("import", "update", "export"), ("update", "export", "import"), ("import", "export", "update")]
)
def test_update_stacks_by_arrival_and_middle_departure_moves_only_panels_above(qtbot, order):
    host, stack = _host(qtbot)
    panels = {
        "import": TaskCenterPanel(host, stack=stack),
        "export": TaskCenterPanel(host, stack=stack),
        "update": ModUpdateNotification(host, stack=stack),
    }
    for name in order:
        panel = panels[name]
        if name == "update":
            panel.present("game-a", {"mod": _snapshot()}, {"mod": "Skin"})
        else:
            panel.start_batch(3, name)
            for index in range(3):
                panel.update_item(index, f"Skin {index}", ST_QUEUED)
        _settle(*panels.values())
    bottom, middle, top = [panels[name] for name in order]
    assert bottom.geometry().bottom() == host.height() - 15
    assert middle.geometry().bottom() + 11 == bottom.y()
    assert top.geometry().bottom() + 11 == middle.y()
    old_bottom = bottom.pos()
    bottom._toggle_expand()
    _settle(*panels.values())
    assert middle.geometry().bottom() + 11 == bottom.y()
    assert top.geometry().bottom() + 11 == middle.y()
    old_top = top.y()
    middle._on_slide_out_done()
    _settle(bottom, top)
    assert top.y() > old_top and top.geometry().bottom() + 11 == bottom.y()
    assert bottom.x() == old_bottom.x()
    assert stack._order == [bottom, top]


def test_three_expanded_notifications_fit_short_content_without_overlap(qtbot):
    host, stack = _host(qtbot, height=600)
    panels = [
        TaskCenterPanel(host, stack=stack),
        TaskCenterPanel(host, stack=stack),
        ModUpdateNotification(host, stack=stack),
    ]
    for panel in panels[:2]:
        panel.start_batch(10, "正在导入/导出")
        for index in range(10):
            panel.update_item(index, f"Skin {index}", ST_QUEUED)
    panels[2].present("game-a", {str(i): _snapshot() for i in range(10)}, {})
    _settle(*panels)
    for panel in panels:
        panel._toggle_expand()
    _settle(*panels)
    assert all(panel.y() >= 0 and panel.geometry().bottom() < host.height() for panel in panels)
    assert panels[1].geometry().bottom() + 11 == panels[0].y()
    assert panels[2].geometry().bottom() + 11 == panels[1].y()
    assert all(panel._list_scroll.verticalScrollBar().maximum() > 0 for panel in panels)


def test_unknown_download_size_and_real_install_progress_never_claim_overall_completion(qtbot):
    host, stack = _host(qtbot)
    panel = ModUpdateNotification(host, stack=stack)
    panel.present(
        "game-a",
        {"a": _snapshot("installing", downloaded=100, progress=0.25), "b": _snapshot(total=0)},
        {"a": "A", "b": "B"},
    )
    assert panel._download_bar.maximum() == 0, "一个文件大小未知时不能显示确定的总下载百分比"
    assert "25%" in panel._items[0]._state_label.text()
    assert "0 / 2" in panel._detail.full_text()
    assert panel._finish_btn.isHidden()


def test_returning_to_background_during_notice_exit_cancels_the_old_exit(qtbot):
    host, stack = _host(qtbot)
    panel = ModUpdateNotification(host, stack=stack)
    snapshots = {"a": _snapshot()}
    panel.present("game-a", snapshots, {"a": "A"})
    _settle(panel)
    panel.dismiss()
    panel.present("game-a", snapshots, {"a": "A"})
    qtbot.wait(300)
    assert panel.isVisible() and panel in stack._order


def test_terminal_notice_keeps_details_until_ack_and_resets_controls_on_retry(qtbot):
    host, stack = _host(qtbot)
    panel = ModUpdateNotification(host, stack=stack)
    panel.present("game-a", {"a": _snapshot("failed")}, {"a": "A"})
    assert panel._finish_btn.isVisible()
    assert not panel._dismiss_armed and not panel._hide_timer.isActive()
    panel._toggle_expand()
    assert panel._is_expanded and panel._list_scroll.isVisible()
    panel.present("game-a", {"a": _snapshot()}, {"a": "A"})
    assert panel._finish_btn.isHidden() and panel._ring.isEnabled()
    assert not panel._is_expanded and not panel._terminal


def test_animated_middle_departure_and_detail_growth_never_overwrite_each_others_position(qtbot, monkeypatch):
    monkeypatch.setattr(anim, "_enabled", True)
    host, stack = _host(qtbot, height=600)
    bottom = TaskCenterPanel(host, stack=stack)
    middle = TaskCenterPanel(host, stack=stack)
    top = ModUpdateNotification(host, stack=stack)
    for panel in (bottom, middle):
        panel.start_batch(10, "Task")
        for index in range(10):
            panel.update_item(index, f"Name {index}", ST_QUEUED)
    qtbot.wait(250)
    bottom._toggle_expand()
    middle._toggle_expand()
    qtbot.wait(250)
    top.present("game-a", {str(i): _snapshot() for i in range(10)}, {})
    qtbot.wait(340)
    top._toggle_expand()
    qtbot.wait(300)
    host.resize(1100, 420)
    stack.relayout(animate=False)
    qtbot.wait(210)
    gaps = []
    timer = QTimer(host)
    timer.setInterval(10)
    timer.timeout.connect(lambda: gaps.append(bottom.y() - top.y() - top.height()) if not middle.isVisible() else None)
    timer.start()
    middle._slide_out()
    qtbot.wait(600)
    timer.stop()
    assert gaps and min(gaps) >= 9, f"通知在离场补位途中重叠：{gaps}"
    assert gaps[-1] == 10


@pytest.mark.parametrize("count,height,collapse_index", [(3, 600, 1), (2, 420, 0)])
def test_collapsing_one_card_redistributes_detail_space_without_transient_overlap(
    qtbot, monkeypatch, count, height, collapse_index
):
    monkeypatch.setattr(anim, "_enabled", True)
    host, stack = _host(qtbot, height=height)
    bottom, middle = [TaskCenterPanel(host, stack=stack) for _ in range(2)]
    panels = [bottom, middle]
    for panel in (bottom, middle):
        panel.start_batch(10, "Task")
        for index in range(10):
            panel.update_item(index, f"Name {index}", ST_QUEUED)
    if count == 3:
        top = ModUpdateNotification(host, stack=stack)
        top.present("game-a", {str(i): _snapshot() for i in range(10)}, {})
        panels.append(top)
    qtbot.wait(250)
    for panel in panels:
        panel._toggle_expand()
    qtbot.wait(300)
    gaps = []
    timer = QTimer(host)
    timer.setInterval(10)
    timer.timeout.connect(
        lambda: gaps.extend(lower.y() - upper.y() - upper.height() for lower, upper in zip(panels, panels[1:]))
    )
    timer.start()
    panels[collapse_index]._toggle_expand()
    qtbot.wait(350)
    timer.stop()
    assert gaps and min(gaps) >= 9, f"回收明细空间时通知短暂重叠：{gaps}"
