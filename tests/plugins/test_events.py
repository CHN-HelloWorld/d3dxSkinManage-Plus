from __future__ import annotations

import pytest

from src.core.mod_manager import ModManager
from tests.helpers import register_games, reset_game_service


@pytest.fixture
def manager(temp_data_dir):
    register_games("events-a", "events-b")
    mm = ModManager()
    mm.set_game("events-a")
    yield mm
    mm.stop_ini_sync()
    reset_game_service()


def listen(manager):
    events = []
    manager.add_event_listener(lambda event, **payload: events.append((event, payload)))
    return events


def add_mod(manager, mid, tags=()):
    root = manager._index.paths.roots[manager.current_game_id]
    source = root.parent / ("source-" + mid)
    source.mkdir(parents=True)
    (source / "mod.ini").write_text(f"[Constants]\nx={mid}\n", encoding="utf-8")
    node = manager.add_character(name=mid, node_type="character")
    mod = manager.import_mod(source_path=str(source), node_id=node.id, name=mid, import_format="zip")
    assert mod is not None
    mod.info.tags = list(tags)
    assert manager.update_mod(mod)
    return manager.get_mod(mod.mod_id)


def test_public_events_accept_new_host_events():
    from src.plugin_sdk.events import EventSubscription
    from src.plugins.runtime.eventbus import EventBus

    subscription = EventSubscription("test", EventBus())
    for event in (
        "mod_metadata_changed",
        "tags_changed",
        "mod_updated",
        "index_rebuilt",
        "variant_restored",
        "variant_state_changed",
        "variant_references_missing",
    ):
        subscription.on(event, lambda **payload: None)


def test_listener_can_be_removed_idempotently(manager):
    seen = []

    def listener(event, **payload):
        seen.append(event)

    manager.add_event_listener(listener)
    manager.remove_event_listener(listener)
    manager.remove_event_listener(listener)
    manager._emit("game_changed", game_id="events-a")
    assert seen == []


def test_metadata_event_uses_owning_game_and_follows_commit(manager, monkeypatch):
    mod = add_mod(manager, "one")
    events = listen(manager)
    committed = []
    manager.add_event_listener(
        lambda event, **payload: (
            committed.append(manager.get_mod(payload["mod_id"]).info.author)
            if event == "mod_metadata_changed"
            else None
        )
    )
    mod.info.author = "updated"
    assert manager.update_mod(mod)
    assert events == [("mod_metadata_changed", {"mod_id": mod.mod_id, "game_id": "events-a"})]
    assert committed == ["updated"]
    assert manager.get_mod(mod.mod_id).info.author == "updated"
    events.clear()
    monkeypatch.setattr(manager._index, "update_mod", lambda mod: False)
    assert not manager.update_mod(mod)
    assert events == []


def test_shared_tag_changes_report_every_affected_mod_once(manager, monkeypatch):
    first = add_mod(manager, "one", ["shared"])
    second = add_mod(manager, "two", ["shared"])
    draft = manager.get_tag_service().begin_edit(first)
    draft.rename(draft.selected_ids[0], "renamed")
    events = listen(manager)
    manager.commit_tag_edit(draft, {"author": "changed"})
    assert len(events) == 3
    assert {payload["mod_id"] for event, payload in events[:2]} == {first.mod_id, second.mod_id}
    assert all(event == "mod_metadata_changed" and payload["game_id"] == "events-a" for event, payload in events[:2])
    assert events[-1][0] == "tags_changed"
    assert events[-1][1]["game_id"] == "events-a"
    assert set(events[-1][1]["affected_ids"]) == {first.mod_id, second.mod_id}
    events.clear()
    clean = manager.get_tag_service().begin_edit(manager.get_mod(first.mod_id))
    manager.commit_tag_edit(clean, {})
    assert events == []


def test_tag_color_only_refreshes_catalog_without_metadata_event(manager, monkeypatch):
    mod = add_mod(manager, "one", ["shared"])
    draft = manager.get_tag_service().begin_edit(mod)
    draft.set_color(draft.selected_ids[0], "#123ABC")
    events = listen(manager)
    manager.commit_tag_edit(draft, {})
    assert events == [("tags_changed", {"affected_ids": [mod.mod_id], "game_id": "events-a"})]


