import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def test_offline_move_replayed_after_probe_without_sql_commit(mm, tmp_path):
    from src.core.library_snapshot import ProbeToken, probe_library

    node = mm.add_character(name="Recovery", node_type="character")
    mod = _import_mod(mm, node.id, tmp_path, "offline")
    mm.set_deploy_dir(str(tmp_path / "deploy"))
    assert mm.toggle_mod(mod.mod_id)
    request = replace(mm.create_library_probe_request(ProbeToken("game_a", 1, 1)), mode="full")
    first = probe_library(request)
    assert not first.error
    old = Path(mod.path).parent
    new = old.with_name("offline-moved")
    old.rename(new)
    uncommitted = probe_library(request)
    assert not uncommitted.error
    assert any(item.mod_id == mod.mod_id for item in uncommitted.relocated)
    # 模拟文件系统已发布而索引尚未提交时进程退出。
    assert mm.get_mod(mod.mod_id).path == mod.path
    recovered = probe_library(request)
    assert not recovered.error
    list(mm.apply_library_probe_result(recovered))
    updated = mm.get_mod(mod.mod_id)
    assert updated.path == str(new / Path(mod.path).name)
    assert updated.is_enabled
    assert updated.imported_at == mod.imported_at


@pytest.mark.parametrize("force", [False, True])
def test_inventory_reads_changed_marker_without_persisted_facts(mm, tmp_path, force):
    from src.core import library_inventory
    from src.core.library_snapshot import ProbeToken

    node = mm.add_character(name="Recovery", node_type="character")
    mod = _import_mod(mm, node.id, tmp_path, "unchanged_version")
    marker = Path(mod.path).parent / ".d3dxmod"
    request = mm.create_library_probe_request(ProbeToken("game_a", 1, 1))
    previous = library_inventory.scan_source_inventory(request)
    data = json.loads(marker.read_text(encoding="utf8"))
    data["content_hash"] = "b" * 40
    marker.write_text(json.dumps(data), encoding="utf8")
    current = library_inventory.scan_source_inventory(request, previous, force=force)
    actual = next(item for item in current.entries if item.source and item.source.mod_id == mod.mod_id)
    assert actual.content_hash == "b" * 40
