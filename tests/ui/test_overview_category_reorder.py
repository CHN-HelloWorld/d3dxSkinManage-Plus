"""验证自定义类别顺序及内部拖放与外部导入的隔离。"""

from contextlib import nullcontext

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from src.models.mod import Mod
from src.ui import win_dnd_admin
from src.ui.pages import overview as overview_module
from src.ui.pages.overview import OverviewPage


@pytest.fixture
def page(qtbot, temp_data_dir):
    """创建三张同级类别卡片，不加载真实资源。"""
    result = OverviewPage()
    qtbot.addWidget(result)
    result.resize(1000, 650)
    result.set_current_game("game.a")
    result.set_characters(
        [
            {"id": "category-a", "name": "Alpha"},
            {"id": "category-b", "name": "Beta"},
            {"id": "category-c", "name": "Charlie", "node_type": "character"},
        ]
    )
    yield result
    result.close()


def _custom(page):
    """通过现有排序入口切换到自定义。"""
    index = page._sort_combo.findData("custom")
    assert index >= 0
    page._sort_combo.setCurrentIndex(index)


def _ids(page):
    """读取实际呈现的条目顺序。"""
    return [item["id"] for item in page._display_items]


def test_custom_order_saves_immediately_and_survives_recreation(page, qtbot):
    page.set_current_mods([Mod(mod_id="mod-z", name="Zulu"), Mod(mod_id="mod-a", name="Alpha")])
    _custom(page)
    assert page._move_category_before("category-c", "category-a")
    assert _ids(page) == ["category-c", "category-a", "category-b", "mod-a", "mod-z"]
    restored = OverviewPage()
    qtbot.addWidget(restored)
    restored.set_current_game("game.a")
    restored.set_characters(page._all_characters)
    assert restored._sort_key == "custom"
    restored._set_view("list")
    assert _ids(restored) == ["category-c", "category-a", "category-b"]


def test_deleted_renamed_and_new_categories_keep_survivor_order(page):
    _custom(page)
    assert page._move_category_before("category-c", "category-a")
    page.set_characters(
        [
            {"id": "category-d", "name": "A new category"},
            {"id": "category-b", "name": "Beta"},
            {"id": "category-c", "name": "Renamed"},
        ]
    )
    expected = ["category-c", "category-b", "category-d"]
    assert _ids(page) == expected
    assert page._category_order.ordered_ids("game.a", "", expected) == expected


def test_builtin_sort_does_not_erase_custom_order(page):
    _custom(page)
    assert page._move_category_before("category-c", "category-a")
    page._sort_combo.setCurrentIndex(page._sort_combo.findData("alpha_asc"))
    assert _ids(page) == ["category-a", "category-b", "category-c"]
    _custom(page)
    assert _ids(page) == ["category-c", "category-a", "category-b"]


@pytest.mark.parametrize("view_mode", ["grid", "list"])
@pytest.mark.parametrize("blocked_by", ["search", "type", "age", "loading"])
def test_reorder_is_disabled_for_filtered_or_loading_views(page, blocked_by, view_mode):
    _custom(page)
    page._set_view(view_mode)
    if blocked_by == "search":
        page._search_input.setText("Alpha")
    elif blocked_by == "type":
        page._type_filter.setCurrentIndex(1)
    elif blocked_by == "age":
        page._type_filter.setCurrentIndex(page._type_filter.findData("mod"))
        page._age_filter.setCurrentIndex(1)
    else:
        page.begin_loading()
    before = page._category_order.path.read_bytes()
    assert not page._can_reorder_categories()
    assert not page._move_category_before("category-c", "category-a")
    assert page._category_order.path.read_bytes() == before


def test_unknown_internal_drop_never_enters_file_import(page, monkeypatch):
    _custom(page)
    calls = []
    monkeypatch.setattr(page, "_process_raw_urilist_async", lambda *args: calls.append(args))
    mime = QMimeData()
    mime.setData("application/x-d3dx-category-id", b"category-c")
    event = QDropEvent(
        QPoint(10, 10), Qt.DropAction.MoveAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    )
    page.dropEvent(event)
    assert not event.isAccepted()
    assert calls == []


