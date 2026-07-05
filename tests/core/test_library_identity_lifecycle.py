import sqlite3
import zipfile
from types import SimpleNamespace

import pytest

from src.core.library_schema import create_schema, register_connection
from src.core.mod_intake import IntakeRequest, ModIntakeService, intake_identity
from src.core.rebuild_session import publish_rebuild_projection

IDENTITY = "abcdefabcdef4def8defabcdefabcdef"
STAMP = "2026-10-05T10:00:00"


def test_explicit_external_identity_ignores_local_marker():
    identity, timestamp = intake_identity("external", {"mod_id": IDENTITY, "imported_at": STAMP})
    assert identity != IDENTITY
    assert timestamp != STAMP
    assert intake_identity("rediscovery", {"mod_id": IDENTITY, "imported_at": STAMP}) == (IDENTITY, STAMP)


def _databases(tmp_path):
    live = sqlite3.connect(tmp_path / "live.db")
    create_schema(live)
    live.execute(
        "INSERT INTO mods VALUES(?, 'game', NULL, 'Original', 'Container', 'body.zip', 'hash', '{}', '[]', ?)",
        (IDENTITY, STAMP),
    )
    live.execute("INSERT INTO deployment_records VALUES(?, 'active', 'D:/deploy')", (IDENTITY,))
    live.execute("INSERT INTO game_deployment_state VALUES('game','preset','stamp')")
    live.execute(
        "INSERT INTO preset_apply_journal VALUES('operation','game','before','preset','prepared','{}'"
        ",'[]','stamp','stamp','{}')"
    )
    live.commit()
    stage = sqlite3.connect(tmp_path / "stage.db")
    live.backup(stage)
    register_connection(stage)
    for table in ("mods", "deployment_records"):
        stage.execute(f"CREATE TABLE rebuild_baseline_{table} AS SELECT * FROM {table}")
    stage.execute("CREATE TABLE rebuild_candidates(mod_id TEXT PRIMARY KEY)")
    stage.execute("INSERT INTO rebuild_candidates VALUES(?)", (IDENTITY,))
    stage.execute("UPDATE mods SET name='Rebuilt' WHERE mod_id=?", (IDENTITY,))
    stage.execute("UPDATE deployment_records SET actual_path='D:/rebuilt' WHERE mod_id=?", (IDENTITY,))
    stage.commit()
    live.execute("ATTACH DATABASE ? AS rebuild_stage", (str(tmp_path / "stage.db"),))
    return live, stage


def test_rebuild_preserves_identity_timestamp_and_transaction_state(tmp_path):
    live, stage = _databases(tmp_path)
    with live:
        assert publish_rebuild_projection(live, "game") == [IDENTITY]
    assert live.execute("SELECT mod_id,name,imported_at FROM mods").fetchone() == (IDENTITY, "Rebuilt", STAMP)
    assert live.execute("SELECT current_preset_id FROM game_deployment_state").fetchone()[0] == "preset"
    assert live.execute("SELECT operation_id FROM preset_apply_journal").fetchone()[0] == "operation"
    assert live.execute("PRAGMA foreign_key_check").fetchall() == []
    stage.close()
    live.close()


def test_rebuild_keeps_newer_source_and_deployment_events(tmp_path):
    live, stage = _databases(tmp_path)
    with live:
        live.execute("UPDATE mods SET name='Newer event' WHERE mod_id=?", (IDENTITY,))
        live.execute("UPDATE deployment_records SET actual_path='D:/newer' WHERE mod_id=?", (IDENTITY,))
    with live:
        assert publish_rebuild_projection(live, "game") == []
    assert live.execute("SELECT name FROM mods").fetchone()[0] == "Newer event"
    assert live.execute("SELECT actual_path FROM deployment_records").fetchone()[0] == "D:/newer"
    stage.close()
    live.close()


