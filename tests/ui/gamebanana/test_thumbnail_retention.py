from dataclasses import replace

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QLabel

from src.services.gamebanana.image_loader import ImageLoader
from src.ui.widgets.gb_adaptive_grid import GbAdaptiveGrid
from src.ui.widgets.gb_mod_card import GbModCard


@pytest.fixture
def thumbnail_loader(qapp, tmp_path, monkeypatch):
    from src.services.gamebanana import image_loader as image_module

    image = QPixmap(24, 18)
    image.fill(QColor("red"))
    path = tmp_path / "thumbnail.png"
    assert image.save(str(path))
    fetched, transformed = [], []

    class FileCache:
        async def fetch(self, url):
            fetched.append(url)
            return path

    transform = image_module._transform_pixmap

    def tracked_transform(*args):
        transformed.append(args)
        return transform(*args)

    monkeypatch.setattr(image_module, "_transform_pixmap", tracked_transform)
    loader = ImageLoader(FileCache())
    yield loader, fetched, transformed
    loader.shutdown()


def _assert_red_thumbnail(card):
    assert card.thumbnail.pixmap().toImage().pixelColor(100, 80) == QColor("red")
    assert card.thumbnail._shimmer.isHidden()
    assert card._thumb_loaded


def test_native_deferred_card_restores_cached_pixels_without_a_shimmer(
    qtbot,
    sample_record,
    thumbnail_loader,
):
    loader, fetched, transformed = thumbnail_loader
    warm = GbModCard(sample_record, image_loader=loader)
    qtbot.addWidget(warm)
    qtbot.waitUntil(lambda: warm.thumbnail._sharp is not None, timeout=3000)
    returning = GbModCard(sample_record, image_loader=loader, defer_image=True)
    qtbot.addWidget(returning)
    returning.show()

    _assert_red_thumbnail(returning)
    returning.set_record(replace(sample_record, thumbnail_url="cold"), defer_image=True)
    returning.set_record(sample_record, defer_image=True)
    _assert_red_thumbnail(returning)
    assert len(fetched) == len(transformed) == 1


