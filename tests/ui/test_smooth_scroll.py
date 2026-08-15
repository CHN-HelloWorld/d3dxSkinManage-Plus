"""SmoothScroller 平滑滚动缓动测试。

只验证缓动数学（_tick 逐帧逼近目标、不越界、最终吸附）——这是平滑滚动正确性的
核心，不依赖真实 wheel 事件 / 动画开关 / 计时器时序，确定性可测。实际「手感」由
人工运行验证。
"""

from PySide6.QtWidgets import QScrollArea, QWidget

from src.ui.smooth_scroll import SmoothScroller


def _make_scrollable(qtbot, content_h=5000, view_h=400):
    area = QScrollArea()
    area.setWidgetResizable(True)
    content = QWidget()
    content.setFixedSize(200, content_h)
    area.setWidget(content)
    qtbot.addWidget(area)
    area.resize(200, view_h)
    area.show()
    qtbot.waitExposed(area)
    return area


def test_tick_eases_monotonically_to_target(qtbot):
    area = _make_scrollable(qtbot)
    s = SmoothScroller.attach(area)
    bar = area.verticalScrollBar()
    assert bar.maximum() > 0, "构造的内容应可滚动"

    s._target = float(bar.maximum())
    prev = bar.value()
    for _ in range(500):
        s._tick()
        cur = bar.value()
        assert 0 <= cur <= bar.maximum(), f"越界：{cur} 不在 [0,{bar.maximum()}]"
        assert cur >= prev, f"未单调朝目标推进：{prev}→{cur}"
        prev = cur
        if cur == bar.maximum():
            break
    assert bar.value() == bar.maximum(), "缓动最终应吸附到目标"


def test_tick_does_not_overshoot_upward(qtbot):
    area = _make_scrollable(qtbot)
    s = SmoothScroller.attach(area)
    bar = area.verticalScrollBar()
    bar.setValue(bar.maximum())  # 先到底
    s._target = 0.0  # 目标回到顶
    prev = bar.value()
    for _ in range(500):
        s._tick()
        cur = bar.value()
        assert 0 <= cur <= bar.maximum()
        assert cur <= prev, f"未单调向上：{prev}→{cur}"
        prev = cur
        if cur == 0:
            break
    assert bar.value() == 0


def test_external_value_change_resyncs_target(qtbot):
    area = _make_scrollable(qtbot)
    s = SmoothScroller.attach(area)
    bar = area.verticalScrollBar()
    bar.setValue(123)  # 模拟外部滚动（_driving=False）
    assert s._target == 123.0
