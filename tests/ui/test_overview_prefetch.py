from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QColor, QImage

from src.models.mod import Mod
from src.ui.pages import overview
from src.ui.widgets import local_preview_cache
from src.ui.widgets.local_preview_cache import load_preview
from src.utils.image_cache_budget import MIB, ImageCacheBudget, image_cache_budget
from src.utils.image_memory_cache import ImageMemoryCache


@pytest.fixture
def rig(qtbot, temp_data_dir, tmp_path, monkeypatch, request):
    page = overview.OverviewPage()
    qtbot.addWidget(page)
    request.addfinalizer(page.close)
    page.resize(1100, 950)
    page.show()
    qtbot.wait(20)
    image_cache_budget.clear()
    request.addfinalizer(image_cache_budget.clear)
    policy = ImageCacheBudget(total_bytes=64 * MIB)
    monkeypatch.setattr(overview, "image_cache_budget", policy)
    submissions, lookups, tokens = [], [], {}

    def enqueue(item_id, path, width, height, request_id, *, mode, dpr):
        submissions.append((item_id, path, overview.PreviewLayout(width, height, mode, dpr), request_id))
        tokens[request_id] = overview.preview_cache_token()

    cached = overview.cached_preview

    def lookup(path, size, **kwargs):
        if not kwargs.get("validate", True):
            lookups.append((str(path), size.width(), size.height()))
        return cached(path, size, **kwargs)

    monkeypatch.setattr(page._pixmap_worker, "enqueue", enqueue)
    monkeypatch.setattr(overview, "cached_preview", lookup)
    ticks = iter(range(100_000))
    monkeypatch.setattr(overview, "perf_counter", lambda: next(ticks) * 0.02)
    return SimpleNamespace(
        page=page, policy=policy, submissions=submissions, lookups=lookups, tokens=tokens, directory=tmp_path
    )


def _set_data(rig, kind="mod", prefix="item", count=240):
    mods = [
        Mod(
            mod_id=f"{prefix}-{i:04}",
            name=f"{prefix} {i:04}",
            preview_images=[str(rig.directory / f"{prefix}-{i:04}.png")],
        )
        for i in range(count)
    ]
    if kind == "category":
        rig.page.set_characters([dict(id=mod.mod_id, name=mod.name, thumbnail=mod.primary_preview) for mod in mods])
    else:
        rig.page.set_current_mods(mods, mods)


def _stop_timers(page):
    for timer in (
        page._vs_population_timer,
        page._vs_scroll_timer,
        page._vs_pixmap_timer,
        page._resize_timer,
        page._scale_timer,
    ):
        timer.stop()


def _settle(page):
    # 同步样本先完成缩放静默定时器，再检查稳定布局的预取范围。
    if page._scale_timer.isActive() and not page._card_size_slider.isSliderDown():
        page._apply_card_scale_pixmaps()
    slices = 0
    while page._vs_population_pending and slices < 200:
        page._vs_update_visible()
        slices += 1
    assert not page._vs_population_pending
    _stop_timers(page)
    return slices


