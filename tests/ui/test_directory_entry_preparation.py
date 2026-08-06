from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QWidget

from src.models.mod import Mod
from src.ui import anim
from src.ui.pages.overview import OverviewPage
from src.ui.widgets.mod_card import ModCard


def test_prepared_viewport_enters_first_mod_directory_without_new_widgets(qtbot, temp_data_dir, monkeypatch):
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(1600, 1000)
    page.show()
    qtbot.wait(20)
    page.prepare_visible_card_pool(QSize(1600, 1000))
    prepared = set(page._scroll_content.findChildren(ModCard))
    assert prepared and all(card.isHidden() for card in prepared)

    def unexpected(*args, **kwargs):
        raise AssertionError("first viewport must reuse prepared cards")

    monkeypatch.setattr(ModCard, "__init__", unexpected)
    mods = [Mod(mod_id=f"fresh-{index}", name=f"Fresh {index}") for index in range(500)]
    page.set_current_mods(mods, mods)
    assert page._vs_card_cache
    assert set(page._vs_card_cache.values()) <= prepared
    assert all(card.isEnabled() and not card.isHidden() for card in page._vs_card_cache.values())
    page.prepare_visible_card_pool(QSize(1600, 1000))
    assert set(page._scroll_content.findChildren(ModCard)) == prepared
    page.close()


def test_card_preparation_waits_for_initial_background_material(qtbot, qapp, temp_data_dir):
    from src.ui.background_style import install_background_style
    from src.ui.main_window import MainWindow

    host = QWidget()
    qtbot.addWidget(host)
    host.resize(1000, 700)
    host._overview_page = OverviewPage()
    qtbot.addWidget(host._overview_page)
    host._nav_container = QWidget(host)
    host._nav_container.resize(50, 600)
    host._background_controller = SimpleNamespace(state="loading", active=False)
    previous = getattr(qapp, "_background_style", None)
    surfaces = install_background_style(qapp)
    previous_enabled, previous_theme = surfaces.enabled, surfaces._theme_name
    surfaces.apply(False)
    try:
        MainWindow._prepare_directory_cards(host)
        assert not host._overview_page._scroll_content.findChildren(ModCard)
        host._background_controller.state = "playing"
        host._background_controller.active = True
        MainWindow._prepare_directory_cards(host)
        assert not host._overview_page._scroll_content.findChildren(ModCard)
        surfaces.apply(True, theme_name="light")
        MainWindow._prepare_directory_cards(host)
        cards = host._overview_page._scroll_content.findChildren(ModCard)
        assert cards
        assert all(card.isHidden() and card.property("_backgroundSurface") is True for card in cards)
        MainWindow._prepare_directory_cards(host)
        assert host._overview_page._scroll_content.findChildren(ModCard) == cards
    finally:
        if previous is None:
            surfaces.dispose()
        else:
            surfaces.apply(previous_enabled, theme_name=previous_theme)
        host._overview_page.close()


def test_every_directory_entry_replays_cached_intro_without_delaying_pointer_input(qtbot, temp_data_dir, monkeypatch):
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.show()
    qtbot.wait(20)
    mods = [Mod(mod_id=f"replay-{index:03d}", name=f"Replay {index:03d}") for index in range(120)]
    page.set_mod_loader(lambda: page.set_current_mods(mods, mods))
    page.category_navigated.connect(
        lambda parent: page.set_current_mods(mods if parent else [], mods if parent else [])
    )
    previous = anim.is_enabled()
    anim.set_enabled(True)
    clock = [1.0]
    monkeypatch.setattr("src.ui.pages.overview.perf_counter", lambda: clock[0])
    monkeypatch.setattr("src.ui.widgets.card_reveal.perf_counter", lambda: clock[0])
    try:
        for entry in range(2):
            page._navigate_to("directory")
            while page._vs_population_pending:
                page._vs_update_visible()
            assert page._card_reveal.is_active()
            clock[0] += 0.06
            page._card_reveal._advance()
            viewport = page._scroll_area.viewport().rect().translated(0, page._scroll_area.verticalScrollBar().value())
            cards = sorted(
                (card for card in page._vs_card_cache.values() if card.geometry().intersects(viewport)),
                key=lambda card: (card.y(), card.x()),
            )
            assert cards[0].graphicsEffect().reveal_opacity > cards[-1].graphicsEffect().reveal_opacity
            assert all(card.graphicsEffect().reveal_opacity == 0 for card in cards[1:])
            assert all(
                card.graphicsEffect().reveal_opacity == 1
                for card in page._vs_card_cache.values()
                if not card.geometry().intersects(viewport)
            )
            if entry == 0:
                qtbot.mouseClick(cards[0], Qt.MouseButton.LeftButton)
                assert page._selected_ids == {cards[0].mod_id}
            for _ in range(len(cards) + 6):
                clock[0] += 0.04
                page._card_reveal._advance()
                page._scroll_area.viewport().grab()
            assert not page._card_reveal.is_active()
            assert all(card.graphicsEffect().reveal_opacity == 1 for card in cards)
            page._scroll_area.verticalScrollBar().setValue(page._vs_row_h - 3)
            page._navigate_to("")
        page.set_current_mods(mods, mods)
        assert not page._card_reveal.is_active(), "ordinary refresh must not replay directory entry"
    finally:
        anim.set_enabled(previous)
        page.close()


