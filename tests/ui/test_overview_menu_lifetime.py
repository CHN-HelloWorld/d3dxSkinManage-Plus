"""分类整理过程中反复打开的临时菜单须在结束后释放。"""

from __future__ import annotations

import pytest
import shiboken6
from PySide6.QtCore import QEvent, QPoint, Qt, QTimer
from PySide6.QtWidgets import QApplication, QMenu

from src.models.mod import Mod
from src.ui.pages.overview import OverviewPage


@pytest.mark.parametrize("surface", ["category", "mod", "table_category", "table_mod", "empty", "table_empty"])
def test_repeated_context_menus_release_widgets(qtbot, qapp, temp_data_dir, surface):
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.show()
    page.set_characters([{"id": "category", "name": "Category", "parent_id": ""}])
    page.set_current_mods([Mod(mod_id="mod", name="Mod", folder_name="Mod")])
    menus = []

    def dismiss_menu():
        """在真实菜单事件循环中按 Escape，保留包装引用以验证原生对象已销毁。"""
        menu = QApplication.activePopupWidget()
        try:
            assert isinstance(menu, QMenu)
            menus.append(menu)
            qtbot.keyClick(menu, Qt.Key.Key_Escape)
        finally:
            if menu is not None and shiboken6.isValid(menu) and menu.isVisible():
                menu.close()

    try:
        qtbot.waitUntil(lambda: "category" in page._vs_card_cache and "mod" in page._vs_card_cache)
        if surface.startswith("table"):
            page._set_view("list")
            if surface == "table_empty":
                pos = QPoint(page._table.viewport().width() - 2, page._table.viewport().height() - 2)
            else:
                wanted = "category" if surface == "table_category" else "mod"
                row = next(i for i, item in enumerate(page._display_items) if item["id"] == wanted)
                pos = page._table.visualItemRect(page._table.item(row, 0)).center()
        for _ in range(4):
            QTimer.singleShot(0, dismiss_menu)
            if surface == "category":
                page._vs_card_cache["category"]._show_context_menu(QPoint(10, 10))
            elif surface == "mod":
                page._show_mod_card_context_menu(page._vs_card_cache["mod"], "mod", QPoint(10, 10))
            elif surface.startswith("table"):
                page._show_table_context_menu(pos)
            else:
                page._show_empty_area_context_menu(QPoint(10, 10))
            qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            qapp.processEvents()
        assert len(menus) == 4
        assert all(not shiboken6.isValid(menu) for menu in menus), "closed context menus must release native resources"
    finally:
        page.close()
