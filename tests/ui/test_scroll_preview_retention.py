from collections import Counter
from threading import Event

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QImage

from src.models.mod import Mod
from src.ui.pages import gallery, overview
from src.ui.widgets import mod_card


def _prepare(qtbot, tmp_path, monkeypatch, request, kind):
    module = gallery if kind == "gallery" else overview
    decoded = Counter()
    original = module.load_scaled_qimage_file

    def decode(path, *args, **kwargs):
        decoded[str(path)] += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(module, "load_scaled_qimage_file", decode)
    mods = []
    for i in range(160):
        path = tmp_path / f"preview-{i:04}.png"
        picture = QImage(32, 24, QImage.Format.Format_RGB32)
        picture.fill(QColor("red" if i % 2 == 0 else "blue"))
        assert picture.save(str(path))
        mods.append(Mod(mod_id=f"mod-{i:04}", name=f"Mod {i:04}", preview_images=[str(path)]))
    page = gallery.GalleryPage() if kind == "gallery" else overview.OverviewPage()
    qtbot.addWidget(page)
    request.addfinalizer(page.close)
    page.resize(900, 600)
    page.show()
    qtbot.wait(20)
    if kind == "category":
        page.set_characters([{"id": mod.mod_id, "name": mod.name, "thumbnail": mod.primary_preview} for mod in mods])
    elif kind == "overview":
        page.set_current_mods(mods, mods)
    else:
        page.set_mods(mods)
    page._fresh_population = False
    page._vs_population_timer.stop()
    _scroll(page, 12)
    return page, decoded


def _scroll(page, row):
    page._scroll_area.verticalScrollBar().setValue(row * page._vs_row_h)
    page._vs_update_visible()
    # 分批创建控件时即恢复缓存像素，不能等待图像任务回调。
    for _ in range(100):
        if not getattr(page, "_vs_population_pending", False):
            break
        page._vs_update_visible()
    assert not getattr(page, "_vs_population_pending", False)
    page._vs_scroll_timer.stop()


def _label(card):
    label = getattr(card, "_preview_label", None)
    if label is None:
        label = getattr(card, "_thumb", None)
    if label is None:
        label = card._image
    return label


def _ready(card):
    label = _label(card)
    return card._cached_pixmap is not None and not label.pixmap().isNull() and label._shimmer.isHidden()


def _path(card):
    return card._data["thumbnail"] if isinstance(card, overview.CharacterCard) else card._mod.primary_preview


