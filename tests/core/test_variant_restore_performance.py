from tests.core.test_named_variant_manager import enable
from tests.core.test_variant_preset_switch import _write_user_ini
from tests.core.test_variant_preset_switch import env as env


def test_loader_defaults_need_no_archive_reads(env, monkeypatch):
    from src.services.archive import ArchiveService

    mm, mod, _ = enable(env)
    mm.variants._default_cache.clear()
    original = ArchiveService.read_all_inis
    reads = []

    def read(service, path):
        reads.append(str(path))
        return original(service, path)

    monkeypatch.setattr(ArchiveService, "read_all_inis", read)
    mm.variants.apply_variant(mod.mod_id, None)
    mm.variants.apply_variant(mod.mod_id, None)
    assert reads == []


def test_watcher_does_not_extract_unseeded_archives(env, monkeypatch):
    import src.core.mod_variant_manager as module

    mm, mod, _ = enable(env)
    before = mm.variants.load_prefs().snapshot_for(mod.mod_id)
    _write_user_ini(env["user_ini"], "9")
    monkeypatch.setattr(module, "overrides_seeded", lambda path: False)

    def unexpected_archive_read(path):
        raise AssertionError("watcher must not extract an archive on the UI thread")

    monkeypatch.setattr(module, "inspect_effective_inis", unexpected_archive_read)
    mm.variants.collect(trigger="watch", only_if_changed=True)
    assert mm.variants.load_prefs().snapshot_for(mod.mod_id) == before
