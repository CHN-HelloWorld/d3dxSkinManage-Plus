"""验证插件菜单不会改变卡片窗口类型，并在使用后释放菜单资源。"""

from __future__ import annotations

import pytest
import shiboken6
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QMenu, QVBoxLayout, QWidget

from src.models.mod import Mod
from src.ui import acrylic, plugin_menu
from src.ui.pages.overview import CharacterCard
from src.ui.widgets.mod_card import ModCard
from tests.qt_dialogs import interact


@pytest.mark.parametrize("kind", ["mod", "category"])
@pytest.mark.parametrize("native_ancestor", [False, True])
def test_plugin_menu_preserves_non_native_cards_and_releases_menu(
    qapp, qtbot, monkeypatch, temp_data_dir, kind, native_ancestor
):
    """菜单显示与回调不得将卡片及其兄弟原生化，避免破坏图形效果和控件复用。"""
    host = QWidget()
    qtbot.addWidget(host)
    ancestor = QWidget(host)
    QVBoxLayout(host).addWidget(ancestor)
    if native_ancestor:
        ancestor.winId()
    content = QWidget(ancestor)
    QVBoxLayout(ancestor).addWidget(content)
    layout = QVBoxLayout(content)
    mod_card = ModCard(Mod(mod_id="sample-mod", name="示例 Mod"))
    category_card = CharacterCard({"id": "sample-category", "name": "示例类别"})
    layout.addWidget(mod_card)
    layout.addWidget(category_card)
    calls = []

    def actions(location: str, context_id: str):
        """提供与插件贡献相同的菜单接口，记录实际选中的上下文。"""
        return [("示例插件操作", lambda: calls.append((location, context_id)))]

    monkeypatch.setattr(plugin_menu, "_provider", actions)
    popup_filter = acrylic.AcrylicPopupFilter()
    qapp.installEventFilter(popup_filter)
    menus = []

    def choose_plugin(menu: QMenu):
        """在真实菜单事件循环中检查窗口类型，再通过键盘选择插件命令。"""
        menus.append(menu)
        widgets = [content, *content.findChildren(QWidget)]
        assert not [
            widget
            for widget in widgets
            if not widget.isWindow() and widget.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        ], "插件菜单不能将卡片及其祖先、兄弟控件升级为原生窗口"
        return next(action for action in menu.actions() if action.text() == "示例插件操作")

    try:
        host.show()
        assert not content.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        with interact(QMenu, choose_plugin):
            if kind == "mod":
                position = QPoint(12, 12)
                event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, position, mod_card.mapToGlobal(position))
                mod_card.contextMenuEvent(event)
            else:
                category_card._show_context_menu(QPoint(12, 12))
        expected = ("mod-context", "sample-mod") if kind == "mod" else ("character-context", "sample-category")
        assert calls == [expected]
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert menus and all(not shiboken6.isValid(menu) for menu in menus)
    finally:
        qapp.removeEventFilter(popup_filter)
        host.close()
