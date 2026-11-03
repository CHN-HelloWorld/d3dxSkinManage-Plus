"""验证默认 INI 的热读复用、旧版本退役和缓存容量边界。"""

from types import SimpleNamespace

from src.core.mod_variant_manager import ModVariantManager
from src.core.persist_vars import ModIniSource, build_attribution_index


def _source(tmp_path, name="body", value="0"):
    """创建仅包含一个持久变量的隔离文件夹资源。"""
    path = tmp_path / name
    path.mkdir(exist_ok=True)
    text = f"[Constants]\nglobal persist $value = {value}\n"
    (path / "merged.ini").write_text(text, encoding="utf-8")
    mod = SimpleNamespace(mod_id=name, path=str(path), content_hash=value)
    index = build_attribution_index((ModIniSource(name, (("merged.ini", text),), "mods\\audit"),))
    return mod, index


def test_new_source_versions_replace_old_defaults_without_losing_hot_reads(tmp_path, monkeypatch):
    import src.core.variant_configuration as configuration

    service = ModVariantManager(SimpleNamespace())
    original = configuration.original_inis
    reads = []

    def read(path):
        """记录真实正文读取，用于验证缓存命中时不重复访问文件。"""
        reads.append(path)
        return original(path)

    monkeypatch.setattr(configuration, "original_inis", read)
    for revision in range(8):
        mod, index = _source(tmp_path, value=str(revision))
        service.invalidate(mod.mod_id)
        assert service._target_values(mod, None, index) == {"$\\mods\\audit\\merged.ini\\value": str(revision)}
        for _ in range(3):
            assert service._target_values(mod, None, index) == {"$\\mods\\audit\\merged.ini\\value": str(revision)}
        assert len(service._default_cache) == 1
    assert len(reads) == 8


def test_deleted_mod_retires_only_its_defaults_and_enable_keeps_hot_cache(tmp_path):
    service = ModVariantManager(SimpleNamespace())
    first, first_index = _source(tmp_path, "first")
    second, second_index = _source(tmp_path, "second")
    service._target_values(first, None, first_index)
    service._target_values(second, None, second_index)
    service._on_library_event("mod_enabled", mod_id=first.mod_id)
    assert len(service._default_cache) == 2
    service._on_library_event("mod_deleted", mod_id=first.mod_id)
    assert len(service._default_cache) == 1
    service._target_values(second, None, second_index)
    assert len(service._default_cache) == 1


def test_default_cache_evicts_cold_sources_and_keeps_snapshot_copies_independent(tmp_path):
    from src.core.variant_default_cache import DefaultIniCache

    cache = DefaultIniCache(max_entries=2, max_bytes=4096)
    loads = []

    def read():
        """提供小型不可变正文，并记录缓存缺失。"""
        loads.append(True)
        return (("merged.ini", "[Constants]"),)

    first = (str(tmp_path / "a"), "a", 1, 1)
    second = (str(tmp_path / "b"), "b", 1, 1)
    third = (str(tmp_path / "c"), "c", 1, 1)
    cache.read("a", first, read)
    cache.read("b", second, read)
    cache.read("a", first, read)
    cache.read("c", third, read)
    assert len(cache) == 2 and len(loads) == 3
    cache.read("a", first, read)
    assert len(loads) == 3
    snapshot = cache.copy()
    snapshot.read("b", second, read)
    cache.read("c", third, read)
    assert len(loads) == 4
    snapshot.clear()
    assert len(snapshot) == 0 and len(cache) == 2


def test_oversized_default_ini_is_usable_without_remaining_cached(tmp_path):
    from src.core.variant_default_cache import DefaultIniCache

    cache = DefaultIniCache(max_entries=4, max_bytes=1024)
    inis = (("merged.ini", "x" * 2048),)
    assert cache.read("a", (str(tmp_path / "a"), "a", 1, 1), lambda: inis) is inis
    assert len(cache) == 0


def test_moved_mod_and_replaced_source_retire_previous_owners(tmp_path):
    from src.core.variant_default_cache import DefaultIniCache

    cache = DefaultIniCache()
    inis = (("merged.ini", "[Constants]"),)
    old = (str(tmp_path / "old"), "body", 1, 1)
    new = (str(tmp_path / "new"), "body", 1, 1)
    cache.read("first", old, lambda: inis)
    cache.read("first", new, lambda: inis)
    assert len(cache) == 1
    cache.read("second", new, lambda: inis)
    cache.discard("first")
    assert len(cache) == 1
    cache.discard("second")
    assert len(cache) == 0


def test_total_body_budget_evicts_old_entries_before_item_limit(tmp_path):
    from src.core.variant_default_cache import DefaultIniCache

    cache = DefaultIniCache(max_entries=8, max_bytes=2048)
    inis = (("merged.ini", "x" * 1000),)
    cache.read("a", (str(tmp_path / "a"), "a", 1, 1), lambda: inis)
    assert len(cache) == 1
    cache.read("b", (str(tmp_path / "b"), "b", 1, 1), lambda: inis)
    assert len(cache) == 1
