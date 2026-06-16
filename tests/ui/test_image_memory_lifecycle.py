import os
import subprocess
import sys
from pathlib import Path
from threading import Event

import pytest
from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QSignalSpy

from src.ui.pages import gallery, overview
from src.ui.widgets import local_preview_cache


@pytest.mark.parametrize("kind", ["overview", "category", "gallery"])
def test_memory_pressure_pauses_speculation_but_loads_visible_local_images(
    qtbot,
    temp_data_dir,
    tmp_path,
    monkeypatch,
    request,
    kind,
):
    from src.utils.image_cache_budget import ImageCacheBudget
    from src.utils.image_memory_cache import ImageMemoryCache
    from tests.ui.test_scroll_preview_retention import _path, _prepare, _ready, _scroll

    budget = ImageCacheBudget()
    for attribute, partition in (
        ("_images", "local_viewed"),
        ("_prefetched_images", "local_prefetch"),
        ("_rendered_images", "local_rendered"),
    ):
        cache = ImageMemoryCache(max_bytes=0)
        budget.register(cache, partition)
        monkeypatch.setattr(local_preview_cache, attribute, cache)
    for now in (0, 5, 10):
        budget.observe_memory(16 * 1024**3, 300 * 1024**2, now=now)
    assert budget.under_pressure
    monkeypatch.setattr(overview, "image_cache_budget", budget)
    monkeypatch.setattr(gallery, "image_cache_budget", budget)
    page, decoded = _prepare(qtbot, tmp_path, monkeypatch, request, kind)
    qtbot.waitUntil(
        lambda: (
            bool(page._vs_card_cache)
            and all(_ready(card) for card in page._vs_card_cache.values())
            and not page._vs_image_requests
        ),
        timeout=5000,
    )
    assert not page._vs_pending_prefetch
    assert not page._vs_pixmap_timer.isActive()
    visible_paths = {_path(card) for card in page._vs_card_cache.values()}
    assert decoded and set(decoded) <= visible_paths
    assert local_preview_cache._prefetched_images.max_bytes == 0
    assert local_preview_cache._images.bytes_used > 0, "foreground history must bypass disabled prefetch storage"
    first_ids = set(page._vs_card_cache)
    previous_reads = {path: decoded[path] for path in visible_paths}
    _scroll(page, 35)
    qtbot.waitUntil(
        lambda: all(_ready(card) for card in page._vs_card_cache.values()) and not page._vs_image_requests,
        timeout=5000,
    )
    _scroll(page, 12)
    assert set(page._vs_card_cache) == first_ids
    assert all(_ready(card) for card in page._vs_card_cache.values())
    assert {path: decoded[path] for path in visible_paths} == previous_reads


@pytest.mark.parametrize("kind", ["overview", "gallery"])
def test_cache_clear_excludes_running_and_queued_previews_but_can_display_them(
    qtbot,
    temp_data_dir,
    tmp_path,
    monkeypatch,
    kind,
):
    from src.ui.widgets.image_utils import fill_crop_rounded
    from src.utils.image_cache_budget import image_cache_budget

    module = overview if kind == "overview" else gallery
    worker_class = overview._PixmapLoadWorker if kind == "overview" else gallery._GalleryPixmapWorker
    started, release = Event(), Event()
    path = tmp_path / "clear-race.png"
    image = QImage(32, 24, QImage.Format.Format_ARGB32)
    image.fill(QColor("red"))
    assert image.save(str(path))
    original = module.load_scaled_qimage_file

    def decode(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    image_cache_budget.clear()
    monkeypatch.setattr(module, "load_scaled_qimage_file", decode)
    worker = worker_class()
    spy = QSignalSpy(worker.image_loaded)
    worker.start()
    try:
        worker.enqueue("one", str(path), 32, 24, 1)
        assert started.wait(3)
        worker.enqueue("two", str(path), 16, 12, 2)
        image_cache_budget.clear()
        release.set()
        qtbot.waitUntil(lambda: spy.count() == 2, timeout=3000)
        assert local_preview_cache._prefetched_images.bytes_used == 0
        for event in [spy.at(index) for index in range(spy.count())]:
            result = event[3]
            assert result.is_current(str(path)), "cache clear must not impersonate a source-file change"
            assert result.image.pixelColor(0, 0) == QColor("red")
            assert not local_preview_cache.retain_viewed_preview(str(path), QSize(32, 24), result)
            rendered = local_preview_cache.rendered_preview(
                result.image,
                16,
                12,
                "crop",
                fill_crop_rounded,
                cache_generation=result.cache_token[2],
            )
            assert not rendered.isNull()
        assert local_preview_cache._rendered_images.bytes_used == 0
        assert local_preview_cache._images.bytes_used == 0
    finally:
        release.set()
        worker.stop()
        assert worker.wait(3000)
        image_cache_budget.clear()


@pytest.mark.parametrize("kind", ["overview", "gallery"])
@pytest.mark.parametrize("action", ["close", "delete"])
def test_slow_image_worker_survives_page_deletion_then_retires(tmp_path, kind, action):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("_image_worker_lifetime_probe.py")), kind, action, str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "page and worker released" in result.stdout