@pytest.mark.parametrize("action", ["scroll", "hide", "refresh", "list", "load_failed", "click"])
def test_interrupted_entry_restores_opacity_without_replaying(qtbot, temp_data_dir, monkeypatch, action):
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.show()
    qtbot.wait(20)
    mods = [Mod(mod_id=f"interrupt-{index:03d}", name=f"Mod {index:03d}") for index in range(120)]
    page.category_navigated.connect(lambda _: page.set_current_mods(mods, mods))
    monkeypatch.setattr("src.ui.pages.overview.perf_counter", lambda: 1.0)
    previous = anim.is_enabled()
    anim.set_enabled(True)
    try:
        page._navigate_to("directory")
        while page._vs_population_pending:
            page._vs_update_visible()
        effects = [card.graphicsEffect() for card in page._vs_card_cache.values()]
        assert any(effect.reveal_opacity < 1 for effect in effects)
        if action == "scroll":
            page._scroll_area.verticalScrollBar().setValue(100)
        elif action == "hide":
            page.hide()
        elif action == "refresh":
            page.set_current_mods(mods, mods)
        elif action == "list":
            page._set_view("list")
        elif action == "load_failed":
            page.begin_loading()
            page._directory_intro_pending = True
            page.loading_failed()
        else:
            card = next(iter(page._vs_card_cache.values()))
            qtbot.mouseClick(card, Qt.MouseButton.LeftButton)
            assert page._selected_ids == {card.mod_id}
        assert not page._card_reveal.is_active()
        assert not page._directory_intro_pending
        assert all(effect.reveal_opacity == 1 for effect in effects)
        page._vs_update_visible()
        assert not page._card_reveal.is_active()
    finally:
        anim.set_enabled(previous)
        page.close()


def test_cold_directory_still_reveals_after_host_repeats_loading_state(qtbot, temp_data_dir, monkeypatch):
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.show()
    qtbot.wait(20)
    # 未缓存刷新在页面导航后调用加载通知。
    page.category_navigated.connect(lambda _: page.begin_loading())
    monkeypatch.setattr("src.ui.pages.overview.perf_counter", lambda: 1.0)
    previous = anim.is_enabled()
    anim.set_enabled(True)
    try:
        page._navigate_to("cold-directory")
        mods = [Mod(mod_id=f"cold-intro-{i}", name=f"Mod {i}") for i in range(10)]
        page.set_current_mods(mods, mods)
        assert page._card_reveal.is_active()
        assert any(card.graphicsEffect().reveal_opacity < 1 for card in page._vs_card_cache.values())
    finally:
        anim.set_enabled(previous)
        page.close()


def test_failed_navigation_from_empty_view_cannot_reveal_later_refresh(qtbot, temp_data_dir, monkeypatch):
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(1000, 700)
    page.show()
    qtbot.wait(20)
    monkeypatch.setattr("src.ui.pages.overview.perf_counter", lambda: 1.0)
    previous = anim.is_enabled()
    anim.set_enabled(True)
    try:
        page._navigate_to("unavailable")
        page.loading_failed()
        mods = [Mod(mod_id=f"recovered-{i}", name=f"Mod {i}") for i in range(10)]
        page.set_current_mods(mods, mods)
        assert not page._card_reveal.is_active()
        assert not page._directory_intro_pending
        assert all(card.graphicsEffect().reveal_opacity == 1 for card in page._vs_card_cache.values())
    finally:
        anim.set_enabled(previous)
        page.close()


def test_cold_previews_do_not_force_prepared_visible_cards_back_to_constructor_slices(
    qtbot, temp_data_dir, monkeypatch
):
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(1600, 1000)
    page.show()
    qtbot.wait(20)
    page.prepare_visible_card_pool(QSize(1600, 1000))
    clock = [0.0]
    monkeypatch.setattr("src.ui.pages.overview.perf_counter", lambda: clock[0])
    monkeypatch.setattr("src.ui.widgets.card_reveal.perf_counter", lambda: clock[0])
    create = page._vs_create_card

    def bind(item, **kwargs):
        card = create(item, **kwargs)
        clock[0] += 0.001
        return card

    monkeypatch.setattr(page, "_vs_create_card", bind)
    mods = [
        Mod(mod_id=f"cold-{index:04d}", name=f"Cold {index:04d}", preview_images=[f"cold-{index}.png"])
        for index in range(500)
    ]
    page.set_current_mods(mods, mods)
    cols = page._vs_cols
    visible_count = min(len(mods), ((page._scroll_area.viewport().height() - 1) // page._vs_row_h + 1) * cols)
    expected = {item["id"] for item in page._display_items[:visible_count]}
    assert expected <= page._vs_card_cache.keys()
    assert all(not page._vs_card_cache[item_id]._preview_resolved for item_id in expected)
    assert page._vs_population_pending
    page.close()
