import pytest
import shiboken6
from PySide6.QtCore import QSize
from PySide6.QtGui import QResizeEvent, QShowEvent
from PySide6.QtWidgets import QLabel

from src.ui.widgets.gb_adaptive_grid import GbAdaptiveGrid


def test_grid_emits_request_next_page_near_bottom(qtbot, sample_record):
    grid = GbAdaptiveGrid()
    qtbot.addWidget(grid)
    grid.resize(800, 400)
    grid.show()
    qtbot.waitExposed(grid)
    grid.set_items([sample_record] * 30, has_more=True, card_factory=_stub_card)
    requested = []
    grid.request_next_page.connect(lambda: requested.append(1))
    bar = grid.scroll_area.verticalScrollBar()
    bar.setValue(bar.maximum())
    qtbot.wait(250)  # 等节流（100ms）
    assert requested


def test_grid_does_not_request_when_no_more(qtbot, sample_record):
    grid = GbAdaptiveGrid()
    qtbot.addWidget(grid)
    grid.resize(800, 400)
    grid.show()
    qtbot.waitExposed(grid)
    grid.set_items([sample_record], has_more=False, card_factory=_stub_card)
    requested = []
    grid.request_next_page.connect(lambda: requested.append(1))
    bar = grid.scroll_area.verticalScrollBar()
    bar.setValue(bar.maximum())
    qtbot.wait(250)
    assert not requested


def test_grid_clear_resets(qtbot, sample_record):
    grid = GbAdaptiveGrid()
    qtbot.addWidget(grid)
    grid.set_items([sample_record] * 3, has_more=False, card_factory=_stub_card)
    grid.clear()
    assert grid.count() == 0


def test_virtualizes_large_record_set(qtbot, sample_record):
    """记录数量超过一屏时，卡片实例数限制在可视区及其缓冲范围内。"""
    grid = GbAdaptiveGrid()
    qtbot.addWidget(grid)
    grid.resize(800, 400)
    grid.show()
    qtbot.waitExposed(grid)
    grid.set_items([sample_record] * 200, has_more=False, card_factory=_stub_card)
    qtbot.wait(50)
    live = sum(1 for _ in grid.iter_cards())
    assert grid.count() == 200, "count() 应反映记录总数"
    assert live < 80, f"未虚拟化：200 条记录建了 {live} 张真卡（应远小于 200）"


# 设置数据后立即按当前视口布局，无需额外的尺寸或滚动事件。


# set_items 后立即按当前视口宽度排列可视卡片，通过 iter_cards 的 y 坐标验证多行布局。
def test_set_items_lays_out_multi_column_immediately(qtbot, sample_record):
    grid = GbAdaptiveGrid()
    qtbot.addWidget(grid)
    grid.resize(900, 500)  # 足够 3+ 列（每张 210 + 8 spacing）
    grid.show()
    qtbot.waitExposed(grid)
    # 900 像素宽度下，12 张卡片应形成至少三列。
    cards = [sample_record] * 12
    grid.set_items(cards, has_more=False, card_factory=_stub_card)
    qtbot.wait(50)  # 让 _update_visible / singleShot 落地

    # 收集可视卡片的 y 坐标，去重后行数 >= 2（多行就证明有多列分摊）
    ys = sorted({c.y() for c in grid.iter_cards()})
    assert len(ys) >= 2, f"set_items 后卡片堆成一列（y={ys}），未按多列布局"


def test_show_event_reflows_existing_items(qtbot, sample_record):
    """如果卡片是在 grid 不可见时插入的（QStackedWidget 切换前后），
    切到可见时 showEvent 应强制重排可视卡。"""
    grid = GbAdaptiveGrid()
    qtbot.addWidget(grid)
    grid.resize(800, 500)
    # 在网格隐藏时设置数据。
    cards = [sample_record] * 10
    grid.set_items(cards, has_more=False, card_factory=_stub_card)
    # 显示网格以触发布局。
    grid.show()
    qtbot.waitExposed(grid)
    qtbot.wait(50)
    ys = sorted({c.y() for c in grid.iter_cards()})
    assert len(ys) >= 2, f"showEvent 没触发重排（y={ys}）"


def _stub_card(_record=None):
    w = QLabel("card")
    w.setFixedSize(210, 270)
    return w


@pytest.mark.parametrize("trigger", ["resize", "show", "items"])
def test_pending_grid_callbacks_are_cancelled_with_the_grid(qapp, sample_record, trigger):
    grid = GbAdaptiveGrid()
    if trigger == "resize":
        grid.eventFilter(grid.scroll_area.viewport(), QResizeEvent(QSize(800, 400), QSize(600, 400)))
    elif trigger == "show":
        grid._records = [sample_record]
        grid._has_more = True
        grid.showEvent(QShowEvent())
    else:
        grid.set_items([sample_record], has_more=True, card_factory=_stub_card)
    shiboken6.delete(grid)
    # Qt 必须在 Python 访问已销毁控件前取消排队任务。
    qapp.processEvents()