def test_prefetch_loads_records_beyond_widgets_and_return_is_immediate(
    qtbot,
    sample_record,
    thumbnail_loader,
):
    loader, fetched, transformed = thumbnail_loader
    grid = GbAdaptiveGrid()
    qtbot.addWidget(grid)
    grid.resize(470, 760)
    grid.show()
    qtbot.wait(10)
    records = [replace(sample_record, mod_id=i, thumbnail_url=f"image-{i}") for i in range(100)]
    prefetched = []

    def prefetch(record):
        prefetched.append(record.mod_id)
        return loader.prefetch(record.thumbnail_url, size=QSize(210, 160), crop_to_size=True)

    def cancel_prefetch(record):
        loader.cancel_prefetch(record.thumbnail_url, size=QSize(210, 160), crop_to_size=True)

    grid.set_items(
        records,
        has_more=False,
        card_factory=lambda record: GbModCard(record, image_loader=loader, defer_image=True),
        card_binder=lambda card, record: card.set_record(record, defer_image=True),
        image_prefetcher=prefetch,
        image_prefetch_canceller=cancel_prefetch,
    )
    widget_limit = len(grid._cards)
    outside = max(grid._cards) + 1
    probe = QLabel()
    qtbot.addWidget(probe)
    qtbot.waitUntil(
        lambda: loader.try_cached(records[outside].thumbnail_url, probe, size=QSize(210, 160), crop_to_size=True),
        timeout=5000,
    )
    assert outside in prefetched
    assert outside not in grid._cards
    assert len(grid._cards) == widget_limit
    requests_before = len(fetched)
    transforms_before = len(transformed)
    grid.scroll_area.verticalScrollBar().setValue((outside // grid._cols) * grid._row_h)
    grid._update_visible()
    _assert_red_thumbnail(grid._cards[outside])
    assert len(fetched) == requests_before and len(transformed) == transforms_before


def test_image_batches_progress_during_continuous_scroll(qtbot, sample_record):
    grid = GbAdaptiveGrid()
    qtbot.addWidget(grid)
    grid.resize(1200, 900)
    grid.show()
    qtbot.wait(10)
    requested = []

    class DeferredCard(QLabel):
        def __init__(self, record):
            super().__init__(record.name)
            self.record = record
            self.setFixedSize(210, 270)
            self._thumb_loaded = False

        def load_thumbnail(self):
            if not self._thumb_loaded:
                self._thumb_loaded = True
                requested.append(self.record.mod_id)

    records = [replace(sample_record, mod_id=i) for i in range(400)]
    grid.set_items(records, has_more=False, card_factory=DeferredCard)
    bar = grid.scroll_area.verticalScrollBar()
    # 每次滚动事件均在八十毫秒静默期限之前到达。
    for position in range(1, 26):
        bar.setValue(position)
        qtbot.wait(10)

    assert grid.is_scrolling()
    assert requested, "image requests must progress before scrolling stops"
    first_visible = bar.value() // grid._row_h * grid._cols
    assert requested[0] == first_visible


def test_each_image_tick_submits_a_small_batch_with_visible_cards_first(qtbot, sample_record):
    grid = GbAdaptiveGrid()
    qtbot.addWidget(grid)
    grid.resize(1200, 900)
    grid.show()
    qtbot.wait(10)
    requested = []

    class DeferredCard(QLabel):
        def __init__(self, record):
            super().__init__(record.name)
            self.setFixedSize(210, 270)
            self.mod_id = record.mod_id

        def load_thumbnail(self):
            requested.append(self.mod_id)

    grid.set_items([replace(sample_record, mod_id=i) for i in range(300)], has_more=False, card_factory=DeferredCard)
    grid.scroll_area.verticalScrollBar().setValue(20 * grid._row_h)
    grid._update_visible()
    grid._load_visible_images()

    assert 1 <= len(requested) <= 4
    assert requested[0] == 20 * grid._cols


def test_prefetch_window_retires_departed_records_without_growing_widgets(qtbot, sample_record):
    grid = GbAdaptiveGrid()
    qtbot.addWidget(grid)
    grid.resize(470, 760)
    grid.show()
    qtbot.wait(10)
    records = [replace(sample_record, mod_id=i) for i in range(300)]
    interests = set()
    cancelled = []

    def prefetch(record):
        interests.add(record.mod_id)
        return True

    def cancel(record):
        interests.discard(record.mod_id)
        cancelled.append(record.mod_id)

    def create(record):
        card = QLabel(record.name)
        card.setFixedSize(210, 270)
        return card

    grid.set_items(
        records, has_more=False, card_factory=create, image_prefetcher=prefetch, image_prefetch_canceller=cancel
    )
    qtbot.waitUntil(lambda: bool(interests), timeout=1500)
    old_interests = set(interests)
    grid.scroll_area.verticalScrollBar().setValue(50 * grid._row_h)
    grid._update_visible()
    qtbot.waitUntil(lambda: bool(interests - old_interests), timeout=1500)

    assert old_interests <= set(cancelled)
    assert not interests & old_interests
    assert len(grid._cards) <= 14
    grid.clear()
    assert not interests


@pytest.mark.parametrize("render_path", ["page", "filter"])
def test_store_prepares_thumbnails_before_building_their_cards(
    qtbot,
    sample_record,
    fake_gb_client,
    thumbnail_loader,
    render_path,
):
    from src.ui.pages.gamebanana_store import GameBananaStorePage

    loader, _fetched, _transformed = thumbnail_loader
    page = GameBananaStorePage(client=fake_gb_client, image_loader=loader)
    qtbot.addWidget(page)
    page.resize(750, 1000)
    page.show()
    records = [replace(sample_record, mod_id=i, thumbnail_url=f"store-{i}") for i in range(60)]
    try:
        if render_path == "page":
            page._render_records(records, False, page._request_gen)
        else:
            page._all_records = records
            page._rerender_from_cache()
        qtbot.waitUntil(lambda: page.grid.count() == 60, timeout=3000)
        outside = max(page.grid._cards) + 1
        probe = QLabel()
        qtbot.addWidget(probe)
        qtbot.waitUntil(
            lambda: loader.try_cached(records[outside].thumbnail_url, probe, size=QSize(210, 160), crop_to_size=True),
            timeout=3000,
        )
        assert probe.pixmap().toImage().pixelColor(100, 80) == QColor("red")
        assert outside not in page.grid._cards
    finally:
        page.grid.clear()
        page.shutdown()
