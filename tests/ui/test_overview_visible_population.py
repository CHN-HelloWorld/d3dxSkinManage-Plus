from __future__ import annotations

import pytest
from PySide6.QtGui import QColor, QImage

from src.models.mod import Mod, ModInfo
from src.ui.pages import overview
from src.ui.widgets import local_preview_cache
from src.utils.image_memory_cache import ImageMemoryCache


@pytest.fixture
def population_page(qtbot, temp_data_dir, tmp_path, monkeypatch, request):
    for name in ("_images", "_prefetched_images", "_rendered_images"):
        monkeypatch.setattr(local_preview_cache, name, ImageMemoryCache(max_bytes=16 * 1024**2))
    clock = [0.0]
    monkeypatch.setattr(overview, "perf_counter", lambda: clock[0])
    page = overview.OverviewPage()
    qtbot.addWidget(page)
    request.addfinalizer(page.close)
    page._card_scale = 100
    page.resize(1700, 1400)
    page.show()
    page._main_splitter.widget(0).setFixedWidth(1600)
    qtbot.wait(20)
    # 实际可见区域为九列六行，缓冲窗口范围更大。
    page._scroll_area.viewport().setFixedHeight(1150)
    paths = []
    for color in ("red", "blue"):
        path = str(tmp_path / f"{color}.png")
        image = QImage(32, 24, QImage.Format.Format_RGB32)
        image.fill(QColor(color))
        assert image.save(path)
        paths.append(path)
    return page, clock, paths


def _prepare_images(paths, kind, width=160, height=200):
    layout = local_preview_cache.PreviewLayout(
        width - 8, int(height * 0.64), "crop" if kind == "category" else "contain"
    )
    for path in paths:
        decoded = local_preview_cache.load_preview(
            path,
            overview._PixmapLoadWorker.cache_size(layout),
            overview.load_scaled_qimage_file,
        )
        local_preview_cache.prepare_preview(decoded, layout)


def _replace_contents(page, kind, prefix, paths=(), *, count=180, tags=()):
    if kind == "category":
        page.set_characters(
            [
                {
                    "id": f"{prefix}-{i:03d}",
                    "name": f"{prefix} {i:03d}",
                    "thumbnail": paths[i % len(paths)] if paths else "",
                    "mod_count": i,
                    "enabled_count": i % 3,
                }
                for i in range(count)
            ]
        )
    else:
        mods = [
            Mod(
                mod_id=f"{prefix}-{i:03d}",
                name=f"{prefix} {i:03d}",
                preview_images=[paths[i % len(paths)]] if paths else [],
                info=ModInfo(tags=list(tags)),
            )
            for i in range(count)
        ]
        page.set_current_mods(mods, mods)


def _finish_population(page):
    for _ in range(100):
        if not page._vs_population_pending:
            return
        page._vs_update_visible()
    pytest.fail("viewport population did not finish")


def _charge_bindings(page, clock, monkeypatch, cost):
    create = page._vs_create_card

    def bind(item, **kwargs):
        card = create(item, **kwargs)
        clock[0] += cost
        return card

    monkeypatch.setattr(page, "_vs_create_card", bind)


