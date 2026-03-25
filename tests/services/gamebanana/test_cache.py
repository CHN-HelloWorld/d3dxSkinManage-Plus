import asyncio

import pytest

from src.services.gamebanana.cache import GbApiCache, ImageCache

pytestmark = pytest.mark.asyncio


async def _ret(v):
    return v


async def test_image_cache_dir_is_fixed_top_level_cache(temp_data_dir):
    """缩略图缓存固定使用 cache/gb_images，不迁移其他目录。"""
    from src.utils import paths as paths_mod

    legacy = temp_data_dir / "data" / "cache" / "gb_images"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "old.jpg").write_bytes(b"x")

    assert paths_mod.get_gb_image_cache_dir() == temp_data_dir / "cache" / "gb_images"
    assert (legacy / "old.jpg").exists()  # 默认目录调整不应移动或删除旧目录中的文件。


async def test_apicache_caches_first_call():
    cache = GbApiCache()
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return {"data": calls["n"]}

    a = await cache.get_or_fetch("k", ttl=60, fetcher=fetch)
    b = await cache.get_or_fetch("k", ttl=60, fetcher=fetch)
    assert a == b
    assert calls["n"] == 1


async def test_apicache_expires_after_ttl(monkeypatch):
    cache = GbApiCache()
    base = [1000.0]
    # 缓存过期时间使用墙钟，以便跨重启比较持久化时间戳。
    monkeypatch.setattr("src.services.gamebanana.cache.time.time", lambda: base[0])

    async def fetch():
        return {"v": base[0]}

    await cache.get_or_fetch("k", ttl=10, fetcher=fetch)
    base[0] += 11
    v = await cache.get_or_fetch("k", ttl=10, fetcher=fetch)
    assert v == {"v": 1011.0}


async def test_apicache_lru_evicts_when_full():
    cache = GbApiCache(max_entries=2)
    await cache.get_or_fetch("a", 60, lambda: _ret("A"))
    await cache.get_or_fetch("b", 60, lambda: _ret("B"))
    await cache.get_or_fetch("c", 60, lambda: _ret("C"))
    # 淘汰 a 后再次获取会增加请求次数。
    calls = {"n": 0}

    async def fetch_a():
        calls["n"] += 1
        return "A-new"

    v = await cache.get_or_fetch("a", 60, fetch_a)
    assert calls["n"] == 1
    assert v == "A-new"


async def test_apicache_concurrent_calls_share():
    cache = GbApiCache()
    calls = {"n": 0}
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow():
        calls["n"] += 1
        started.set()
        await release.wait()
        return "ok"

    t1 = asyncio.create_task(cache.get_or_fetch("k", 60, slow))
    await started.wait()
    t2 = asyncio.create_task(cache.get_or_fetch("k", 60, slow))
    release.set()
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1 == r2 == "ok"
    assert calls["n"] == 1


async def test_imagecache_path_stable(tmp_path):
    cache = ImageCache(base_dir=tmp_path)
    p1 = cache.get_path("https://example.com/a.png")
    p2 = cache.get_path("https://example.com/a.png")
    assert p1 == p2
    assert p1.parent == tmp_path


async def test_imagecache_fetch_writes_file(tmp_path, respx_mock):
    import httpx

    respx_mock.get("https://example.com/x.png").mock(return_value=httpx.Response(200, content=b"\x89PNG\r\n\x1a\n"))
    cache = ImageCache(base_dir=tmp_path)
    path = await cache.fetch("https://example.com/x.png")
    assert path.exists()
    assert path.read_bytes().startswith(b"\x89PNG")


async def test_imagecache_fetch_hit_skips_network(tmp_path):
    cache = ImageCache(base_dir=tmp_path)
    # 预先写入缓存；未注册网络路由，任何 HTTP 请求都会失败。
    target = cache.get_path("https://example.com/cached.png")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"prefilled")
    path = await cache.fetch("https://example.com/cached.png")
    assert path.read_bytes() == b"prefilled"
