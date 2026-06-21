"""验证多张任务浮层的堆叠布局。

先进入的面板靠近右下角，后进入的面板依次向上排列；某层收起时，其上方面板下移补位。
只有可见面板参与位置偏移计算。

任务面板自行管理 geometry 动画。测试通过 ``_settle`` 停止动画并定位到终点，
避免在事件循环未推进时读取起始帧坐标。
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QWidget

from src.ui import anim
from src.ui.widgets.task_center import ST_QUEUED, TaskCenterPanel, TaskCenterStack


def _settle(*panels: TaskCenterPanel) -> None:
    """停止滑入动画并将面板置于最终位置，确保位置断言稳定。"""
    for panel in panels:
        panel._stop_current_anim()
        panel._anim = None
        panel.reposition()


@pytest.fixture()
def stacked(qtbot, monkeypatch):
    """一个宿主 + 两张挂在同一 TaskCenterStack 上的浮层。"""
    monkeypatch.setattr(anim, "_enabled", False)
    host = QWidget()
    host.resize(1200, 700)
    qtbot.addWidget(host)
    host.show()
    stack = TaskCenterStack()
    first = TaskCenterPanel(host, stack=stack)
    second = TaskCenterPanel(host, stack=stack)
    yield host, stack, first, second


def test_second_panel_stacks_above_the_first(stacked):
    host, stack, first, second = stacked

    first.start_batch(2, "正在导入 2 个 Mod")
    _settle(first)
    bottom_y = first.y()
    assert first.isVisible()
    # 最下面那张：底边贴宿主底边（差一个 _MARGIN），没有额外偏移
    assert stack.offset_for(first) == 0
    assert bottom_y + first.height() == host.height() - first._MARGIN

    second.start_batch(3, "正在导出 3 个 Mod")
    _settle(second)
    assert second.isVisible()
    # 后进入的叠在上面：y 更小，且正好让出下面那张的高度 + 间距
    assert second.y() < bottom_y
    assert stack.offset_for(second) == first.height() + TaskCenterStack._GAP
    assert second.y() + second.height() == bottom_y - TaskCenterStack._GAP
    # 下面那张没被挤动
    assert first.y() == bottom_y


def test_upper_panel_sinks_when_lower_one_hides(stacked):
    host, stack, first, second = stacked

    first.start_batch(1, "正在导入")
    second.start_batch(1, "正在导出")
    _settle(first, second)
    bottom_y = first.y()
    assert second.y() < bottom_y

    # 下面那张收起（模拟导入先跑完滑走）
    first._slide_out()
    first._on_slide_out_done()

    assert not first.isVisible()
    assert stack.offset_for(second) == 0, "下面那张走了，上面的不该再让位"
    assert second.y() == bottom_y, "上面那张必须下沉到原来最底下的位置"


def test_slide_in_finish_triggers_a_relayout(stacked):
    host, stack, first, second = stacked

    first.start_batch(1, "正在导入")
    _settle(first)
    bottom_y = first.y()

    second.start_batch(1, "正在导出")  # 滑入动画在跑，未结束
    first._slide_out()
    first._on_slide_out_done()  # 此时 second 有动画在跑 → 本次下沉被让开
    assert second.y() != bottom_y

    second._stop_current_anim()
    second._on_slide_in_done()  # 滑入结束 → 自愈补一次重排
    assert second.y() == bottom_y


def test_hidden_panel_does_not_occupy_stack_space(stacked):
    host, stack, first, second = stacked

    # 只显示上面那张：它此时就是唯一可见的，应该贴底
    second.start_batch(1, "正在导出")
    _settle(second)
    assert stack.offset_for(second) == 0
    assert second.y() + second.height() == host.height() - second._MARGIN

    # 之后进来的那张压到栈顶，叠在它上面
    first.start_batch(1, "正在导入")
    _settle(first)
    assert first.y() < second.y()


def test_relayout_is_idempotent_and_survives_repeat_calls(stacked):
    host, stack, first, second = stacked

    first.start_batch(1, "正在导入")
    second.start_batch(1, "正在导出")
    _settle(first, second)
    before = (first.pos(), second.pos())

    for _ in range(3):
        stack.relayout()

    assert (first.pos(), second.pos()) == before


def test_panel_without_stack_keeps_bottom_right_anchor(qtbot, monkeypatch):
    monkeypatch.setattr(anim, "_enabled", False)
    host = QWidget()
    host.resize(900, 600)
    qtbot.addWidget(host)
    host.show()
    panel = TaskCenterPanel(host)

    panel.start_batch(1, "正在纳管")
    _settle(panel)

    assert panel._stack is None
    assert panel._stack_offset() == 0
    assert panel.y() + panel.height() == host.height() - panel._MARGIN


def test_expanding_bottom_panel_pushes_the_upper_one_up(stacked):
    """展开任意面板明细时，其上方面板同步重排并保持固定间距，不得重叠。"""
    host, stack, first, second = stacked

    first.start_batch(3, "正在导入 3 个 Mod")
    for i, name in enumerate(("A", "B", "C")):
        first.update_item(i, name, ST_QUEUED)
    second.start_batch(1, "正在导出 1 个 Mod")
    _settle(first, second)

    h_before = first.height()
    y_before = second.y()
    assert second.y() + second.height() == first.y() - TaskCenterStack._GAP

    first._toggle_expand()  # 展开最下面那张的明细
    _settle(first)

    assert first.height() > h_before, "展开明细必须把面板撑高（否则这条测试没意义）"
    assert second.y() < y_before, "上面那张必须被顶上去，不能留在原位被遮挡"
    assert second.y() + second.height() == first.y() - TaskCenterStack._GAP, "展开后仍不许重叠"

    first._toggle_expand()  # 再收起 → 上面那张要沉回来
    _settle(first)

    assert first.height() == h_before
    assert second.y() == y_before
    assert second.y() + second.height() == first.y() - TaskCenterStack._GAP


def test_expanding_upper_panel_does_not_move_the_lower_one(stacked):
    host, stack, first, second = stacked

    first.start_batch(1, "正在导入")
    second.start_batch(3, "正在导出 3 个 Mod")
    for i, name in enumerate(("A", "B", "C")):
        second.update_item(i, name, ST_QUEUED)
    _settle(first, second)
    first_y = first.y()

    second._toggle_expand()
    _settle(second)

    assert first.y() == first_y, "最底下那张是锚，展开上面的不该动它"
    assert second.y() + second.height() == first.y() - TaskCenterStack._GAP
