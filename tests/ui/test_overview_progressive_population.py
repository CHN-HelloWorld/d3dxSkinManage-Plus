from __future__ import annotations

import pytest
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import QGraphicsOpacityEffect

from src.models.mod import AgeRating, Mod, ModInfo
from src.ui import anim
from src.ui.pages.overview import OverviewPage
from src.ui.widgets.mod_card import ModCard


def _write_image(path, color: str) -> None:
    image = QImage(32, 24, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    assert image.save(str(path))


def _deferred_card(page: OverviewPage, mod: Mod) -> ModCard:
    card = ModCard(parent=page._scroll_content, adult_mode=page._adult_mode)
    card.set_mod_deferred(mod)
    page._vs_card_cache[mod.mod_id] = card
    return card


def test_directory_refresh_shows_ready_cards_without_staggered_reveal(
    qtbot,
    temp_data_dir,
    monkeypatch,
):
    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(3840, 2160)
    page.show()
    qtbot.wait(20)

    monkeypatch.setattr(anim, "_enabled", True)
    # 低开销绑定可在一片预算内完成，不应等待入场定时器。
    monkeypatch.setattr("src.ui.pages.overview.perf_counter", lambda: 0.0)
    mods = [Mod(mod_id=f"mod-{index:03d}", name=f"Mod {index:03d}") for index in range(24)]

    page.set_current_mods(mods, mods)

    assert len(page._vs_card_cache) == len(mods)
    assert not page._vs_population_timer.isActive()
    assert not page._card_reveal.is_active()
    assert all(
        not card.isHidden() and not isinstance(card.graphicsEffect(), QGraphicsOpacityEffect)
        for card in page._vs_card_cache.values()
    )


def test_deferred_preview_queue_is_submitted_in_small_batches(qtbot, monkeypatch):
    page = OverviewPage()
    qtbot.addWidget(page)

    submitted: list[tuple] = []
    clears: list[bool] = []
    original_clear_queue = page._pixmap_worker.clear_queue

    def record_clear_queue() -> None:
        clears.append(True)
        original_clear_queue()

    monkeypatch.setattr(page._pixmap_worker, "clear_queue", record_clear_queue)
    monkeypatch.setattr(
        page._pixmap_worker,
        "enqueue",
        lambda *args, **kwargs: submitted.append(args),
    )

    cards = []
    for index in range(page._VS_PIXMAP_BATCH_SIZE + 2):
        mod = Mod(
            mod_id=f"preview-mod-{index}",
            name=f"Preview Mod {index}",
            preview_images=[f"preview-{index}.png"],
        )
        card = ModCard(parent=page._scroll_content)
        card._mod = mod
        card._cached_pixmap = None
        page._vs_card_cache[mod.mod_id] = card
        page._vs_pending_pixmap_cards.append((mod.mod_id, card))
        cards.append(card)

    page._vs_load_deferred_pixmaps()
    page._vs_pixmap_timer.stop()

    assert len(submitted) == page._VS_PIXMAP_BATCH_SIZE
    assert len(page._vs_pending_pixmap_cards) == 2
    assert [args[0] for args in submitted] == [card._mod.mod_id for card in cards[: page._VS_PIXMAP_BATCH_SIZE]]

    page._vs_load_deferred_pixmaps()
    page._vs_pixmap_timer.stop()

    assert not page._vs_pending_pixmap_cards
    assert [args[0] for args in submitted] == [card._mod.mod_id for card in cards]
    assert not clears, "normal preview batches must not clear queued worker tasks"


def test_slow_preview_keeps_previous_batch_waiting_on_next_tick(qtbot, monkeypatch):
    page = OverviewPage()
    qtbot.addWidget(page)

    queued: list[tuple] = []
    clears: list[bool] = []
    original_clear_queue = page._pixmap_worker.clear_queue

    def record_clear_queue() -> None:
        clears.append(True)
        original_clear_queue()

    monkeypatch.setattr(page._pixmap_worker, "clear_queue", record_clear_queue)
    monkeypatch.setattr(page._pixmap_worker, "enqueue", lambda *args, **kwargs: queued.append(args))

    total = page._VS_PIXMAP_BATCH_SIZE * 2
    cards = []
    for index in range(total):
        mod = Mod(
            mod_id=f"slow-preview-{index}",
            name=f"Slow Preview {index}",
            preview_images=[f"slow-preview-{index}.png"],
        )
        card = _deferred_card(page, mod)
        page._vs_pending_pixmap_cards.append((mod.mod_id, card))
        cards.append(card)

    page._vs_load_deferred_pixmaps()
    page._vs_pixmap_timer.stop()
    in_progress = queued.pop(0)
    still_waiting = list(queued)

    # 模拟下一次八十毫秒定时器触发时首次解码仍未完成。
    page._vs_load_deferred_pixmaps()
    page._vs_pixmap_timer.stop()

    assert in_progress[0] == cards[0]._mod.mod_id
    assert [task[0] for task in queued] == [card._mod.mod_id for card in cards[1:]]
    assert queued[: len(still_waiting)] == still_waiting
    assert not clears, "the next timer tick must preserve the previous batch"


def test_selecting_loading_card_reuses_panel_first_pixmap(qtbot, tmp_path):
    preview = tmp_path / "first.png"
    _write_image(preview, "#d94141")

    page = OverviewPage()
    qtbot.addWidget(page)
    mod = Mod(mod_id="panel-first", name="Panel First", preview_images=[str(preview)])
    card = _deferred_card(page, mod)
    page._selected_mod = mod

    assert card._cached_pixmap is None
    page._show_mod_panel(mod)

    # 面板加载器在后台完成解码，再共享首张图像。
    qtbot.waitUntil(lambda: card._cached_pixmap is not None, timeout=2000)
    assert not card._cached_pixmap.isNull()
    assert not card._preview_label.pixmap().isNull()
    assert card._preview_label._shimmer.isHidden()
    assert card._cached_pixmap.cacheKey() == page._panel_preview._cached_pixmap.cacheKey()


def test_switching_panel_preview_reuses_cache_without_second_read(
    qtbot,
    tmp_path,
    monkeypatch,
):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    _write_image(first, "#d94141")
    _write_image(second, "#315ecf")

    page = OverviewPage()
    qtbot.addWidget(page)
    mod = Mod(
        mod_id="panel-switch",
        name="Panel Switch",
        preview_images=[str(first), str(second)],
    )
    card = _deferred_card(page, mod)
    page._selected_mod = mod
    page._show_mod_panel(mod)

    def forbidden_card_read(_path):
        raise AssertionError("card must reuse the pixmap already decoded by PreviewWidget")

    monkeypatch.setattr("src.ui.widgets.mod_card.load_pixmap_file", forbidden_card_read)
    page._panel_preview._show_next()

    assert card.effective_preview_path() == str(second)
    assert card._cached_pixmap.cacheKey() == page._panel_preview._cached_pixmap.cacheKey()
    center = card._cached_pixmap.toImage().pixelColor(
        card._cached_pixmap.width() // 2,
        card._cached_pixmap.height() // 2,
    )
    assert center.blue() > center.red()


def test_panel_cache_rejects_wrong_path_selection_and_recycled_card(qtbot, tmp_path):
    preview = tmp_path / "expected.png"
    other_preview = tmp_path / "other.png"
    _write_image(preview, "#d94141")
    _write_image(other_preview, "#315ecf")

    page = OverviewPage()
    qtbot.addWidget(page)
    mod = Mod(mod_id="guarded-mod", name="Guarded", preview_images=[str(preview)])
    card = _deferred_card(page, mod)
    page._panel_mode = "mod"
    page._selected_mod = mod
    cached = QPixmap(str(preview))
    page._panel_preview._cached_pixmap = cached
    page._panel_preview._cached_path = str(preview)

    assert not page._reuse_panel_preview_for_card(mod.mod_id, str(other_preview), card)
    assert card._cached_pixmap is None

    page._selected_mod = Mod(mod_id="another-mod", name="Another")
    assert not page._reuse_panel_preview_for_card(mod.mod_id, str(preview), card)
    assert card._cached_pixmap is None

    page._selected_mod = mod
    replacement = ModCard(parent=page._scroll_content)
    replacement.set_mod_deferred(mod)
    page._vs_card_cache[mod.mod_id] = replacement
    assert not page._reuse_panel_preview_for_card(mod.mod_id, str(preview), card)
    assert card._cached_pixmap is None
    assert replacement._cached_pixmap is None


def test_panel_cache_cannot_bypass_hidden_adult_preview(qtbot, tmp_path):
    preview = tmp_path / "adult.png"
    _write_image(preview, "#d94141")

    page = OverviewPage()
    qtbot.addWidget(page)
    page._adult_mode = "hide"
    mod = Mod(
        mod_id="hidden-adult",
        name="Hidden Adult",
        info=ModInfo(age_rating=AgeRating.X),
        preview_images=[str(preview)],
    )
    card = _deferred_card(page, mod)
    page._panel_mode = "mod"
    page._selected_mod = mod
    cached = QPixmap(str(preview))
    page._panel_preview._cached_pixmap = cached
    page._panel_preview._cached_path = str(preview)

    assert not page._reuse_panel_preview_for_card(mod.mod_id, str(preview), card)
    assert not card.set_preview_pixmap(cached)
    assert card._cached_pixmap is None
    assert card._preview_label.text() == "已隐藏"
    assert card._preview_label.pixmap().isNull()


def test_scrolling_during_initial_population_prioritizes_new_viewport(
    qtbot,
    temp_data_dir,
    monkeypatch,
):
    from src.ui.pages import overview as overview_module

    page = OverviewPage()
    qtbot.addWidget(page)
    page.resize(800, 600)
    page.show()
    qtbot.wait(20)

    clock = [0.0]
    create = page._vs_create_card

    def slow_card(item, **kwargs):
        """用确定的创建耗时保证首次填充仍有待处理项。"""
        card = create(item, **kwargs)
        clock[0] += 0.009
        return card

    monkeypatch.setattr(overview_module, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(page, "_vs_create_card", slow_card)
    mods = [Mod(mod_id=f"scroll-mod-{index:03d}", name=f"Scroll Mod {index:03d}") for index in range(100)]
    page.set_current_mods(mods, mods)

    scrollbar = page._scroll_area.verticalScrollBar()
    assert page._vs_population_timer.isActive()
    assert scrollbar.maximum() > 0

    scrollbar.setValue(min(scrollbar.maximum(), page._vs_row_h * 5))

    assert not page._vs_population_timer.isActive()
    assert not page._fresh_population

    first_visible = (scrollbar.value() // page._vs_row_h) * page._vs_cols
    target_id = page._display_items[first_visible]["id"]
    qtbot.waitUntil(lambda: target_id in page._vs_card_cache, timeout=1000)


@pytest.mark.parametrize("retarget", [False, True])
def test_scrolling_yields_between_expensive_cards_and_fills_current_viewport(
    qtbot,
    temp_data_dir,
    monkeypatch,
    request,
    retarget,
):
    from src.ui.pages import overview as overview_module

    page = OverviewPage()
    qtbot.addWidget(page)
    request.addfinalizer(page.close)
    page.resize(1200, 800)
    page.show()
    qtbot.wait(20)
    mods = [Mod(mod_id=f"budget-{i:03d}", name=f"Mod {i:03d}") for i in range(200)]
    page.set_current_mods(mods, mods)
    page._vs_population_timer.stop()
    page._fresh_population = False
    clock = [0.0]
    created = []
    create = page._vs_create_card

    def slow_card(item, **kwargs):
        card = create(item, **kwargs)
        created.append(item["id"])
        clock[0] += 0.009  # 单次慢渲染耗尽八毫秒执行预算。
        return card

    monkeypatch.setattr(overview_module, "perf_counter", lambda: clock[0], raising=False)
    monkeypatch.setattr(page, "_vs_create_card", slow_card)
    scrollbar = page._scroll_area.verticalScrollBar()
    scrollbar.setValue(page._vs_row_h * 20)
    page._vs_scroll_timer.stop()
    page._vs_update_visible()

    visible_index = scrollbar.value() // page._vs_row_h * page._vs_cols
    assert created == [page._display_items[visible_index]["id"]]
    assert page._vs_population_timer.isActive()

    if retarget:
        scrollbar.setValue(page._vs_row_h * 40)
    _, _, first, last = page._vs_visible_rows()
    expected = {item["id"] for item in page._display_items[first : last + 1]}
    qtbot.waitUntil(lambda: set(page._vs_card_cache) == expected, timeout=2000)
    qtbot.waitUntil(lambda: not page._vs_population_timer.isActive(), timeout=1000)
    assert not page._fresh_population
    assert all(card.mod_id == mid for mid, card in page._vs_card_cache.items())
