import json
import threading
import zipfile
from types import SimpleNamespace

import pytest

from src.core.game_config_store import GameConfigStore
from src.core.mod_index import ModIndex
from src.core.mod_manager import ModManager
from src.models.mod import Mod


@pytest.fixture
def manager(temp_data_dir):
    GameConfigStore.instance().set_games(
        [{"id": gid, "name": gid, "name_en": gid, "mods_path": "", "resource_dir": gid} for gid in ("g", "other")]
    )
    index = ModIndex("g")
    index.paths.roots["g"].mkdir(parents=True)
    index.paths.roots["other"].mkdir(parents=True)
    value = ModManager.__new__(ModManager)
    value._index = index
    value._game_service = SimpleNamespace(current_game_id="g")
    value._library_epoch_lock = threading.RLock()
    value._library_epoch = 0
    value._library_mutations = 0
    value._ini_sync = None
    value._ini_sync_refs = {}
    value._library_probe_events = None
    value._event_listeners = []
    value._operation_trusted = False
    yield value
    index.close()


def setup_tree(manager):
    from src.core.mod_identity import new_mod_id
    from src.core.mod_package import build_local_manifest, local_meta_from_mod, write_container_marker, write_sidecar

    a = manager.add_character("A")
    b = manager.add_character("B")
    child = manager.add_character("Child", parent_id=a.id)
    root = manager._index.paths.node_path(child.id)
    box = root / "Box"
    box.mkdir()
    with zipfile.ZipFile(box / "body.zip", "w") as archive:
        archive.writestr("mod.ini", "[Constants]\nx=1\n")
    (box / "preview.png").write_bytes(b"image")
    thumbnail = root / "thumb.png"
    thumbnail.write_bytes(b"thumbnail")
    item = Mod(
        mod_id=new_mod_id(),
        game_id="g",
        node_id=child.id,
        folder_name="Box",
        name="Box",
        path=str(box / "body.zip"),
        preview_images=[str(box / "preview.png")],
        content_hash=Mod.compute_sha1(box / "body.zip"),
        imported_at="2026-10-06T00:00:00",
    )
    write_container_marker(box, "body.zip", item.content_hash, mod_id=item.mod_id, imported_at=item.imported_at)
    assert write_sidecar(item.path, build_local_manifest(local_meta_from_mod(item)))
    assert manager._index.add_mod(item)
    assert manager.update_character_thumbnail(child.id, str(thumbnail))
    return a, b, child, item


@pytest.mark.parametrize("operation", ["move", "rename"])
def test_node_operations_update_only_own_row_and_resolve_images(manager, operation):
    a, b, child, item = setup_tree(manager)
    statements = []
    manager._index._db().set_trace_callback(statements.append)
    if operation == "move":
        assert manager.move_character(a.id, b.id)
        expected = manager._index.paths.roots["g"] / "B/A/Child"
    else:
        assert manager.update_character(a.id, "Renamed")
        expected = manager._index.paths.roots["g"] / "Renamed/Child"
    assert manager.get_character(child.id).thumbnail == str(expected / "thumb.png")
    loaded = manager._index.get_mod(item.mod_id)
    assert loaded.path == str(expected / "Box/body.zip")
    assert loaded.preview_images == [str(expected / "Box/preview.png")]
    assert not any(s.upper().startswith("UPDATE MODS") for s in statements)
    assert not any(s.upper().startswith("UPDATE NODES") and child.id in s for s in statements)
    marker = json.loads((expected / ".d3dxcategory").read_text())
    assert marker["thumbnail"] == {"scope": "node", "path": "thumb.png"}


@pytest.mark.parametrize("reason", ["occupied", "cycle", "cross_game", "rename_occupied"])
def test_node_operation_rejects_conflict_without_moving_data(manager, reason):
    a, b, child, item = setup_tree(manager)
    old = manager._index.paths.node_path(a.id)
    if reason == "occupied":
        target = manager._index.paths.node_path(b.id) / "A"
        target.mkdir()
        (target / "keep.txt").write_text("keep")
        assert not manager.move_character(a.id, b.id)
        assert (target / "keep.txt").read_text() == "keep"
    elif reason == "cycle":
        assert not manager.move_character(a.id, child.id)
    elif reason == "cross_game":
        foreign = manager.add_character("Foreign", game_id="other")
        assert not manager.move_character(a.id, foreign.id)
    else:
        assert not manager.update_character(a.id, "B")
    assert old.is_dir()
    assert manager._index.paths.node_path(a.id) == old
    assert manager._index.get_mod(item.mod_id).path == item.path


def test_rename_marker_failure_restores_directory_and_database(manager, monkeypatch):
    a, b, child, item = setup_tree(manager)
    old = manager._index.paths.node_path(a.id)
    marker = (old / ".d3dxcategory").read_bytes()
    monkeypatch.setattr(manager, "_ensure_category_marker", lambda *args: False)
    assert not manager.update_character(a.id, "Broken")
    assert manager._index.paths.node_path(a.id) == old
    assert (old / ".d3dxcategory").read_bytes() == marker
    assert not (old.parent / "Broken").exists()


def test_get_character_is_local_and_counts_use_deployment_join(manager, monkeypatch):
    a, b, child, item = setup_tree(manager)

    def forbidden(*args):
        raise AssertionError("local get_character enumerated every node")

    monkeypatch.setattr(manager, "get_characters", forbidden)
    node = manager.get_character(child.id)
    assert node.mod_count == 1 and node.enabled_count == 0
    manager._index._db().execute("INSERT INTO deployment_records VALUES(?,'active','target')", (item.mod_id,))
    manager._index._db().commit()
    assert manager.get_character(child.id).enabled_count == 1


def test_resource_directory_move_does_not_update_projection_rows(manager):
    a, b, child, item = setup_tree(manager)
    statements = []
    manager._index._db().set_trace_callback(statements.append)
    assert manager.migrate_game_resource_dir("g", "g", "RenamedGame") == "RenamedGame"
    assert not any(s.upper().startswith("UPDATE") for s in statements)
    assert "RenamedGame" in manager._index.get_mod(item.mod_id).path


@pytest.mark.parametrize("repair_fails", [False, True])
def test_node_move_repairs_only_affected_active_deployment(manager, monkeypatch, repair_fails):
    from src.core.mod_identity import write_deployment_identity

    a, b, child, item = setup_tree(manager)
    target = manager._index.paths.roots["g"].parent / "deployed"
    target.mkdir()
    write_deployment_identity(target, item.mod_id, item.content_hash)
    manager._index._db().execute("INSERT INTO deployment_records VALUES(?,'active',?)", (item.mod_id, str(target)))
    manager._index._db().commit()
    calls = []

    def check(path, identity):
        calls.append(identity)
        if repair_fails:
            raise PermissionError("deployment locked")

    manager._storage = SimpleNamespace(_check_deployment_identity=check)
    monkeypatch.setattr("src.core.mod_package.list_overrides", lambda body: [])
    monkeypatch.setattr(manager, "_register_ini_sync", lambda *args, **kwargs: calls.append(args[0]))
    result = manager.move_character(a.id, b.id)
    assert result is (not repair_fails)
    assert calls == [item.mod_id] * (1 if repair_fails else 2)
    record = manager._index.get_deployment(item.mod_id)
    assert record.actual_path == str(target)
    assert record.state.value == ("error" if repair_fails else "active")
    assert manager._index.paths.node_path(a.id) == manager._index.paths.node_path(b.id) / "A"