def _center_color(card):
    image = _label(card).pixmap().toImage()
    return image.pixelColor(image.width() // 2, image.height() // 2)


@pytest.mark.parametrize("kind", ["overview", "category", "gallery"])
def test_scrolling_back_restores_loaded_pixels_before_event_loop(
    qtbot,
    temp_data_dir,
    tmp_path,
    monkeypatch,
    request,
    kind,
):
    page, decoded = _prepare(qtbot, tmp_path, monkeypatch, request, kind)
    qtbot.waitUntil(lambda: all(_ready(card) for card in page._vs_card_cache.values()), timeout=5000)
    old_ids = set(page._vs_card_cache)
    paths = {_path(card) for card in page._vs_card_cache.values()}
    previous_reads = {path: decoded[path] for path in paths}

    _scroll(page, 35)
    _scroll(page, 12)

    assert set(page._vs_card_cache) == old_ids
    assert all(_ready(card) for card in page._vs_card_cache.values()), "history must not show shimmer again"
    assert {path: decoded[path] for path in paths} == previous_reads
    for item_id, card in page._vs_card_cache.items():
        assert _center_color(card) == QColor("red" if int(item_id[-4:]) % 2 == 0 else "blue")


@pytest.mark.parametrize("kind", ["overview", "category", "gallery"])
def test_previews_progress_while_scroll_events_keep_arriving(
    qtbot,
    temp_data_dir,
    tmp_path,
    monkeypatch,
    request,
    kind,
):
    page, _ = _prepare(qtbot, tmp_path, monkeypatch, request, kind)
    bar = page._scroll_area.verticalScrollBar()
    for _ in range(25):
        bar.setValue(bar.value() + 1)
        qtbot.wait(12)

    assert any(_ready(card) for card in page._vs_card_cache.values()), "scrolling must not starve previews"


@pytest.mark.parametrize("kind", ["overview", "gallery"])
def test_visible_previews_start_before_offscreen_buffer_rows(
    qtbot,
    temp_data_dir,
    tmp_path,
    monkeypatch,
    request,
    kind,
):
    page, decoded = _prepare(qtbot, tmp_path, monkeypatch, request, kind)
    qtbot.waitUntil(lambda: bool(decoded), timeout=2000)
    expected = str(tmp_path / f"preview-{12 * page._vs_cols:04}.png")
    assert next(iter(decoded)) == expected, "the first image must belong to the screen, not its hidden buffer"


@pytest.mark.parametrize("kind", ["overview", "category", "gallery"])
def test_data_outside_widget_window_is_prefetched(
    qtbot,
    temp_data_dir,
    tmp_path,
    monkeypatch,
    request,
    kind,
):
    page, decoded = _prepare(qtbot, tmp_path, monkeypatch, request, kind)
    active = set(page._vs_card_cache)
    next_index = max(int(item_id[-4:]) for item_id in active) + 1
    next_id = f"mod-{next_index:04}"
    path = str(tmp_path / f"preview-{next_index:04}.png")
    qtbot.waitUntil(lambda: decoded[path] and next_id not in page._vs_image_requests, timeout=2500)
    assert set(page._vs_card_cache) == active, "prefetching images must not construct extra cards"

    _scroll(page, 13)
    assert _ready(page._vs_card_cache[next_id]), "prefetched pixels must be ready on entry"
    assert _center_color(page._vs_card_cache[next_id]) == QColor("red" if next_index % 2 == 0 else "blue")


@pytest.mark.parametrize("kind", ["overview", "category", "gallery"])
def test_overwriting_a_preview_invalidates_retained_pixels(
    qtbot,
    temp_data_dir,
    tmp_path,
    monkeypatch,
    request,
    kind,
):
    page, decoded = _prepare(qtbot, tmp_path, monkeypatch, request, kind)
    qtbot.waitUntil(lambda: all(_ready(card) for card in page._vs_card_cache.values()), timeout=5000)
    item_id, card = next(iter(page._vs_card_cache.items()))
    path = _path(card)
    reads = decoded[path]
    _scroll(page, 35)
    replacement = QImage(48, 36, QImage.Format.Format_RGB32)
    replacement.fill(QColor("#00ff00"))
    assert replacement.save(path)

    _scroll(page, 12)

    card = page._vs_card_cache[item_id]
    assert card._cached_pixmap is None, "changed files must not restore old cached pixels"
    qtbot.waitUntil(lambda: _ready(card), timeout=2500)
    assert _center_color(card) == QColor("#00ff00")
    assert decoded[path] == reads + 1


@pytest.mark.parametrize("kind", ["overview", "category", "gallery"])
@pytest.mark.parametrize("stage", ["decode", "delivery"])
def test_file_replaced_during_inflight_request_cannot_deliver_old_pixels(
    qtbot,
    temp_data_dir,
    tmp_path,
    monkeypatch,
    request,
    kind,
    stage,
):
    page, _ = _prepare(qtbot, tmp_path, monkeypatch, request, kind)
    target_id = f"mod-{12 * page._vs_cols:04}"
    path = _path(page._vs_card_cache[target_id])
    started, release = Event(), Event()
    request.addfinalizer(release.set)
    if stage == "decode":
        module = gallery if kind == "gallery" else overview
        decode = module.load_scaled_qimage_file

        def delayed_decode(source, *args, **kwargs):
            image = decode(source, *args, **kwargs)
            if str(source) == path:
                started.set()
                release.wait(3)
            return image

        monkeypatch.setattr(module, "load_scaled_qimage_file", delayed_decode)
    else:

        def notified(_item_id, source, _request_id, _image):
            if source == path:
                started.set()

        page._pixmap_worker.image_loaded.connect(notified, Qt.ConnectionType.DirectConnection)
    page._vs_load_deferred_pixmaps()
    page._vs_pixmap_timer.stop()
    # 后台任务解码旧像素前暂不分发界面事件。
    assert started.wait(2)
    replacement = QImage(48, 36, QImage.Format.Format_RGB32)
    replacement.fill(QColor("#00ff00"))
    assert replacement.save(path)
    _scroll(page, 35)
    _scroll(page, 12)
    release.set()

    card = page._vs_card_cache[target_id]
    qtbot.waitUntil(lambda: _ready(card), timeout=2500)
    assert _center_color(card) == QColor("#00ff00"), "a queued old decode must not resolve a new binding"


@pytest.mark.parametrize("kind", ["overview", "gallery"])
def test_inflight_preview_survives_a_row_change(
    qtbot,
    temp_data_dir,
    tmp_path,
    monkeypatch,
    request,
    kind,
):
    page, decoded = _prepare(qtbot, tmp_path, monkeypatch, request, kind)
    target_id = f"mod-{12 * page._vs_cols:04}"
    card = page._vs_card_cache[target_id]
    path = _path(card)
    started, release = Event(), Event()
    request.addfinalizer(release.set)
    module = overview if kind == "overview" else gallery
    original = module.load_scaled_qimage_file

    def delayed_decode(source, *args, **kwargs):
        if str(source) == path:
            started.set()
            release.wait(3)
        return original(source, *args, **kwargs)

    monkeypatch.setattr(module, "load_scaled_qimage_file", delayed_decode)
    qtbot.waitUntil(started.is_set, timeout=2000)
    _scroll(page, 13)
    release.set()

    assert page._vs_card_cache[target_id] is card
    qtbot.waitUntil(lambda: _ready(card), timeout=2500)
    assert decoded[path] == 1, "moving to the next row must not cancel and repeat a valid decode"


@pytest.mark.parametrize("kind", ["overview", "category", "gallery"])
def test_return_scroll_reuses_composed_thumbnail_pixels(
    qtbot,
    temp_data_dir,
    tmp_path,
    monkeypatch,
    request,
    kind,
):
    page, _ = _prepare(qtbot, tmp_path, monkeypatch, request, kind)
    qtbot.waitUntil(lambda: all(_ready(card) for card in page._vs_card_cache.values()), timeout=5000)
    _scroll(page, 35)
    compositions = []

    def track_compositor(module, name):
        original = getattr(module, name)

        def compose(*args, **kwargs):
            compositions.append(name)
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, compose)

    track_compositor(mod_card, "fill_blur_rounded")
    track_compositor(overview, "fill_crop_rounded")
    track_compositor(gallery, "fill_blur_rounded")

    _scroll(page, 12)

    assert all(_ready(card) for card in page._vs_card_cache.values())
    assert not compositions, "returning thumbnails must reuse their blur/crop/rounding work"


