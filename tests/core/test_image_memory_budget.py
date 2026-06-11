import weakref
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.utils.image_memory_cache import ImageMemoryCache


def test_shrink_evicts_oldest_immediately_and_zero_rejects_new_images():
    cache = ImageMemoryCache(max_bytes=12)
    first, second, third = object(), object(), object()
    for key, value in enumerate((first, second, third)):
        assert cache.put(key, value, cost_bytes=4)
    assert cache.get(0) is first  # 此次访问使编号 0 的使用时间晚于 1 和 2。
    cache.resize(8)
    assert cache.get(1) is None
    assert cache.get(0) is first
    assert cache.get(2) is third
    assert cache.bytes_used == 8
    cache.resize(0)
    assert cache.bytes_used == 0
    assert cache.entry_count == 0
    assert not cache.put("late", object(), cost_bytes=1)


def test_clear_rejects_results_started_before_clear_but_resize_accepts_fresh_work():
    cache = ImageMemoryCache(max_bytes=12)
    generation = cache.generation
    cache.clear()
    assert not cache.put("obsolete", object(), cost_bytes=4, expected_generation=generation)
    generation = cache.generation
    cache.resize(4)
    assert cache.put("current", object(), cost_bytes=4, expected_generation=generation)
    assert cache.bytes_used == 4


@pytest.mark.parametrize("bad", [-1, True, 1.5])
def test_bad_resize_does_not_change_existing_images(bad):
    cache = ImageMemoryCache(max_bytes=8)
    image = object()
    cache.put("image", image, cost_bytes=4)
    with pytest.raises((ValueError, TypeError)):
        cache.resize(bad)
    assert cache.max_bytes == 8
    assert cache.get("image") is image


def test_concurrent_resize_and_put_keep_accounting_bounded():
    cache = ImageMemoryCache(max_bytes=32)

    def write():
        for index in range(400):
            cache.put(index, object(), cost_bytes=4)

    def resize():
        for index in range(400):
            cache.resize((index % 9) * 4)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(write), executor.submit(write), executor.submit(resize)]
        for future in futures:
            future.result()
    cache.resize(8)
    assert cache.bytes_used <= 8
    assert cache.entry_count <= 2


def test_online_instances_share_allowance_and_do_not_evict_viewed_local_images():
    from src.utils.image_cache_budget import ImageCacheBudget

    budget = ImageCacheBudget(total_bytes=768)
    local = ImageMemoryCache(max_bytes=0)
    online = ImageMemoryCache(max_bytes=0)
    another = ImageMemoryCache(max_bytes=0)
    prefetch = ImageMemoryCache(max_bytes=0)
    budget.register(local, "local_viewed")
    budget.register(online, "online_viewed")
    image = object()
    local.put("viewed", image, cost_bytes=300)
    online.put("old", object(), cost_bytes=80)
    previous_allowance = online.max_bytes
    budget.register(another, "online_viewed")
    assert online.max_bytes + another.max_bytes == previous_allowance
    assert online.bytes_used <= online.max_bytes
    budget.register(prefetch, "online_prefetch")
    for index in range(100):
        prefetch.put(index, object(), cost_bytes=16)
        another.put(index, object(), cost_bytes=24)
    assert local.get("viewed") is image
    assert budget.snapshot().bytes_used <= 768
    assert sum(c.max_bytes for c in (local, online, another, prefetch)) <= 768


def test_registration_is_weak_and_unregister_releases_and_disables_cache():
    from src.utils.image_cache_budget import ImageCacheBudget

    budget = ImageCacheBudget(total_bytes=768)
    first = ImageMemoryCache(max_bytes=0)
    second = ImageMemoryCache(max_bytes=0)
    budget.register(first, "online_viewed")
    original_limit = first.max_bytes
    budget.register(second, "online_viewed")
    reference = weakref.ref(second)
    del second
    assert reference() is None
    assert first.max_bytes == original_limit
    first.put("pixels", object(), cost_bytes=4)
    budget.unregister(first)
    assert first.bytes_used == 0
    assert not first.put("late", object(), cost_bytes=4)
    assert budget.snapshot().cache_count == 0


def test_global_shrink_and_clear_cover_every_registered_partition():
    from src.utils.image_cache_budget import PARTITION_WEIGHTS, ImageCacheBudget

    budget = ImageCacheBudget(total_bytes=768)
    caches = []
    for partition in PARTITION_WEIGHTS:
        cache = ImageMemoryCache(max_bytes=0)
        budget.register(cache, partition)
        cache.put(partition, object(), cost_bytes=cache.max_bytes)
        caches.append(cache)
    assert budget.snapshot().bytes_used == 768
    budget.set_budget(128)
    assert sum(cache.max_bytes for cache in caches) <= 128
    assert budget.snapshot().bytes_used <= 128
    generations = [cache.generation for cache in caches]
    budget.clear()
    for cache, generation in zip(caches, generations):
        assert not cache.put("late", object(), cost_bytes=1, expected_generation=generation)
    assert budget.snapshot().bytes_used == 0


def test_sustained_pressure_stops_prefetch_without_overwriting_saved_budget():
    from src.utils.image_cache_budget import ImageCacheBudget

    mib = 1024**2
    total = 16 * 1024**3
    budget = ImageCacheBudget(total_bytes=768 * mib)
    viewed, speculative = ImageMemoryCache(max_bytes=0), ImageMemoryCache(max_bytes=0)
    budget.register(viewed, "local_viewed")
    budget.register(speculative, "local_prefetch")
    visible_image = object()
    viewed.put("small working set", visible_image, cost_bytes=4)
    for now in (0, 5):
        budget.observe_memory(total, 300 * mib, now=now)
        assert not budget.under_pressure
    budget.observe_memory(total, 300 * mib, now=10)
    assert budget.under_pressure
    assert speculative.max_bytes == 0
    assert viewed.get("small working set") is visible_image
    assert budget.snapshot().configured_bytes == 768 * mib
    assert budget.snapshot().limit_bytes < 768 * mib
    for now in (15, 20):
        budget.observe_memory(total, 4 * 1024**3, now=now)
    assert budget.under_pressure, "brief headroom must not cause eviction/refill oscillation"
    for now in (70, 75, 80, 85, 90, 95):
        budget.observe_memory(total, 4 * 1024**3, now=now)
    assert not budget.under_pressure
    assert speculative.max_bytes > 0
    assert budget.snapshot().limit_bytes == 768 * mib