def import_enabled(manager, tmp_path, name):
    from src.core.config import Config

    Config().set("import.format", "zip")
    source = tmp_path / name
    source.mkdir()
    (source / "mod.ini").write_text(f"[Constants]\nx={name}\n", encoding="utf-8")
    char = manager.add_character(name=name, node_type="character")
    mod = manager.import_mod(source_path=str(source), node_id=char.id)
    assert mod is not None
    assert manager.toggle_mod(mod.mod_id).is_enabled
    return manager.get_mod(mod.mod_id)


def test_disable_all_preserves_failed_refs_and_updates_deployment_records(manager, tmp_path, monkeypatch):
    manager.set_deploy_dir(str(tmp_path / "deploy-a"))
    first = import_enabled(manager, tmp_path, "first")
    second = import_enabled(manager, tmp_path, "second")
    undeploy = manager._storage.undeploy_mod
    monkeypatch.setattr(
        manager._storage,
        "undeploy_mod",
        lambda *a, **kw: False if kw.get("mod_id") == second.mod_id else undeploy(*a, **kw),
    )
    events = listen(manager)
    assert manager.disable_all_mods() == 1
    assert not manager.get_mod(first.mod_id).is_enabled
    assert manager.get_mod(second.mod_id).is_enabled
    assert manager._index.get_deployment(first.mod_id).state.value == "absent"
    assert manager._index.get_deployment(second.mod_id).state.value == "active"
    assert second.mod_id in manager.preset_manager.get_current("events-a").enabled_mod_ids
    assert events == [("mod_disabled", {"mod_id": first.mod_id, "game_id": "events-a"})]


def test_disable_all_does_not_touch_another_game(manager, tmp_path):
    manager.set_deploy_dir(str(tmp_path / "deploy-a"))
    first = import_enabled(manager, tmp_path, "first")
    manager.set_game("events-b")
    manager.set_deploy_dir(str(tmp_path / "deploy-b"))
    second = import_enabled(manager, tmp_path, "second")
    assert manager.disable_all_mods() == 1
    assert manager.get_mod(first.mod_id).is_enabled
    assert not manager.get_mod(second.mod_id).is_enabled


def test_disable_all_parks_deployment_referenced_by_another_preset(manager, tmp_path):
    from pathlib import Path

    manager.set_deploy_dir(str(tmp_path / "deploy-a"))
    mod = import_enabled(manager, tmp_path, "source")
    saved = manager.preset_manager.create_preset("events-a", "saved", {mod.node_id: [mod.mod_id]})
    assert manager.disable_all_mods() == 1
    record = manager._index.get_deployment(mod.mod_id)
    assert record.state.value == "disabled"
    assert Path(record.actual_path).is_dir()
    assert mod.mod_id in saved.enabled_mod_ids


def test_managed_container_rename_publishes_metadata_change(manager, tmp_path):
    manager.set_deploy_dir(str(tmp_path / "deploy-a"))
    mod = import_enabled(manager, tmp_path, "source")
    manager.disable_all_mods()
    events = listen(manager)
    renamed = manager.rename_mod(mod.mod_id, "renamed")
    assert renamed is not None
    assert events == [("mod_metadata_changed", {"mod_id": mod.mod_id, "game_id": "events-a"})]


def test_rebuild_publishes_after_commit_without_changing_mod_identity(manager, tmp_path):
    manager.set_deploy_dir(str(tmp_path / "deploy-a"))
    old = import_enabled(manager, tmp_path, "source")
    events = listen(manager)
    manager.rebuild_index_from_filesystem("events-a")
    new = manager._index.get_mod(old.mod_id)
    assert new.mod_id == old.mod_id
    assert new.imported_at == old.imported_at
    assert events == [("index_rebuilt", {"game_id": "events-a"})]


def test_applied_update_publishes_game_from_index(manager):
    from types import SimpleNamespace

    from src.core.mod_update_service import ModUpdateService

    mod = add_mod(manager, "one")
    manager.set_game("events-b")
    events = listen(manager)
    service = SimpleNamespace(
        _mm=manager,
        _status={},
        _failures={},
        _preserve_user_change=lambda mid: None,
        _save_status=lambda: None,
        update_applied=SimpleNamespace(emit=lambda *args: None),
        status_changed=SimpleNamespace(emit=lambda: None),
    )
    ModUpdateService._on_task_applied(service, mod.mod_id, mod)
    assert events == [("mod_updated", {"mod_id": mod.mod_id, "game_id": "events-a"})]
