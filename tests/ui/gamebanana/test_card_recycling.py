from dataclasses import replace

import shiboken6
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QDesktopServices, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QScrollArea, QWidget

from src.services.gamebanana.models import GbAuthor, GbModel
from src.ui.widgets.gb_adaptive_grid import GbAdaptiveGrid
from src.ui.widgets.gb_mod_card import GbModCard
from src.ui.widgets.scroll_edge_fade import ScrollEdgeFade
from tests.qt_dialogs import interact


def test_online_scroll_reuses_cards_and_emits_current_record(qtbot, sample_record, fake_image_loader):
    grid = GbAdaptiveGrid()
    qtbot.addWidget(grid)
    grid.resize(900, 600)
    grid.show()
    qtbot.wait(10)
    records = [replace(sample_record, mod_id=i, name=f"Mod {i}") for i in range(800)]
    clicked = []

    def create(record):
        card = GbModCard(record, image_loader=fake_image_loader, defer_image=True)
        card.clicked.connect(lambda rec: clicked.append(rec.mod_id))
        return card

    def bind(card, record):
        card.set_record(record, defer_image=True, is_installed=record.mod_id == 245)
        card.set_selected(record.mod_id == 246)

    grid.set_items(records, has_more=False, card_factory=create, card_binder=bind)
    bar = grid.scroll_area.verticalScrollBar()
    bar.setValue(20 * grid._row_h)
    grid._update_visible()
    original = set(grid.iter_cards())
    bar.setValue(60 * grid._row_h)
    grid._update_visible()

    assert set(grid.iter_cards()) <= original
    for index, card in grid._cards.items():
        assert card.record is records[index]
        assert card._selected == (index == 246)
        assert card.installed_badge.isHidden() == (index != 245)
    index, card = next(iter(grid._cards.items()))
    qtbot.mouseClick(card, Qt.MouseButton.LeftButton)
    assert clicked == [index]
    assert grid.count() == 800


def test_online_rebind_resets_text_badges_and_thumbnail(qtbot, sample_record, fake_image_loader):
    first = replace(sample_record, model=GbModel.WIP, is_nsfw=True)
    card = GbModCard(first, image_loader=fake_image_loader, is_installed=True, defer_image=True)
    qtbot.addWidget(card)
    card.show()
    card.set_selected(True)
    image = QPixmap(210, 160)
    image.fill(QColor("red"))
    card.thumbnail.setPixmap(image)
    title_widget = card.title_label
    image_widget = card.thumbnail
    second = replace(
        sample_record,
        mod_id=456,
        name="A different mod",
        is_nsfw=False,
        submitter=GbAuthor(8, "New author", "", ""),
        view_count=456,
        like_count=19,
        download_count=0,
        thumbnail_url="https://example.invalid/new.png",
    )

    card.set_record(second, download_count_pending=True, defer_image=True)

    assert card.title_label is title_widget and card.thumbnail is image_widget
    assert card.title_label.text() == "A different mod"
    assert card.author_label.text() == "New author"
    assert card.view_label.text() == "456" and card.like_label.text() == "19"
    assert card.dl_label.text() == "…"
    assert card.type_badge.isHidden() and card.nsfw_badge.isHidden()
    assert card.installed_badge.isHidden() and not card._selected
    assert not card.thumbnail._blur_on
    assert card.thumbnail._sharp is None
    assert card.thumbnail.pixmap().isNull()
    assert card.thumbnail._shimmer.isVisible()


def test_online_clear_cancels_images_and_clears_live_cards(qtbot, sample_record, fake_image_loader):
    grid = GbAdaptiveGrid()
    qtbot.addWidget(grid)
    grid.resize(800, 500)
    grid.show()
    qtbot.wait(10)
    cards = []

    def create(record):
        card = GbModCard(record, image_loader=fake_image_loader, defer_image=True)
        cards.append(card)
        return card

    grid.set_items(
        [sample_record] * 400,
        has_more=False,
        card_factory=create,
        card_binder=lambda card, rec: card.set_record(rec, defer_image=True),
    )
    grid.scroll_area.verticalScrollBar().setValue(5000)
    grid._update_visible()
    grid.clear()

    assert grid.count() == 0 and not list(grid.iter_cards())
    assert all(card.isHidden() for card in cards)


def test_online_menu_keeps_url_from_opening_record(qtbot, sample_record, fake_image_loader, monkeypatch):
    card = GbModCard(sample_record, image_loader=fake_image_loader, defer_image=True)
    qtbot.addWidget(card)
    menu = card._build_context_menu()
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))

    card.set_record(replace(sample_record, mod_id=456, page_url="https://gamebanana.com/mods/456"))
    menu.actions()[0].trigger()
    menu.actions()[1].trigger()

    assert QApplication.clipboard().text() == sample_record.page_url
    assert opened == [sample_record.page_url]


def test_cached_online_card_updates_image_and_status(qtbot, sample_record, fake_image_loader):
    area = QScrollArea()
    qtbot.addWidget(area)
    area.resize(400, 340)
    content = QWidget()
    content.resize(380, 1800)
    card = GbModCard(replace(sample_record, thumbnail_url=""), image_loader=fake_image_loader, parent=content)
    card.move(12, 400)
    card.show()
    area.setWidget(content)
    fade = ScrollEdgeFade(area, lambda: [card])
    area.show()
    area.verticalScrollBar().setValue(350)
    qtbot.wait(10)
    for color in ("red", "blue"):
        preview = QPixmap(210, 160)
        preview.fill(QColor(color))
        card.thumbnail.setPixmap(preview)
        image = area.viewport().grab().toImage()
        assert image.pixelColor(60, 100) == QColor(color)
    before = area.viewport().grab().toImage()
    card.update_downloads(98765)
    card.set_installed(True)
    after = area.viewport().grab().toImage()
    assert before != after, "installation status must also invalidate the painted source"
    assert card.dl_label.text() == "98765"
    assert fade.top_active


def test_online_menu_survives_owner_deleted_during_exec(
    qapp,
    sample_record,
    fake_image_loader,
):
    card = GbModCard(sample_record, image_loader=fake_image_loader, defer_image=True)

    def delete_owner(menu):
        shiboken6.delete(card)
        assert not shiboken6.isValid(menu)
        return None

    try:
        with interact(QMenu, delete_owner):
            card._on_context_menu(QPoint(3, 3))
    finally:
        if shiboken6.isValid(card):
            shiboken6.delete(card)
