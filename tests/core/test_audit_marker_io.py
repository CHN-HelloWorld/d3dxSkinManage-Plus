from dataclasses import replace
from pathlib import Path

from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def test_raw_audit_reads_marker_without_redundant_readability_handles(mm, tmp_path, monkeypatch):
    from src.core import watcher_windows
    from src.core.library_snapshot import ProbeToken, probe_library

    node = mm.add_character(name="Audit", node_type="character")
    mod = _import_mod(mm, node.id, tmp_path, "raw")
    request = replace(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)), mode="full", force_source_audit=True)
    original = watcher_windows.open_marker
    opens = []

    def opened(path):
        opens.append(str(path))
        return original(path)

    monkeypatch.setattr(watcher_windows, "open_marker", opened)
    result = probe_library(request)
    assert not result.error
    assert [entry.source.mod_id for entry in result.source_inventory.entries if entry.source] == [mod.mod_id]
    assert str(Path(mod.path).parent / ".d3dxmod") not in opens