def test_image_cache_evicts_by_bytes_and_recent_use():
    from src.utils.image_memory_cache import ImageMemoryCache

    cache = ImageMemoryCache(max_bytes=512)
    small = QImage(8, 8, QImage.Format.Format_ARGB32)
    cache.put("a", small)
    cache.put("b", small)
    assert cache.get("a") is not None
    cache.put("c", small)
    assert cache.get("a") is not None and cache.get("c") is not None
    assert cache.get("b") is None
    assert cache.bytes_used == 512
    cache.put("large", QImage(32, 32, QImage.Format.Format_ARGB32))
    assert cache.get("large") is None
    assert cache.get("a") is not None, "an oversized entry must not evict useful small images"
    cache.put("c", ("file-stamp", small), cost_bytes=small.sizeInBytes())
    assert cache.bytes_used == 512, "replacing an entry must not count its old pixels twice"
    cache.remove("a")
    assert cache.bytes_used == 256
    cache.clear()
    assert cache.bytes_used == 0


def test_speculative_previews_do_not_evict_recently_viewed_images(tmp_path, monkeypatch):
    from src.ui.widgets import local_preview_cache
    from src.ui.widgets.image_utils import load_scaled_qimage_file
    from src.utils.image_memory_cache import ImageMemoryCache

    monkeypatch.setattr(local_preview_cache, "_images", ImageMemoryCache(max_bytes=256))
    monkeypatch.setattr(local_preview_cache, "_prefetched_images", ImageMemoryCache(max_bytes=512), raising=False)
    paths = []
    for index in range(4):
        path = str(tmp_path / f"image-{index}.png")
        picture = QImage(8, 8, QImage.Format.Format_RGB32)
        picture.fill(QColor("red" if index == 0 else "blue"))
        assert picture.save(path)
        paths.append(path)
    size = QSize(8, 8)
    local_preview_cache.load_preview(paths[0], size, load_scaled_qimage_file)
    assert local_preview_cache.cached_preview(paths[0], size) is not None  # 已查看的预览。
    for path in paths[1:]:
        local_preview_cache.load_preview(path, size, load_scaled_qimage_file)  # 预取的预览。

    retained = local_preview_cache.cached_preview(paths[0], size)
    assert retained is not None, "prefetch must have a separate budget from viewed previews"
    assert retained.pixelColor(4, 4) == QColor("red")