def test_reordering_does_not_cross_game_or_parent_scope(page):
    _custom(page)
    assert page._move_category_before("category-c", "category-a")
    page.set_characters(
        [
            {"id": "parent", "name": "Parent"},
            {"id": "category-a", "name": "Alpha", "parent_id": "parent"},
            {"id": "category-b", "name": "Beta", "parent_id": "parent"},
            {"id": "category-c", "name": "Charlie", "parent_id": "parent"},
        ]
    )
    page._current_parent_id = "parent"
    page._refresh_current_level()
    assert _ids(page) == ["category-a", "category-b", "category-c"]
    assert not page._move_category_before("parent", "category-a")
    assert page._move_category_before("category-b", "category-a")
    page.set_current_game("game-b")
    page.set_characters(
        [
            {"id": "other-a", "name": "Alpha"},
            {"id": "other-b", "name": "Beta"},
        ]
    )
    assert _ids(page) == ["other-a", "other-b"]
    orders = page._category_order._orders()
    assert orders["game.a"][""] == ["parent"]
    assert orders["game.a"]["parent"] == ["category-b", "category-a", "category-c"]
    assert "game-b" not in orders


@pytest.mark.parametrize("item_type", ["category", "mod"])
def test_long_press_uses_internal_drag_and_saves_the_drop(page, qtbot, monkeypatch, item_type):
    """通过真实长按定时器启动拖动，受控落点保留页面的来源验证与重排流程。"""
    prefix = []
    if item_type == "mod":
        page.resize(1200, 1000)
        page.set_current_mods([Mod(mod_id=f"mod-{suffix}", name=suffix) for suffix in ("a", "b", "c")])
        prefix = ["category-a", "category-b", "category-c"]
    expected = prefix + [f"{item_type}-c", f"{item_type}-a", f"{item_type}-b"]
    _custom(page)
    page.show()
    qtbot.waitUntil(lambda: len(page._vs_card_cache) == len(expected))
    source_card = page._vs_card_cache[f"{item_type}-c"]
    target_card = page._vs_card_cache[f"{item_type}-a"]
    position = QPointF(target_card.mapTo(page, target_card.rect().center()))
    suspended = []

    class ControlledDrop:
        """提供与 Qt 落点一致的来源、载荷与页面坐标。"""

        accepted = False

        def __init__(self, source, mime):
            """保留拖动来源和内部载荷。"""
            self._source = source
            self._mime = mime

        def source(self):
            """返回启动拖动的原始卡片。"""
            return self._source

        def mimeData(self):
            """返回内部类别载荷。"""
            return self._mime

        def position(self):
            """返回目标卡片在页面中的中心。"""
            return position

        def acceptProposedAction(self):
            """记录页面接受移动。"""
            self.accepted = True

        def ignore(self):
            """记录页面拒绝移动。"""
            self.accepted = False

    class ControlledDrag:
        """替换系统拖动事件循环，但保留真实长按和页面处理。"""

        def __init__(self, source):
            """保留原始卡片来源。"""
            self.source = source

        def setMimeData(self, mime):
            """保存页面创建的私有 MIME 载荷。"""
            self.mime = mime

        def setPixmap(self, pixmap):
            """确认拖动使用真实卡片截图。"""
            assert not pixmap.isNull()

        def setHotSpot(self, point):
            """接收鼠标按下位置。"""

        def exec(self, action):
            """在实际拖动作用域中提交同页落点。"""
            assert action == Qt.DropAction.MoveAction
            event = ControlledDrop(self.source, self.mime)
            page.dragEnterEvent(event)
            assert event.accepted
            page.dragMoveEvent(event)
            assert event.accepted
            page.dropEvent(event)
            assert event.accepted

        def deleteLater(self):
            """受控对象不持有原生拖动资源。"""

    def suspend(owner):
        """记录管理员兼容通道被正确调用。"""
        suspended.append(owner)
        return nullcontext()

    monkeypatch.setattr(overview_module, "QDrag", ControlledDrag)
    monkeypatch.setattr(win_dnd_admin, "suspend_for_internal_drag", suspend)
    qtbot.mousePress(source_card, Qt.MouseButton.LeftButton, pos=source_card.rect().center())
    qtbot.waitUntil(lambda: _ids(page) == expected)
    qtbot.mouseRelease(source_card, Qt.MouseButton.LeftButton)
    assert suspended == [page]
    assert page._category_drag_source is None
    assert page._category_drag_scope is None
    assert not source_card._press_timer.isActive()


