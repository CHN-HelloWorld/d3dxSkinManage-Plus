"""验证在线分类菜单的输入法焦点、原名及本地化名称筛选。"""

import gc
import weakref
from pathlib import Path

import pytest
import shiboken6
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtWidgets import QLineEdit, QPushButton, QVBoxLayout, QWidget

from src.services.gamebanana.models import GbCategory
from src.ui import acrylic
from src.ui.widgets.gb_breadcrumb_bar import GbBreadcrumbBar
from src.utils import i18n


def _category(name: str, category_id: int) -> GbCategory:
    """创建不依赖在线接口的分类数据，保留原始名称供选择结果校验。"""
    return GbCategory(id=category_id, name=name, parent_id=None, item_count=0, icon_url=None)


def _commit_input(qapp, text: str) -> None:
    """按系统输入法查询的焦点对象投递已确认文字。"""
    event = QInputMethodEvent()
    event.setCommitString(text)
    qapp.sendEvent(qapp.focusObject(), event)


@pytest.fixture
def picker_host(qtbot, qapp, monkeypatch):
    """在独立窗口内组合主输入框和分类菜单，恢复语言及弹窗过滤器。"""
    catalog = i18n.I18n(directory=Path(__file__).resolve().parents[3] / "config" / "languages")
    catalog.set_language("zh-CN")
    monkeypatch.setattr(i18n, "_i18n", catalog)
    host = QWidget()
    qtbot.addWidget(host)
    layout = QVBoxLayout(host)
    idle = QPushButton("打开分类")
    main_search = QLineEdit()
    bar = GbBreadcrumbBar()
    layout.addWidget(idle)
    layout.addWidget(main_search)
    layout.addWidget(bar)
    bar.set_path([_category("Example Game", 0), _category("Mods", -1), _category("Skins", 5000)])
    bar.set_children(5000, [_category("Characters", 5100), _category("Audio", 5200)])
    popup_filter = acrylic.AcrylicPopupFilter()
    qapp.installEventFilter(popup_filter)
    host.show()
    host.activateWindow()
    qapp.processEvents()
    try:
        yield host, idle, main_search, bar
    finally:
        bar._segments[2].menu().close()
        host.close()
        qapp.removeEventFilter(popup_filter)


@pytest.mark.parametrize("previous_input", [False, True], ids=["first-input", "after-main-input"])
def test_category_input_method_uses_the_visible_search(qtbot, qapp, picker_host, previous_input):
    """首次中文输入及从主输入框切换后，组词与提交均归属于菜单搜索框。"""
    _host, idle, main_search, bar = picker_host
    origin = main_search if previous_input else idle
    origin.setFocus()
    qtbot.waitUntil(origin.hasFocus)
    if previous_input:
        _commit_input(qapp, "主查询")
    before = main_search.text()
    menu = bar._segments[2].menu()
    menu.popup(bar.mapToGlobal(QPoint(0, bar.height())))
    qtbot.waitUntil(menu.isVisible)
    picker = bar._pickers[2]
    qtbot.waitUntil(picker._search.hasFocus)
    qtbot.waitUntil(lambda: qapp.focusObject() is picker._search, timeout=1000)
    preedit = QInputMethodEvent("jue", [])
    qapp.sendEvent(qapp.focusObject(), preedit)
    assert picker._search.text() == ""
    assert picker._list.count() == 2
    _commit_input(qapp, "角色")

    assert picker._search.text() == "角色"
    assert main_search.text() == before
    assert picker._list.count() == 1
    result = picker._list.item(0)
    assert result.text() == "角色"
    assert result.data(Qt.ItemDataRole.UserRole).name == "Characters"


@pytest.mark.parametrize("query", ["  CHAR  ", "角色"])
def test_category_filter_matches_original_and_localized_names(qtbot, qapp, picker_host, query):
    _host, _idle, _main_search, bar = picker_host
    bar._populate_menu(2)
    picker = bar._pickers[2]
    picker._search.setText(query)

    assert picker._list.count() == 1
    assert picker._list.item(0).data(Qt.ItemDataRole.UserRole).id == 5100
    picker._search.clear()
    assert picker._list.count() == 2


def test_category_arrival_preserves_input_entered_while_loading(picker_host):
    """分类异步返回时应用等待期间输入的本地化筛选，避免覆盖用户正在使用的查询。"""
    _host, _idle, _main_search, bar = picker_host
    bar._children_cache.clear()
    bar._populate_menu(2)
    picker = bar._pickers[2]
    picker._search.setText("角色")

    bar.set_children(5000, [_category("Characters", 5100), _category("Audio", 5200)])

    assert picker._search.text() == "角色"
    assert picker._stack.currentWidget() is picker._list
    assert picker._list.count() == 1
    assert picker._list.item(0).data(Qt.ItemDataRole.UserRole).id == 5100


def test_closing_category_menu_restores_main_input(qtbot, qapp, picker_host):
    """关闭类别菜单后输入法回到主输入框，再次打开时使用新的菜单筛选框。"""
    _host, _idle, main_search, bar = picker_host
    main_search.setFocus()
    qtbot.waitUntil(main_search.hasFocus)
    _commit_input(qapp, "主查询")
    menu = bar._segments[2].menu()
    position = bar.mapToGlobal(QPoint(0, bar.height()))
    menu.popup(position)
    picker = bar._pickers[2]
    qtbot.waitUntil(lambda: qapp.focusObject() is picker._search, timeout=1000)
    _commit_input(qapp, "角色")

    qtbot.keyClick(picker._search, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not menu.isVisible())
    qtbot.waitUntil(lambda: qapp.focusObject() is main_search, timeout=1000)
    _commit_input(qapp, "恢复")
    assert main_search.text() == "主查询恢复"
    assert picker._search.text() == "角色"

    menu.popup(position)
    picker = bar._pickers[2]
    qtbot.waitUntil(lambda: qapp.focusObject() is picker._search, timeout=1000)
    assert picker._search.text() == ""
    _commit_input(qapp, "音频")
    assert picker._list.item(0).data(Qt.ItemDataRole.UserRole).id == 5200
    assert main_search.text() == "主查询恢复"


def test_pending_category_focus_releases_destroyed_picker(qapp):
    """菜单在焦点回调投递前销毁时，不遗留选择器的 Python 引用。"""
    bar = GbBreadcrumbBar()
    bar.set_path([_category("Example Game", 0), _category("Mods", -1)])
    bar._populate_menu(1)
    reference = weakref.ref(bar._pickers[1])

    shiboken6.delete(bar)
    del bar
    gc.collect()

    assert reference() is None
