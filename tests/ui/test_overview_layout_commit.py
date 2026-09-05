from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPoint, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication

from src.models.mod import Mod, ModInfo
from src.ui.pages import overview


@pytest.fixture
def layout_page(qtbot, temp_data_dir, monkeypatch, request):
    previous_cursor = QCursor.pos()
    request.addfinalizer(lambda: QCursor.setPos(previous_cursor))
    monkeypatch.setattr(overview, "perf_counter", lambda: 0.0)
    page = overview.OverviewPage()
    qtbot.addWidget(page)
    request.addfinalizer(page.close)
    page.resize(1100, 750)
    page.show()
    # 静态像素缓存不包含悬停效果，先将指针移出页面。
    QCursor.setPos(page.mapToGlobal(QPoint(-20, -20)))
    qtbot.wait(20)
    return page


def _contents(page, prefix, count, kind):
    if kind == "category":
        page.set_characters(
            [{"id": f"{prefix}-{i}", "name": f"{prefix} category {i}", "mod_count": i} for i in range(count)]
        )
    else:
        mods = [
            Mod(mod_id=f"{prefix}-{i}", name=f"{prefix} mod {i}", info=ModInfo(tags=[f"{prefix} tag", "Shared"]))
            for i in range(count)
        ]
        page.set_current_mods(mods, mods)


@pytest.mark.parametrize("old_count,new_count", [(120, 1), (1, 120)])
@pytest.mark.parametrize("kind", ["category", "mod"])
def test_directory_commit_has_final_viewport_geometry(layout_page, qtbot, old_count, new_count, kind):
    page = layout_page
    _contents(page, "old", old_count, kind)
    qtbot.wait(30)

    unrelated_turns = []
    QTimer.singleShot(0, lambda: unrelated_turns.append(True))
    _contents(page, "new", new_count, kind)
    assert not unrelated_turns, "layout commits must not reenter unrelated event callbacks"
    viewport = page._scroll_area.viewport()
    first_geometry = (viewport.geometry().getRect(), page._scroll_content.size())
    first_frame = viewport.grab().toImage()
    qtbot.wait(30)

    assert first_geometry == (viewport.geometry().getRect(), page._scroll_content.size())
    assert first_frame == viewport.grab().toImage(), "the first directory frame must already be final"


@pytest.mark.parametrize("kind", ["category", "mod"])
def test_rebound_card_layout_requests_do_not_discard_first_frame(layout_page, qtbot, kind):
    page = layout_page
    _contents(page, "old", 120, kind)
    qtbot.wait(30)
    old_cards = set(page._vs_card_cache.values())

    _contents(page, "new", 120, kind)
    cards = [card for card in page._vs_card_cache.values() if card.y() < page._scroll_area.viewport().height()]
    assert cards and all(card in old_cards for card in cards)
    first_frame = page._scroll_area.viewport().grab().toImage()
    caches = [card.graphicsEffect()._source_cache for card in cards]
    assert all(cache._key is not None and not cache._pixmap.isNull() for cache in caches)
    first_pixels = [cache._pixmap.cacheKey() for cache in caches]

    QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
    assert all(cache._key is not None for cache in caches), "queued layout requests discarded the first frame"
    assert first_frame == page._scroll_area.viewport().grab().toImage()
    assert first_pixels == [cache._pixmap.cacheKey() for cache in caches], "unchanged pixels must be reused"