def test_list_drop_targets_use_row_halves_and_reject_mod_rows(page):
    _custom(page)
    page.set_current_mods([Mod(mod_id="mod-a", name="Example Mod")])
    page._set_view("list")
    page.show()
    viewport = page._table.viewport()
    first = page._table.visualItemRect(page._table.item(0, 0))
    last = page._table.visualItemRect(page._table.item(2, 0))
    mod_rect = page._table.visualItemRect(page._table.item(3, 0))
    assert page._category_drop_target(viewport.mapTo(page, first.topLeft() + QPoint(8, 4))) == (True, "category-a")
    assert page._category_drop_target(viewport.mapTo(page, first.bottomLeft() + QPoint(8, -4))) == (True, "category-b")
    assert page._category_drop_target(viewport.mapTo(page, last.bottomLeft() + QPoint(8, -4))) == (True, None)
    assert page._category_drop_target(viewport.mapTo(page, mod_rect.center())) == (False, None)


def test_list_mod_rows_use_separate_drag_scope_and_builtin_sort_disables_drag(page, monkeypatch):
    _custom(page)
    page.set_current_mods([Mod(mod_id="mod-a", name="Example Mod")])
    page._set_view("list")
    started = []
    monkeypatch.setattr(page, "_exec_category_drag", lambda *args, **kwargs: started.append((args, kwargs)))
    before = page._category_order.path.read_bytes()
    page._table.setCurrentCell(3, 0)
    page._table.startDrag(Qt.DropAction.MoveAction)
    assert len(started) == 1
    assert started[0][1]["item_type"] == "mod"
    started.clear()
    page._sort_combo.setCurrentIndex(page._sort_combo.findData("alpha_asc"))
    assert not page._table.dragEnabled()
    page._table.setCurrentCell(0, 0)
    page._table.startDrag(Qt.DropAction.MoveAction)
    assert started == []
    assert page._category_order.path.read_bytes() == before


def test_list_internal_drop_support_does_not_intercept_external_file_import(page, qtbot, monkeypatch):
    """管理员旧式文件拖放仍应越过列表模型并交给总览导入处理。"""
    page._set_view("list")
    page._current_parent_id = "category-a"
    page._refresh_current_level()
    page.show()
    calls = []
    monkeypatch.setattr(page, "_process_raw_urilist_async", lambda *args: calls.append(args))
    monkeypatch.setattr(win_dnd_admin, "_last_mime", None)
    position = page._table.viewport().mapTo(page, QPoint(10, 10))
    assert win_dnd_admin._deliver(page, ["D:/example.zip"], position)
    qtbot.waitUntil(lambda: len(calls) == 1)
    assert calls[0][1] == "category-a"


def test_list_drag_edge_scroll_stops_when_drag_finishes(page):
    """长列表拖动可滚动访问屏外类别，收尾必须停止定时器并隐藏插入线。"""
    page.set_characters([{"id": f"category-{index}", "name": f"Category {index:02d}"} for index in range(40)])
    _custom(page)
    page._set_view("list")
    page.show()
    viewport = page._table.viewport()
    page._category_drag_source = page._table
    page._category_drag_scope = ("game.a", "", "list", "category", "category-0")
    try:
        position = viewport.mapTo(page, QPoint(8, viewport.height() - 8))
        page._update_category_list_drop_hover(position)
        assert page._category_list_drag_timer.isActive()
        before = page._table.verticalScrollBar().value()
        page._scroll_category_list_drag()
        assert page._table.verticalScrollBar().value() > before
    finally:
        page._category_drag_source = None
        page._category_drag_scope = None
        page._clear_category_drop_hover()
    assert not page._category_list_drag_timer.isActive()
    assert not page._category_drop_line.isVisible()


def test_card_reorder_grip_stays_at_top_right_after_resizing(page, qtbot):
    _custom(page)
    page.show()
    qtbot.waitUntil(lambda: len(page._vs_card_cache) == 3)
    card = page._vs_card_cache["category-a"]
    assert card._reorder_grip.isVisible()
    assert card._reorder_grip.y() == 4
    assert card._reorder_grip.geometry().right() == card.width() - 5
    card.resize_card(220, 260)
    assert card._reorder_grip.y() == 4
    assert card._reorder_grip.geometry().right() == card.width() - 5


