from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest

from src.core.mod_manager import ModManager
from src.core.mod_package import write_container_marker
from src.models.mod import AgeRating, Mod, ModInfo
from src.ui.pages.overview import OverviewPage
from tests.helpers import make_mod, register_games, reset_game_service

TAG_MOD_IDS = {
    "one": "00000000000040008000000000000001",
    "two": "00000000000040008000000000000002",
    "outside": "00000000000040008000000000000003",
    "similar": "00000000000040008000000000000004",
    "imported": "00000000000040008000000000000005",
}


def reveal_widget(qtbot, widget):
    """等待布局完成，并滚动真实控件到可点击的位置。"""
    from PySide6.QtWidgets import QScrollArea

    def visible():
        if not widget.isVisible():
            return False
        areas = []
        ancestor = widget.parentWidget()
        while ancestor is not None:
            if isinstance(ancestor, QScrollArea):
                areas.append(ancestor)
                ancestor.ensureWidgetVisible(widget, 0, 0)
            ancestor = ancestor.parentWidget()
        for area in areas:
            viewport = area.viewport()
            if not viewport.rect().contains(widget.mapTo(viewport, widget.rect().center())):
                return False
        window = widget.window()
        hit = window.childAt(widget.mapTo(window, widget.rect().center()))
        return hit is widget or (hit is not None and widget.isAncestorOf(hit))

    qtbot.waitUntil(visible, timeout=2000)


@pytest.fixture
def tag_page(qtbot, temp_data_dir):
    register_games("game-a", "game-b")
    manager = ModManager()
    manager.set_game("game-a")
    category = manager.add_character(name="Category", node_type="category")
    assert category is not None
    nested = manager.add_character(name="Nested", parent_id=category.id, node_type="character")
    other = manager.add_character(name="Other", node_type="category")
    assert nested is not None and other is not None
    nodes = {"cat": category.id, "sub": nested.id, "other": other.id}
    specs = [
        ("one", "First", "cat", ["高清材质", "服装"], AgeRating.G),
        ("two", "Second", "sub", ["高清材质"], AgeRating.X),
        ("outside", "Outside", "other", ["高清材质"], AgeRating.G),
        ("similar", "Similar", "cat", ["超高清材质试验版"], AgeRating.G),
    ]
    for mod_id, name, category, tags, rating in specs:
        mod = make_mod(
            manager._index,
            name,
            mod_id=TAG_MOD_IDS[mod_id],
            node_id=nodes[category],
            info=ModInfo(author="Author", tags=tags, age_rating=rating),
        )
        body = Path(mod.path)
        body.parent.mkdir(parents=True)
        with ZipFile(body, "w") as archive:
            archive.writestr("mod.ini", f"; {name}\n[Constants]\nvalue=1\n")
        mod.content_hash = Mod.compute_sha1(body)
        assert write_container_marker(
            body.parent, body.name, mod.content_hash, mod_id=mod.mod_id, imported_at=mod.imported_at
        )
        assert manager._index.add_mod(mod)
        assert manager._write_mod_sidecar(mod)
    page = OverviewPage()
    qtbot.addWidget(page)
    page.set_mod_manager(manager)
    page.set_current_game("game-a")
    page.set_characters(manager.get_characters_for_ui(), refresh=False)
    page._current_parent_id = nodes["cat"]
    page.resize(1200, 780)
    page.show()
    page.set_current_mods(
        [manager.get_mod(TAG_MOD_IDS["one"]), manager.get_mod(TAG_MOD_IDS["similar"])],
        [
            manager.get_mod(TAG_MOD_IDS["one"]),
            manager.get_mod(TAG_MOD_IDS["two"]),
            manager.get_mod(TAG_MOD_IDS["similar"]),
        ],
    )
    qtbot.wait(20)
    tags = {tag.name: tag.id for tag in manager.get_tag_service().list_tags()}
    yield page, manager, tags
    page.close()
    manager._index.close()
    reset_game_service()


def displayed_mod_ids(page):
    return [entry["id"] for entry in page._display_items if entry["type"] == "mod"]