def _adjacent(page):
    rows = max(1, (page._scroll_area.viewport().height() + page._vs_row_h - 1) // page._vs_row_h)
    _, _, first, last = page._vs_visible_rows(rows)
    return [item for item in page._display_items[first : last + 1] if item["id"] not in page._vs_card_cache]


def _drain(page):
    """直接完成拦截的请求，避免后台任务及事件循环竞争。"""
    for _ in range(200):
        page._vs_load_deferred_pixmaps()
        assert len(page._vs_image_requests) <= page._VS_MAX_IMAGE_REQUESTS
        assert sum(card is None for _, card, _ in page._vs_image_requests.values()) <= page._VS_MAX_PREFETCH_REQUESTS
        for item_id, (request_id, _card, path) in list(page._vs_image_requests.items()):
            page._on_bg_pixmap_loaded(item_id, path, request_id, None)
        if not page._vs_pending_pixmap_cards and not page._vs_pending_prefetch and not page._vs_image_requests:
            _stop_timers(page)
            return
    pytest.fail("prefetch must finish instead of rebuilding a consumed queue")


def _assert_one_adjacent_scan(rig):
    expected = {rig.page._vs_item_preview_path(item) for item in _adjacent(rig.page)}
    counts = Counter(path for path, _width, _height in rig.lookups)
    assert expected, "the fixture must include offscreen candidates"
    assert counts == Counter({path: 1 for path in expected})


def test_population_slices_scan_adjacent_candidates_once(rig):
    _set_data(rig)

    assert _settle(rig.page) > 5

    _assert_one_adjacent_scan(rig)
    rig.lookups.clear()
    for _ in range(10):
        rig.page._vs_update_visible()
    assert not rig.lookups, "pixel-level updates must reuse the prepared queue"


@pytest.mark.parametrize("change", ["row", "scale", "dpr", "directory"])
def test_new_window_data_or_layout_gets_one_current_prefetch_scan(rig, monkeypatch, change):
    _set_data(rig)
    _settle(rig.page)
    rig.lookups.clear()
    page = rig.page
    if change == "row":
        page._scroll_area.verticalScrollBar().setValue(page._vs_row_h * 12)
    elif change == "scale":
        page._on_card_scale_changed(170)
    elif change == "dpr":
        monkeypatch.setattr(page._scroll_content, "devicePixelRatioF", lambda: 2.0)
    else:
        _set_data(rig, prefix="replacement")

    page._vs_update_visible()
    _settle(page)

    _assert_one_adjacent_scan(rig)


def test_dpr_event_refreshes_prefetch_without_scrolling(rig, qtbot, monkeypatch):
    rig.page._card_scale = 200
    _set_data(rig)
    _settle(rig.page)
    rig.lookups.clear()
    monkeypatch.setattr(rig.page._scroll_content, "devicePixelRatioF", lambda: 2.0)

    QCoreApplication.sendEvent(rig.page._scroll_area.viewport(), QEvent(QEvent.Type.DevicePixelRatioChange))

    qtbot.waitUntil(
        lambda: any(width > overview._PixmapLoadWorker._MAX_CACHE_SIZE.width() for _, width, _ in rig.lookups),
        timeout=1000,
    )


@pytest.mark.parametrize("kind", ["mod", "category"])
@pytest.mark.parametrize("seed_size", ["worker", "base"])
@pytest.mark.parametrize("stage", ["preparation", "dispatch"])
def test_high_dpr_prefetch_uses_worker_cache_dimensions(rig, monkeypatch, kind, seed_size, stage):
    page = rig.page
    page._card_scale = 200
    monkeypatch.setattr(page._scroll_content, "devicePixelRatioF", lambda: 2.0)
    monkeypatch.setattr(overview.ModCard, "devicePixelRatioF", lambda _self: 2.0)
    monkeypatch.setattr(overview.CharacterCard, "devicePixelRatioF", lambda _self: 2.0)
    rig.policy.set_budget(0)
    _set_data(rig, kind=kind)
    _settle(page)
    target = _adjacent(page)[0]
    path = page._vs_item_preview_path(target)
    image = QImage(32, 24, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    assert image.save(path)
    layout = page._vs_preview_layout(next(iter(page._vs_card_cache.values())))
    actual_size = overview._PixmapLoadWorker.cache_size(layout)
    assert actual_size != overview._PixmapLoadWorker._MAX_CACHE_SIZE
    seed = actual_size if seed_size == "worker" else overview._PixmapLoadWorker._MAX_CACHE_SIZE
    rig.policy.set_budget(64 * MIB)
    _, _, first, last = page._vs_visible_rows()
    if stage == "dispatch":
        page._vs_prepare_prefetch(first, last)
    load_preview(path, seed, overview.load_scaled_qimage_file)
    if stage == "preparation":
        page._vs_prepare_prefetch(first, last)

    _drain(page)

    submitted = [entry for entry in rig.submissions if entry[0] == target["id"]]
    assert bool(submitted) is (seed_size == "base")
    if submitted:
        assert submitted[0][2] == layout


@pytest.mark.parametrize("consumed", [False, True])
@pytest.mark.parametrize("entrypoint", ["update", "scheduler"])
def test_pressure_discards_pending_prefetch_and_recovery_rebuilds_same_window(rig, consumed, entrypoint):
    _set_data(rig)
    _settle(rig.page)
    adjacent_ids = {item["id"] for item in _adjacent(rig.page)}
    if consumed:
        _drain(rig.page)
    else:
        assert rig.page._vs_pending_prefetch
    for now in (0, 5, 10):
        rig.policy.observe_memory(8 * 1024 * MIB, 100 * MIB, now=now)
    assert not rig.policy.prefetch_allowed

    if entrypoint == "update":
        rig.page._vs_update_visible()
    else:
        rig.page._vs_schedule_pixmaps()

    assert not rig.page._vs_pending_prefetch, "pressure must discard unsubmitted speculative work"
    _drain(rig.page)
    rig.submissions.clear()
    for now in (70, 75, 80, 85, 90, 95):
        rig.policy.observe_memory(8 * 1024 * MIB, 4 * 1024 * MIB, now=now)
    assert rig.policy.prefetch_allowed

    rig.page._vs_update_visible()
    _drain(rig.page)

    assert any(item_id in adjacent_ids for item_id, *_ in rig.submissions)


@pytest.mark.parametrize("kind", ["mod", "category"])
@pytest.mark.parametrize("consumed", [False, True])
def test_changed_preview_path_replaces_old_speculative_work(rig, kind, consumed):
    _set_data(rig, kind=kind)
    _settle(rig.page)
    target = _adjacent(rig.page)[0]
    if consumed:
        _drain(rig.page)
    rig.submissions.clear()
    new_path = str(rig.directory / "changed-preview.png")
    if kind == "mod":
        rig.page.update_mod_in_view(Mod(mod_id=target["id"], name=target["data"].name, preview_images=[new_path]))
    else:
        rig.page.apply_node_delta([{**target["data"], "thumbnail": new_path}])

    rig.page._vs_update_visible()
    _settle(rig.page)
    _drain(rig.page)

    paths = [path for item_id, path, *_ in rig.submissions if item_id == target["id"]]
    assert paths == [new_path]


def test_visible_requests_stay_first_and_all_work_remains_bounded(rig):
    _set_data(rig, count=2400)
    page = rig.page
    page._vs_load_deferred_pixmaps()
    assert rig.submissions[0][0] == page._display_items[0]["id"]
    assert all(card is not None for _, card, _ in page._vs_image_requests.values())
    _settle(page)
    active_ids = set(page._vs_card_cache)
    before_widgets = page.findChildren(overview.ModCard)

    _drain(page)

    spec_positions = [i for i, entry in enumerate(rig.submissions) if entry[0] not in active_ids]
    visible_positions = [i for i, entry in enumerate(rig.submissions) if entry[0] in active_ids]
    assert spec_positions and max(visible_positions) < min(spec_positions)
    assert page.findChildren(overview.ModCard) == before_widgets
    rows = (page._scroll_area.viewport().height() + page._vs_row_h - 1) // page._vs_row_h
    assert len(active_ids) <= (rows + 2 * page._VS_SCROLL_BUFFER_ROWS + 1) * page._vs_cols
    assert len(rig.submissions) <= len(active_ids) + 2 * rows * page._vs_cols


@pytest.fixture
def real_cache_rig(rig, monkeypatch):
    """页面策略和真实预览函数共享隔离的字节预算。"""
    for name, partition in (
        ("_images", "local_viewed"),
        ("_prefetched_images", "local_prefetch"),
        ("_rendered_images", "local_rendered"),
    ):
        cache = ImageMemoryCache(max_bytes=0)
        rig.policy.register(cache, partition)
        monkeypatch.setattr(local_preview_cache, name, cache)
    return rig


def _save_picture(path):
    picture = QImage(32, 24, QImage.Format.Format_RGB32)
    picture.fill(QColor("red"))
    assert picture.save(str(path))


def _complete_real(rig, submission):
    """执行已拦截任务并保留原始准入令牌。"""
    item_id, path, layout, request_id = submission
    preview = load_preview(
        path,
        overview._PixmapLoadWorker.cache_size(layout),
        overview.load_scaled_qimage_file,
        cache_token=rig.tokens[request_id],
    )
    result = overview.prepare_preview(preview, layout)
    rig.page._on_bg_pixmap_loaded(item_id, path, request_id, result)
    return result


def _pump_real(rig, *, hold=None):
    page = rig.page
    for _ in range(200):
        page._vs_load_deferred_pixmaps()
        assert len(page._vs_image_requests) <= 8
        assert sum(card is None for _, card, _ in page._vs_image_requests.values()) <= 2
        requests = list(page._vs_image_requests.items())
        for item_id, (request_id, _card, _path) in requests:
            if item_id != hold:
                submission = next(entry for entry in rig.submissions if entry[3] == request_id)
                _complete_real(rig, submission)
        if hold in page._vs_image_requests:
            _stop_timers(page)
            return next(entry for entry in rig.submissions if entry[3] == page._vs_image_requests[hold][0])
        if not page._vs_pending_pixmap_cards and not page._vs_pending_prefetch and not page._vs_image_requests:
            _stop_timers(page)
            return None
    pytest.fail("worker completion must eventually consume this window's finite work")


def _start_held_adjacent(rig, monkeypatch, *, kind="mod", dpr=1.0):
    page = rig.page
    page._card_scale = 200
    monkeypatch.setattr(page._scroll_content, "devicePixelRatioF", lambda: dpr)
    monkeypatch.setattr(overview.ModCard, "devicePixelRatioF", lambda _self: dpr)
    monkeypatch.setattr(overview.CharacterCard, "devicePixelRatioF", lambda _self: dpr)
    _set_data(rig, kind=kind)
    _settle(page)
    page._vs_update_visible()
    _settle(page)
    target = _adjacent(page)[0]
    _save_picture(page._vs_item_preview_path(target))
    submission = _pump_real(rig, hold=target["id"])
    assert submission is not None
    assert page._vs_image_requests[target["id"]][1] is None
    return target, submission


@pytest.mark.parametrize("kind", ["mod", "category"])
@pytest.mark.parametrize("change", ["dpr", "size", "cache"])
def test_old_inflight_prefetch_refills_current_cache_without_rescanning_neighbors(
    real_cache_rig,
    monkeypatch,
    kind,
    change,
):
    rig = real_cache_rig
    page = rig.page
    target, old = _start_held_adjacent(rig, monkeypatch, kind=kind, dpr=2.0 if change == "size" else 1.0)
    if change == "dpr":
        monkeypatch.setattr(page._scroll_content, "devicePixelRatioF", lambda: 2.0)
    elif change == "size":
        page._on_card_scale_changed(195)
    else:
        rig.policy.clear()
    page._vs_update_visible()
    _settle(page)
    assert target["id"] in {item["id"] for item in _adjacent(page)}
    assert page._vs_image_requests[target["id"]][0] == old[3], "keep useful old work in flight"
    layout = page._vs_item_preview_layout(target)
    size = overview._PixmapLoadWorker.cache_size(layout)
    assert change == "cache" or size != overview._PixmapLoadWorker.cache_size(old[2])
    rig.lookups.clear()

    _complete_real(rig, old)

    assert {path for path, *_ in rig.lookups} <= {old[1]}, "completion must only reconsider its waiting candidate"
    assert local_preview_cache.cached_preview(old[1], size, validate=False) is None
    _pump_real(rig)
    target_submissions = [entry for entry in rig.submissions if entry[0] == target["id"]]
    assert len(target_submissions) == 2, "the still-offscreen target needs a current-layout/current-generation request"
    assert target_submissions[-1][2] == layout
    assert rig.tokens[target_submissions[-1][3]] == overview.preview_cache_token()
    assert local_preview_cache.cached_preview(old[1], size, validate=False) is not None
    assert target["id"] not in page._vs_card_cache
    rig.lookups.clear()
    completed_count = len(rig.submissions)
    for _ in range(10):
        page._vs_update_visible()
        page._vs_schedule_pixmaps()
    assert not rig.lookups
    assert len(rig.submissions) == completed_count


def test_failed_current_prefetch_is_not_retried_by_completions_or_stable_updates(real_cache_rig):
    rig = real_cache_rig
    _set_data(rig)
    _settle(rig.page)
    rig.page._vs_update_visible()
    _settle(rig.page)
    adjacent_ids = {item["id"] for item in _adjacent(rig.page)}
    _pump_real(rig)  # 此测试样本不包含图像文件。
    counts = Counter(item_id for item_id, *_ in rig.submissions if item_id in adjacent_ids)
    assert counts == Counter({item_id: 1 for item_id in adjacent_ids})
    previous = len(rig.submissions)
    rig.lookups.clear()
    for _ in range(10):
        rig.page._vs_update_visible()
        _pump_real(rig)
    assert not rig.lookups
    assert len(rig.submissions) == previous


@pytest.mark.parametrize("entrypoint", ["update", "scheduler"])
@pytest.mark.parametrize("cycle", ["pressure", "budget"])
def test_idle_page_observes_completed_policy_cycle_and_refills_evicted_prefetch(
    real_cache_rig,
    monkeypatch,
    entrypoint,
    cycle,
):
    rig = real_cache_rig
    target, old = _start_held_adjacent(rig, monkeypatch)
    _complete_real(rig, old)
    _pump_real(rig)
    size = overview._PixmapLoadWorker.cache_size(old[2])
    assert local_preview_cache.cached_preview(old[1], size, validate=False) is not None
    old_token = overview.preview_cache_token()
    rig.submissions.clear()
    if cycle == "pressure":
        for now in (0, 5, 10):
            rig.policy.observe_memory(8 * 1024 * MIB, 100 * MIB, now=now)
        assert not rig.policy.prefetch_allowed
    else:
        rig.policy.set_budget(1)  # 仍允许处理，但容量不足以保留像素。
        assert rig.policy.prefetch_allowed
    assert local_preview_cache.cached_preview(old[1], size, validate=False) is None
    # 淘汰到策略恢复之间不调用页面入口。
    if cycle == "pressure":
        for now in (70, 75, 80, 85, 90, 95):
            rig.policy.observe_memory(8 * 1024 * MIB, 4 * 1024 * MIB, now=now)
    else:
        rig.policy.set_budget(64 * MIB)
    assert rig.policy.prefetch_allowed
    assert overview.preview_cache_token() == old_token, "resize must not masquerade as an explicit cache clear"

    if entrypoint == "update":
        rig.page._vs_update_visible()
    else:
        rig.page._vs_schedule_pixmaps()
    _pump_real(rig)

    assert sum(entry[0] == target["id"] for entry in rig.submissions) == 1
    assert local_preview_cache.cached_preview(old[1], size, validate=False) is not None
    assert rig.policy.snapshot().bytes_used <= rig.policy.snapshot().limit_bytes
    rig.lookups.clear()
    rig.policy.set_budget(64 * MIB)
    rig.policy.observe_memory(8 * 1024 * MIB, 4 * 1024 * MIB, now=100)
    rig.page._vs_update_visible()
    assert not rig.lookups, "unchanged budget and pressure samples must preserve the consumed scan"


def test_failed_old_and_replacement_prefetch_stop_after_one_current_attempt(real_cache_rig, monkeypatch):
    rig = real_cache_rig
    target, old = _start_held_adjacent(rig, monkeypatch)
    Path(old[1]).unlink()
    monkeypatch.setattr(rig.page._scroll_content, "devicePixelRatioF", lambda: 2.0)
    rig.page._vs_update_visible()
    _settle(rig.page)

    result = _complete_real(rig, old)
    assert result.image is None or result.image.isNull()
    _pump_real(rig)
    assert sum(entry[0] == target["id"] for entry in rig.submissions) == 2
    for _ in range(10):
        rig.page._vs_update_visible()
        _pump_real(rig)
    assert sum(entry[0] == target["id"] for entry in rig.submissions) == 2


def test_compatible_old_request_fills_current_scan_without_duplicate_prefetch(real_cache_rig, monkeypatch):
    rig = real_cache_rig
    target, old = _start_held_adjacent(rig, monkeypatch)
    # 修改策略键，同时保持后台任务尺寸不变。
    rig.policy.set_budget(65 * MIB)
    rig.page._vs_schedule_pixmaps()
    _complete_real(rig, old)
    _pump_real(rig)
    assert sum(entry[0] == target["id"] for entry in rig.submissions) == 1
    assert (
        local_preview_cache.cached_preview(old[1], overview._PixmapLoadWorker.cache_size(old[2]), validate=False)
        is not None
    )


@pytest.mark.parametrize("invalidate", ["navigation", "game", "path", "delete", "scroll"])
def test_invalidated_waiting_candidate_cannot_requeue_old_path(real_cache_rig, monkeypatch, invalidate):
    rig = real_cache_rig
    page = rig.page
    target, old = _start_held_adjacent(rig, monkeypatch)
    monkeypatch.setattr(page._scroll_content, "devicePixelRatioF", lambda: 2.0)
    page._vs_update_visible()
    _settle(page)
    assert target["id"] in page._vs_waiting_prefetch
    assert len(page._vs_waiting_prefetch) <= len(page._vs_image_requests) <= 8
    if invalidate == "navigation":
        page.begin_loading()
    elif invalidate == "game":
        page.set_current_game("replacement")
    elif invalidate == "path":
        page.update_mod_in_view(
            Mod(mod_id=target["id"], name=target["data"].name, preview_images=[str(rig.directory / "replacement.png")])
        )
        page._vs_update_visible()
        _settle(page)
    elif invalidate == "delete":
        assert page.apply_reconcile_delta([], [target["id"]])
        _settle(page)
    else:
        page._scroll_area.verticalScrollBar().setValue(page._vs_row_h * 20)
        page._vs_update_visible()
        _settle(page)
    assert target["id"] not in page._vs_waiting_prefetch

    _complete_real(rig, old)
    _pump_real(rig)

    assert sum(entry[0] == target["id"] and entry[1] == old[1] for entry in rig.submissions) == 1
    assert target["id"] not in page._vs_waiting_prefetch
    assert target["id"] not in page._vs_card_cache or page._vs_card_cache[target["id"]]._cached_pixmap is None
    if invalidate == "path":
        assert any(entry[0] == target["id"] and entry[1].endswith("replacement.png") for entry in rig.submissions)