def test_list_reorder_shares_saved_order_with_grid_and_recreation(page, qtbot):
    _custom(page)
    page._set_view("list")
    assert page._can_reorder_categories()
    assert page._table.dragEnabled()
    assert page._move_category_before("category-c", "category-a")
    assert _ids(page) == ["category-c", "category-a", "category-b"]
    page._set_view("grid")
    assert _ids(page) == ["category-c", "category-a", "category-b"]
    restored = OverviewPage()
    qtbot.addWidget(restored)
    restored.set_current_game("game.a")
    restored.set_characters(page._all_characters)
    assert _ids(restored) == ["category-c", "category-a", "category-b"]


@pytest.mark.parametrize("item_type", ["category", "mod"])
def test_list_native_drag_delivers_viewport_drop_and_preserves_selection(page, qtbot, monkeypatch, item_type):
    """保留真实表格鼠标手势及视口事件，仅替换系统拖动循环。"""
    prefix = []
    if item_type == "mod":
        page.set_current_mods([Mod(mod_id=f"mod-{suffix}", name=suffix) for suffix in ("a", "b", "c")])
        prefix = ["category-a", "category-b", "category-c"]
    expected = prefix + [f"{item_type}-c", f"{item_type}-a", f"{item_type}-b"]
    _custom(page)
    page._set_view("list")
    page.show()
    viewport = page._table.viewport()
    source_position = page._table.visualItemRect(page._table.item(len(prefix) + 2, 0)).center()
    destination = page._table.visualItemRect(page._table.item(len(prefix), 0)).topLeft() + QPoint(8, 4)
    suspended = []

    class InternalEnter(QDragEnterEvent):
        """带真实表格来源的进入事件。"""

        def source(self):
            """返回发起拖动的表格。"""
            return page._table

    class InternalMove(QDragMoveEvent):
        """带真实表格来源的悬停事件。"""

        def source(self):
            """返回发起拖动的表格。"""
            return page._table

    class InternalDrop(QDropEvent):
        """带真实表格来源的落下事件。"""

        def source(self):
            """返回发起拖动的表格。"""
            return page._table

    class ControlledDrag:
        """用原生 Qt 视口事件代替平台拖动循环。"""

        def __init__(self, source):
            """验证拖动由表格而非缓存卡片发起。"""
            assert source is page._table

        def setMimeData(self, mime):
            """保留页面生成的私有载荷。"""
            self.mime = mime

        def setPixmap(self, pixmap):
            """确保拖动预览为实际行截图。"""
            assert not pixmap.isNull()

        def setHotSpot(self, position):
            """接收行内鼠标位置。"""

        def exec(self, action):
            """按原生顺序投递进入、悬停和落下。"""
            assert action == Qt.DropAction.MoveAction
            for event_type in (InternalEnter, InternalMove, InternalDrop):
                event = event_type(
                    destination, action, self.mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
                )
                QApplication.sendEvent(viewport, event)
                assert event.isAccepted()
            assert page._selected_ids == {f"{item_type}-c"}

        def deleteLater(self):
            """受控对象无平台资源。"""

    def suspend(owner):
        """验证管理员模式内部拖动使用同一兼容通道。"""
        suspended.append(owner)
        return nullcontext()

    monkeypatch.setattr(overview_module, "QDrag", ControlledDrag)
    monkeypatch.setattr(win_dnd_admin, "suspend_for_internal_drag", suspend)
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=source_position)
    for distance in (QApplication.startDragDistance() + 2, QApplication.startDragDistance() + 10):
        event = QMouseEvent(
            QMouseEvent.Type.MouseMove,
            QPointF(source_position + QPoint(distance, 0)),
            QPointF(viewport.mapToGlobal(source_position + QPoint(distance, 0))),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(viewport, event)
        if _ids(page) == expected:
            break
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=destination)
    assert _ids(page) == expected
    assert suspended == [page]
    assert page._selected_ids == {f"{item_type}-c"}
    assert page._category_drag_source is None
    assert page._category_drag_scope is None
