from dataclasses import replace

import pytest

from src.core.library_snapshot import ProbeToken
from src.models.deployment import DeploymentRecord, DeploymentState
from tests.core.test_library_reconcile import mm as mm
from tests.helpers import make_mod


def populate(manager, count=130):
    mods = [make_mod(manager._index, f"Mod{i:03}") for i in range(count)]
    with manager._index.transaction():
        for mod in mods:
            assert manager._index.add_mod(mod, commit=False)
            manager._index.upsert_deployment(
                DeploymentRecord(mod.mod_id, DeploymentState.ACTIVE, f"C:/deploy/{mod.mod_id}"), commit=False
            )
    return mods


def finish(iterator):
    yielded = 0
    while True:
        try:
            assert next(iterator) is None
            yielded += 1
        except StopIteration as done:
            return yielded, done.value


def test_row_capture_yields_before_materializing_whole_library_and_close_does_not_cache(mm, monkeypatch):
    populate(mm)
    projected = []
    original = mm._index._container

    def container(row, cache):
        projected.append(row["mod_id"])
        return original(row, cache)

    monkeypatch.setattr(mm._index, "_container", container)
    iterator = mm._index.iter_library_rows("game_a")
    assert next(iterator) is None
    assert len(projected) == 64
    assert mm._index._library_rows_cache is None
    iterator.close()
    assert mm._index._library_rows_cache is None
    yields, rows = finish(mm._index.iter_library_rows("game_a"))
    assert yields == 2 and len(rows) == 130
    assert mm._index.get_library_rows("game_a") is rows


@pytest.mark.parametrize("change", ["sql", "rollback", "paths"])
def test_row_capture_rejects_changed_stamp_without_publishing_cache(mm, change):
    populate(mm)
    iterator = mm._index.iter_library_rows("game_a")
    next(iterator)
    if change == "paths":
        mm._index.paths.roots["game_a"] /= "moved"
    else:
        db = mm._index._db()
        db.execute("UPDATE mods SET name=name || ' changed'")
        db.rollback() if change == "rollback" else db.commit()
    with pytest.raises(RuntimeError, match="changed"):
        finish(iterator)
    assert mm._index._library_rows_cache is None


def test_request_slices_deployment_projection_and_reuses_only_complete_template(mm):
    populate(mm)
    rows = mm._index.get_library_rows("game_a")
    token = ProbeToken("game_a", 1, 1)
    iterator = mm.iter_library_probe_request(token)
    assert next(iterator) is None  # 命中缓存的记录仍须按批次让出执行权。
    assert mm._library_probe_template is None
    yields, request = finish(iterator)
    assert yields >= 3 and request.rows is rows and len(request.records) == 130
    assert request == mm.create_library_probe_request(token)
    next_token = ProbeToken("game_a", 2, 2)
    count, cached = finish(mm.iter_library_probe_request(next_token))
    assert count == 0 and cached == replace(request, token=next_token)


def test_request_rejects_manager_mutation_after_rows_were_captured(mm):
    populate(mm)
    mm._index.get_library_rows("game_a")
    iterator = mm.iter_library_probe_request(ProbeToken("game_a", 1, 1))
    next(iterator)
    mm._library_epoch += 1
    with pytest.raises(RuntimeError, match="changed"):
        finish(iterator)
    assert mm._library_probe_template is None


def test_topology_path_resolution_is_bounded_and_matches_sync_api(mm, monkeypatch):
    for number in range(5):
        mm.add_character(f"Node{number}")
    resolved = []
    original = mm._index.paths.node_path

    def node_path(identity):
        resolved.append(identity)
        return original(identity)

    monkeypatch.setattr(mm._index.paths, "node_path", node_path)
    iterator = mm.iter_library_scan_layout("game_a", batch_size=2)
    assert next(iterator) is None
    assert next(iterator) is None
    assert resolved == []
    assert next(iterator) is None
    assert len(resolved) == 2
    _, layout = finish(iterator)
    assert len(layout[1]) == 5 and len(layout[2]) == 6
    assert layout == mm.library_scan_layout("game_a")
