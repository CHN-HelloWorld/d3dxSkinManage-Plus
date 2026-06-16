from threading import get_ident

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtTest import QSignalSpy

from src.ui.pages import overview
from src.ui.widgets import local_preview_cache
from src.utils.image_memory_cache import ImageMemoryCache


@pytest.fixture
def isolated_preview_cache(monkeypatch):
    for name in ("_images", "_prefetched_images", "_rendered_images"):
        monkeypatch.setattr(local_preview_cache, name, ImageMemoryCache(max_bytes=8 * 1024**2))


def test_raw_cache_hit_queues_composition_instead_of_blocking_recycled_card(
    qtbot,
    temp_data_dir,
    tmp_path,
    monkeypatch,
    request,
    isolated_preview_cache,
):
    from src.models.mod import Mod
    from src.ui.widgets import mod_card, preview_compositor
    from src.ui.widgets.image_utils import load_scaled_qimage_file

    path = str(tmp_path / "portrait.png")
    source = QImage(40, 80, QImage.Format.Format_RGB32)
    source.fill(QColor("red"))
    assert source.save(path)
    local_preview_cache.load_preview(path, overview._PixmapLoadWorker._MAX_CACHE_SIZE, load_scaled_qimage_file)
    page = overview.OverviewPage()
    qtbot.addWidget(page)
    request.addfinalizer(page.close)
    mod = Mod(mod_id="portrait", name="Portrait", preview_images=[path])

    def forbid(*args, **kwargs):
        raise AssertionError("a raw cache hit must not compose on the GUI thread")

    # 总览使用已准备像素，界面同步读取路径位于 ModCard。
    monkeypatch.setattr(mod_card, "fill_blur_rounded", forbid)
    owner_thread = get_ident()
    original = preview_compositor.compose_preview

    def compose(*args, **kwargs):
        if get_ident() == owner_thread:
            forbid()
        return original(*args, **kwargs)

    monkeypatch.setattr(preview_compositor, "compose_preview", compose)
    card = page._vs_create_card({"id": mod.mod_id, "type": "mod", "data": mod}, defer_pixmap=True)
    assert not card._preview_resolved
    assert card._cached_pixmap is None


def test_worker_prepares_qimage_and_reuses_it_without_gui_composition(
    qtbot,
    tmp_path,
    monkeypatch,
    isolated_preview_cache,
):
    from src.ui.widgets import preview_compositor

    path = str(tmp_path / "portrait.png")
    source = QImage(40, 80, QImage.Format.Format_RGB32)
    source.fill(QColor("red"))
    assert source.save(path)
    owner_thread = get_ident()
    composing_threads = []
    original = preview_compositor.compose_preview

    def compose(*args, **kwargs):
        composing_threads.append(get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(preview_compositor, "compose_preview", compose)
    worker = overview._PixmapLoadWorker()
    spy = QSignalSpy(worker.image_loaded)
    worker.start()
    try:
        worker.enqueue("portrait", path, 152, 128, 1, mode="contain", dpr=2.0)
        qtbot.waitUntil(lambda: spy.count() == 1, timeout=3000)
        result = spy.at(0)[3]
        assert result.rendered.size() == QSize(304, 256)
        assert result.rendered.devicePixelRatio() == 2.0
        assert result.is_current(path)
        assert composing_threads and owner_thread not in composing_threads
        cached = local_preview_cache.cached_rendered_preview(result.image, result.layout)
        assert cached.cacheKey() == result.rendered.cacheKey()
        worker.enqueue("portrait", path, 152, 128, 2, mode="contain", dpr=2.0)
        qtbot.waitUntil(lambda: spy.count() == 2, timeout=3000)
        assert len(composing_threads) == 1
    finally:
        worker.stop()
        assert worker.wait(3000)


@pytest.mark.parametrize("mode", ["contain", "crop"])
@pytest.mark.parametrize("dpr", [1.0, 2.0])
def test_prepared_preview_preserves_foreground_rounding_and_blurred_letterbox(mode, dpr):
    from src.ui.widgets.preview_compositor import compose_preview

    source = QImage(40, 80, QImage.Format.Format_RGB32)
    source.fill(QColor("red"))
    output = compose_preview(source, 152, 128, mode, dpr=dpr)
    assert output.size() == QSize(round(152 * dpr), round(128 * dpr))
    assert output.devicePixelRatio() == dpr
    assert output.pixelColor(0, 0).alpha() == 0
    assert output.pixelColor(round(76 * dpr), round(64 * dpr)) == QColor("red")
    edge = output.pixelColor(round(10 * dpr), round(64 * dpr))
    assert edge.alpha() == 255
    assert edge.red() == (183 if mode == "contain" else 255)
    assert edge.green() == edge.blue() == 0


@pytest.mark.parametrize("mode", ["contain", "crop"])
def test_prepared_preview_keeps_existing_foreground_geometry(qapp, mode):
    from src.ui.widgets.image_utils import fill_blur_rounded, fill_crop_rounded
    from src.ui.widgets.preview_compositor import compose_preview

    source = QImage(40, 80, QImage.Format.Format_RGB32)
    source.fill(QColor("red"))
    painter = QPainter(source)
    painter.fillRect(0, 0, 40, 40, QColor("green"))
    painter.end()
    reference = (fill_blur_rounded if mode == "contain" else fill_crop_rounded)(
        QPixmap.fromImage(source), 152, 128, radius=8
    ).toImage()
    prepared = compose_preview(source, 152, 128, mode)
    for x in range(46, 106, 3):
        for y in range(8, 120, 3):
            actual, expected = prepared.pixelColor(x, y).getRgb(), reference.pixelColor(x, y).getRgb()
            # 直接采样源矩形与先放大后裁切的插值结果最多相差一个颜色通道单位。
            assert max(abs(a - b) for a, b in zip(actual, expected)) <= 1, (x, y, actual, expected)


@pytest.mark.parametrize("field,value", [("width", 151), ("mode", "crop"), ("dpr", 2.0)])
def test_stale_size_mode_or_dpr_result_is_requeued_before_display(
    qtbot,
    temp_data_dir,
    tmp_path,
    request,
    monkeypatch,
    field,
    value,
):
    from dataclasses import replace

    from src.models.mod import Mod

    path = str(tmp_path / "portrait.png")
    source = QImage(40, 80, QImage.Format.Format_RGB32)
    source.fill(QColor("red"))
    assert source.save(path)
    page = overview.OverviewPage()
    qtbot.addWidget(page)
    request.addfinalizer(page.close)
    mod = Mod(mod_id="portrait", name="Portrait", preview_images=[path])
    card = page._vs_create_card({"id": mod.mod_id, "type": "mod", "data": mod}, defer_pixmap=True)
    page._vs_card_cache[mod.mod_id] = card
    page._vs_image_requests[mod.mod_id] = (1, card, path)
    stale_layout = replace(page._vs_preview_layout(card), **{field: value})
    result = local_preview_cache.DecodedPreview(
        source,
        local_preview_cache._file_stamp(path),
        local_preview_cache.preview_cache_token(),
        rendered=source,
        layout=stale_layout,
    )

    def forbid(*args, **kwargs):
        raise AssertionError("a stale layout must not be displayed")

    monkeypatch.setattr(card, "set_preview_pixmap", forbid)
    page._on_bg_pixmap_loaded(mod.mod_id, path, 1, result)

    assert page._vs_pending_pixmap_cards == [(mod.mod_id, card)]
    assert card._preview_requested and not card._preview_resolved
