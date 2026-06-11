import builtins
from dataclasses import replace
from pathlib import Path

import pytest

from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


@pytest.mark.parametrize("old_file", [False, True])
def test_probe_ignores_persisted_marker_cache(mm, tmp_path, monkeypatch, old_file):
    from src.core.library_snapshot import ProbeToken, probe_library

    node = mm.add_character(name="direct", node_type="character")
    mod = _import_mod(mm, node.id, tmp_path, "direct")
    request = replace(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)), mode="full")
    cache = Path(request.cache_root).parent / ".library-marker-facts.json"
    old_bytes = b'{"format":1,"entries":{"untrusted":"stale"}}'
    if old_file:
        cache.write_bytes(old_bytes)
    original_import = builtins.__import__

    def without_retired_modules(name, *args, **kwargs):
        assert name not in ("src.core.journal_marker_cache", "src.core.usn_journal"), name
        return original_import(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", without_retired_modules)
        result = probe_library(request)
    assert not result.error
    assert any(entry.source and entry.source.mod_id == mod.mod_id for entry in result.source_inventory.entries)
    assert cache.read_bytes() == old_bytes if old_file else not cache.exists()
    assert not tuple(cache.parent.glob(".journal-*"))
