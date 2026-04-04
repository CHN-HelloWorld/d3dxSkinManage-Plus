from PySide6.QtCore import Qt

from src.services.gamebanana.models import GbCategory
from src.ui.widgets.gb_breadcrumb_bar import GbBreadcrumbBar


def _seg(name, cid=0):
    return GbCategory(id=cid, name=name, parent_id=None, item_count=0, icon_url=None)


def test_set_path_renders_each_segment(qtbot):
    bar = GbBreadcrumbBar()
    qtbot.addWidget(bar)
    bar.set_path([_seg("ExampleGameA", 0), _seg("Mods", -1), _seg("Skins", 5000)])
    labels = bar.segment_texts()
    # 首段（游戏名）按原文显示；其余段使用当前语言包的 gamebanana 映射：
    # "Mods"→"Mod"、"Skins"→"皮肤"。本地化是面包屑的既定行为。
    assert labels == ["ExampleGameA", "Mod", "皮肤"]


def test_first_segment_has_no_dropdown_arrow(qtbot):
    bar = GbBreadcrumbBar()
    qtbot.addWidget(bar)
    bar.set_path([_seg("ExampleGameA", 0), _seg("Mods", -1)])
    assert bar.segment_has_dropdown(0) is False
    assert bar.segment_has_dropdown(1) is True


def test_clicking_segment_emits_truncated_path(qtbot):
    bar = GbBreadcrumbBar()
    qtbot.addWidget(bar)
    path = [_seg("ExampleGameA", 0), _seg("Mods", -1), _seg("Skins", 5000), _seg("Chars", 5100)]
    bar.set_path(path)
    with qtbot.waitSignal(bar.path_changed, timeout=1000) as blocker:
        bar.click_segment(1)  # 点 Mods
    assert [c.name for c in blocker.args[0]] == ["ExampleGameA", "Mods"]


def test_dropdown_emits_children_request(qtbot):
    bar = GbBreadcrumbBar()
    qtbot.addWidget(bar)
    bar.set_path([_seg("ExampleGameA", 0), _seg("Mods", -1), _seg("Skins", 5000)])
    with qtbot.waitSignal(bar.children_requested, timeout=1000) as blocker:
        bar.open_dropdown(2)
    assert blocker.args[0] == 5000


def test_failed_dropdown_can_retry_then_display_categories(qtbot):
    bar = GbBreadcrumbBar()
    qtbot.addWidget(bar)
    bar.set_path([_seg("Game"), _seg("Mods", -1), _seg("Skins", 5000)])
    requested = []
    bar.children_requested.connect(requested.append)
    bar._populate_menu(2)
    picker = bar._pickers[2]
    bar.set_children_error(5000, "网络连接失败")
    assert picker._stack.currentWidget() == picker._error
    assert not picker._retry.icon().isNull()
    picker._retry.click()
    assert requested == [5000, 5000]
    assert picker._stack.currentWidget() == picker._loading
    picker._retry.click()
    assert requested == [5000, 5000]
    bar.set_children(5000, [_seg("Characters", 5100)])
    assert picker._stack.currentWidget() == picker._list
    assert picker._list.item(0).data(Qt.ItemDataRole.UserRole).id == 5100