def test_rediscovery_uses_same_cache_identity_and_rollback_keeps_existing_cache(tmp_path, temp_data_dir):
    from src.core.mod_package import write_container_marker
    from src.models.mod import Mod

    body = tmp_path / "library" / "Container" / "body.zip"
    body.parent.mkdir(parents=True)
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("body.ini", "[Constants]\n")
    write_container_marker(body.parent, body.name, Mod.compute_sha1(body), mod_id=IDENTITY, imported_at=STAMP)
    original = (body.parent / ".d3dxmod").read_bytes()
    cache = tmp_path / "data" / "resource" / "preview_cache" / IDENTITY
    cache.mkdir(parents=True)
    cached = cache / "existing.webp"
    cached.write_bytes(b"cached-image")

    class Manager:
        current_game_id = "game"
        _storage = SimpleNamespace(is_deployed=lambda *args, **kwargs: False)

        def __init__(self):
            self.inflight = set()

        def register_inflight_mod_id(self, identity):
            self.inflight.add(identity)

        def unregister_inflight_mod_id(self, identity):
            self.inflight.discard(identity)

        def protect_library_operation(self, token, *, paths=(), mod_ids=()):
            pass

        def release_library_operation(self, token):
            pass

        def _find_previews(self, path, *, mod_id, **kwargs):
            assert mod_id == IDENTITY
            return [str(cached)]

        def _apply_sidecar_info(self, mod, sidecar):
            pass

        def _order_previews(self, paths, sidecar):
            return paths

    manager = Manager()
    service = ModIntakeService(manager)
    request = IntakeRequest(
        body, "game", "", destination_path=body.parent.parent, context="rediscovery", resource_dir="resource"
    )
    prepared = service.prepare(request)
    assert (prepared.mod.mod_id, prepared.mod.imported_at) == (IDENTITY, STAMP)
    assert prepared.preview_cache_path == cache
    service.rollback(prepared)
    assert not manager.inflight
    assert cached.read_bytes() == b"cached-image"
    assert (body.parent / ".d3dxmod").read_bytes() == original


def test_deployment_only_newer_event_survives_source_rebuild(tmp_path):
    live, stage = _databases(tmp_path)
    with live:
        live.execute("UPDATE deployment_records SET actual_path='D:/newer' WHERE mod_id=?", (IDENTITY,))
    with live:
        publish_rebuild_projection(live, "game")
    assert live.execute("SELECT name FROM mods").fetchone()[0] == "Rebuilt"
    assert live.execute("SELECT actual_path FROM deployment_records").fetchone()[0] == "D:/newer"
    stage.close()
    live.close()


def test_failed_rebuild_publication_rolls_back_source_and_state(tmp_path):
    live, stage = _databases(tmp_path)
    live.execute(
        "CREATE TRIGGER fail_deployment BEFORE UPDATE ON deployment_records BEGIN SELECT RAISE(ABORT,'locked'); END"
    )
    live.commit()
    with pytest.raises(sqlite3.IntegrityError, match="locked"):
        with live:
            publish_rebuild_projection(live, "game")
    assert live.execute("SELECT name,imported_at FROM mods").fetchone() == ("Original", STAMP)
    assert live.execute("SELECT actual_path FROM deployment_records").fetchone()[0] == "D:/deploy"
    assert live.execute("SELECT current_preset_id FROM game_deployment_state").fetchone()[0] == "preset"
    stage.close()
    live.close()


def test_rebuild_does_not_resurrect_source_removed_after_baseline(tmp_path):
    live, stage = _databases(tmp_path)
    with live:
        live.execute("DELETE FROM deployment_records WHERE mod_id=?", (IDENTITY,))
        live.execute("DELETE FROM mods WHERE mod_id=?", (IDENTITY,))
    with live:
        assert publish_rebuild_projection(live, "game") == []
    assert live.execute("SELECT COUNT(*) FROM mods").fetchone()[0] == 0
    stage.close()
    live.close()