@pytest.mark.parametrize("kind", ["mod", "category"])
def test_cached_first_viewport_is_bound_before_returning_to_event_loop(
    population_page,
    monkeypatch,
    kind,
):
    page, clock, paths = population_page
    _prepare_images(paths, kind)
    _replace_contents(page, kind, "old", paths)
    _finish_population(page)
    old_widgets = set(page._vs_card_cache.values())
    assert page._vs_cols == 9
    _charge_bindings(page, clock, monkeypatch, 0.001)

    _replace_contents(page, kind, "new", paths)

    expected = {f"new-{i:03d}" for i in range(54)}
    assert set(page._vs_card_cache) == expected, "only the complete visible viewport bypasses the 8 ms slice"
    assert set(page._vs_card_cache.values()) <= old_widgets
    assert page._vs_population_pending and page._vs_population_timer.isActive(), "buffer rows still yield"
    for item_id, card in page._vs_card_cache.items():
        index = int(item_id[-3:])
        assert not card.isHidden() and card.isEnabled()
        assert card._name_label.text() == f"new {index:03d}"
        assert (card._data["id"] if kind == "category" else card.mod_id) == item_id
        label = card._thumb if kind == "category" else card._preview_label
        assert card._preview_resolved and not card._preview_requested
        assert label._shimmer.isHidden()
        image = label.pixmap().toImage()
        assert image.pixelColor(image.width() // 2, image.height() // 2) == QColor("red" if index % 2 == 0 else "blue")
    assert not page._vs_pending_pixmap_cards


def _content_geometry(card):
    widgets = [card._preview_container, card._name_label]
    if isinstance(card, overview.CharacterCard):
        widgets += [card._thumb, card._info_label]
    else:
        widgets += [
            card._preview_label,
            card._info_widget,
            card._rating_badge,
            card._status_indicator,
            card._tag_row,
            *card._tag_row._chips,
        ]
    return tuple((widget.geometry().getRect()) for widget in widgets)


@pytest.mark.parametrize("kind", ["mod", "category"])
def test_changed_visible_card_layout_is_final_before_show(population_page, monkeypatch, kind):
    page, _, paths = population_page
    _replace_contents(page, kind, "old")
    _finish_population(page)
    observations = {}
    for card in page._vs_card_cache.values():
        show = card.show

        def record_show(card=card, show=show):
            before = _content_geometry(card)
            show()
            observations[card] = before, _content_geometry(card)

        monkeypatch.setattr(card, "show", record_show)
    page._card_scale = 125
    _prepare_images(paths, kind, width=200, height=250)

    _replace_contents(page, kind, "new", paths, tags=("New tag", "Second tag"))

    visible = [card for card in page._vs_card_cache.values() if card.y() < page._scroll_area.viewport().height()]
    assert visible and all(card in observations for card in visible)
    page.grab()  # 子控件几何必须在截图之前就已正确。
    for card in visible:
        before, shown = observations[card]
        assert before == shown == _content_geometry(card)
        assert card._name_label.width() > 0


def test_slow_first_time_construction_keeps_the_normal_slice(population_page, monkeypatch):
    page, clock, _ = population_page
    _charge_bindings(page, clock, monkeypatch, 0.009)

    _replace_contents(page, "mod", "new")

    assert set(page._vs_card_cache) == {"new-000"}
    assert page._vs_population_pending and page._vs_population_timer.isActive()


@pytest.mark.parametrize("pooled_count", [0, 2])
def test_pool_miss_preserves_the_card_creation_observer_contract(
    population_page,
    monkeypatch,
    pooled_count,
):
    page, clock, _ = population_page
    if pooled_count:
        _replace_contents(page, "mod", "old", count=pooled_count)
    create = page._vs_create_card

    def observed(item, **kwargs):
        card = create(item, **kwargs)
        # 绘制观察器附加到每个新创建卡片。
        assert isinstance(card, overview.QFrame)
        assert card.parentWidget() is page._scroll_content
        return card

    monkeypatch.setattr(page, "_vs_create_card", observed)
    _charge_bindings(page, clock, monkeypatch, 0.006)

    _replace_contents(page, "mod", "new")

    assert set(page._vs_card_cache) == {"new-000", "new-001"}
    assert all(card.mod_id == item_id for item_id, card in page._vs_card_cache.items())
    assert page._vs_population_pending


def test_uncached_reused_preview_keeps_the_normal_slice(population_page, monkeypatch):
    page, clock, paths = population_page
    _replace_contents(page, "mod", "old")
    _finish_population(page)
    _charge_bindings(page, clock, monkeypatch, 0.009)

    _replace_contents(page, "mod", "new", paths)

    assert set(page._vs_card_cache) == {"new-000"}
    assert not page._vs_card_cache["new-000"]._preview_resolved
    assert page._vs_population_pending


def test_slow_cached_batch_yields_and_navigation_cancels_remaining_bindings(population_page, monkeypatch):
    page, clock, paths = population_page
    _prepare_images(paths, "mod")
    _replace_contents(page, "mod", "old", paths)
    _finish_population(page)
    _charge_bindings(page, clock, monkeypatch, 0.020)

    _replace_contents(page, "mod", "new", paths)

    assert 1 < len(page._vs_card_cache) <= 4, "the cached batch has a deadline, even for 54 reused cards"
    assert page._vs_population_pending
    page._navigate_to("replacement")
    _replace_contents(page, "mod", "replacement", paths)
    _finish_population(page)
    assert all(
        item_id.startswith("replacement-") and card.mod_id == item_id for item_id, card in page._vs_card_cache.items()
    )


def test_extreme_viewport_does_not_bind_an_unbounded_cached_batch(population_page):
    page, _, paths = population_page
    page._scroll_area.viewport().setFixedHeight(2500)
    _prepare_images(paths, "mod")
    _replace_contents(page, "mod", "old", paths, count=500)
    _finish_population(page)
    assert len(page._vs_card_cache) > 96

    _replace_contents(page, "mod", "new", paths, count=500)

    assert 0 < len(page._vs_card_cache) <= 96
    assert page._vs_population_pending and page._vs_population_timer.isActive()
