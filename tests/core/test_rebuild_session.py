from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.core.test_rebuild_index import _make_deployable_mod
from tests.core.test_rebuild_index import mm as rebuild_mm
from tests.helpers import registered_games as registered_games

mm = rebuild_mm


def test_close_releases_session_when_repaired_marker_was_deleted(mm, tmp_path):
    from src.core.rebuild_session import RebuildSession

    session = RebuildSession(mm, "game_a")
    session._repaired_markers.append((tmp_path / "deleted-marker", b"old", b"written"))
    mm.register_inflight_mod_id("unpublished")
    mm._rebuild_inflight_ids.add("unpublished")
    session.close()
    assert mm._active_rebuild_session is None
    assert "unpublished" not in mm.inflight_mod_ids()


def test_publication_rechecks_pending_journal_after_write_lock(mm, monkeypatch):
    session = session_with_mod(mm)
    try:
        while not session.stage_batch():
            pass
        session.validate()

        def pending(_game):
            return mm._index._db().in_transaction

        monkeypatch.setattr(mm._preset_manager, "has_pending_apply", pending)
        with pytest.raises(RuntimeError, match="预设事务"):
            session.commit()
        assert not mm.get_mods()
    finally:
        session.close()


def session_with_mod(mm):
    from src.core.rebuild_session import RebuildSession

    _make_deployable_mod(mm._get_library_root("game_a"), "Example")
    session = RebuildSession(mm, "game_a")
    with ThreadPoolExecutor(1) as pool:
        pool.submit(session.prepare).result()
    return session


def test_background_plan_and_staging_do_not_change_live_index(mm):
    session = session_with_mod(mm)
    try:
        assert not mm.get_mods()
        while not session.stage_batch(1):
            assert not mm._index._db().in_transaction
            assert not mm.get_mods()
        assert not mm.get_mods()
        assert session.commit().registered == 1
        assert len(mm.get_mods()) == 1
    finally:
        session.close()


def test_private_stage_backup_finishes_before_owner_staging(mm, monkeypatch):
    from src.core.rebuild_session import RebuildSession

    _make_deployable_mod(mm._get_library_root("game_a"), "Example")
    session = RebuildSession(mm, "game_a")
    live = mm._index._db()

    class LiveConnection:
        def backup(self, *args, **kwargs):
            raise AssertionError("staging must not back up the live owner connection")

        def __getattr__(self, name):
            return getattr(live, name)

    original_db = type(mm._index)._db

    def database(index):
        return LiveConnection() if index is mm._index else original_db(index)

    monkeypatch.setattr(type(mm._index), "_db", database)
    try:
        with ThreadPoolExecutor(1) as pool:
            pool.submit(session.prepare).result()
        assert session.stage is None
        while not session.stage_batch():
            pass
        assert session.stage.get_all_mods("game_a")
        assert not mm.get_mods()
    finally:
        session.close()


def test_stage_connections_are_readonly_and_closed_before_owner_reopens(mm, monkeypatch):
    import sqlite3
    import threading

    from src.core.rebuild_session import RebuildSession

    _make_deployable_mod(mm._get_library_root("game_a"), "Example")
    session = RebuildSession(mm, "game_a")
    owner = threading.get_ident()
    opened, closed, backups = {}, set(), []
    connect = sqlite3.connect

    class Connection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            opened[id(self)] = threading.get_ident()

        def backup(self, target, **kwargs):
            thread = threading.get_ident()
            assert opened[id(self)] == opened[id(target)] == thread
            assert thread != owner
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                self.execute("CREATE TABLE forbidden_stage_write (value INTEGER)")
            backups.append((id(self), id(target)))
            return super().backup(target, **kwargs)

        def close(self):
            assert opened[id(self)] == threading.get_ident()
            closed.add(id(self))
            super().close()

    def tracked_connect(*args, **kwargs):
        return connect(*args, factory=Connection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    try:
        with ThreadPoolExecutor(1) as pool:
            pool.submit(session.prepare).result()
        assert len(backups) == 1
        assert set(backups[0]) <= closed
        while not session.stage_batch():
            pass
        assert opened[id(session.stage._db())] == owner
        assert session.commit().registered == 1
    finally:
        session.close()


def test_cancel_during_stage_backup_keeps_live_index_and_closes_files(mm, monkeypatch):
    import sqlite3

    from src.core.rebuild_session import RebuildSession

    session = RebuildSession(mm, "game_a")
    connect = sqlite3.connect

    class Connection(sqlite3.Connection):
        def backup(self, target, **kwargs):
            callback = kwargs["progress"]

            def cancel(*args):
                session.cancel.set()
                callback(*args)

            return super().backup(target, **{**kwargs, "progress": cancel})

    def tracked_connect(*args, **kwargs):
        return connect(*args, factory=Connection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    try:
        with ThreadPoolExecutor(1) as pool:
            with pytest.raises(InterruptedError):
                pool.submit(session.prepare).result()
        stage_path = session.stage_path
        assert not session._stage_ready
        assert session.stage is None
        assert not mm.get_mods()
    finally:
        session.close()
    assert not stage_path.parent.exists()


def test_cancel_during_private_stage_clear_keeps_live_rows(mm, monkeypatch):
    import sqlite3

    from src.core.rebuild_session import RebuildSession

    _make_deployable_mod(mm._get_library_root("game_a"), "Keep")
    mm.rebuild_index_from_filesystem("game_a")
    before = [tuple(row) for row in mm._index._db().execute("SELECT * FROM mods")]
    session = RebuildSession(mm, "game_a")
    connect = sqlite3.connect
    clearing = []

    class Connection(sqlite3.Connection):
        in_clear = False

        def execute(self, sql, *args):
            self.in_clear = sql.startswith("CREATE TABLE rebuild_baseline_")
            try:
                return super().execute(sql, *args)
            finally:
                self.in_clear = False

        def set_progress_handler(self, callback, instructions):
            def cancel():
                if self.in_clear:
                    clearing.append(True)
                    session.cancel.set()
                return callback()

            super().set_progress_handler(cancel, 1)

    def tracked_connect(*args, **kwargs):
        return connect(*args, factory=Connection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    try:
        with ThreadPoolExecutor(1) as pool:
            with pytest.raises(InterruptedError):
                pool.submit(session.prepare).result()
        assert clearing
        assert not session._stage_ready
        assert [tuple(row) for row in mm._index._db().execute("SELECT * FROM mods")] == before
    finally:
        session.close()


def test_cancel_keeps_original_rows_and_releases_inflight(mm):
    session = session_with_mod(mm)
    session.cancel.set()
    with pytest.raises(InterruptedError):
        session.stage_batch()
    session.close()
    assert not mm.get_mods()
    assert not mm._rebuild_inflight_ids


def test_change_during_staging_rejects_commit(mm):
    session = session_with_mod(mm)
    try:
        while not session.stage_batch():
            pass
        mm._index._db().execute("UPDATE nodes SET name=name")
        mm._index._db().commit()
        # 其他游戏的真实数据库变更同样使快照失效。
        mm._index._db().execute("INSERT INTO game_deployment_state VALUES ('other','','')")
        mm._index._db().commit()
        session.commit()
        assert len(mm.get_mods()) == 1
        assert mm._index._db().execute("SELECT 1 FROM game_deployment_state WHERE game_id='other'").fetchone()
    finally:
        session.close()


def test_final_validation_rejects_changed_sidecar(mm):
    import json

    session = session_with_mod(mm)
    try:
        while not session.stage_batch():
            pass
        path = session.root / "Example" / ".d3dxmeta" / "info.json"
        data = json.loads(path.read_text(encoding="utf8"))
        data["name"] = "Changed"
        path.write_text(json.dumps(data), encoding="utf8")
        with ThreadPoolExecutor(1) as pool:
            pool.submit(session.validate).result()
        session.commit()
        assert not mm.get_mods()
    finally:
        session.close()


def test_publish_failure_rolls_back_and_tags_survive(mm):
    import json
    import sqlite3

    body = _make_deployable_mod(mm._get_library_root("game_a"), "Tagged")
    path = body.parent / ".d3dxmeta" / "info.json"
    data = json.loads(path.read_text(encoding="utf8"))
    data["info"]["tags"] = ["Blue", "Red"]
    path.write_text(json.dumps(data), encoding="utf8")
    mm.rebuild_index_from_filesystem("game_a")
    original = mm.get_mods()[0]
    from src.core.rebuild_session import RebuildSession

    session = RebuildSession(mm, "game_a")
    try:
        with ThreadPoolExecutor(1) as pool:
            pool.submit(session.prepare).result()
        while not session.stage_batch():
            pass
        db = mm._index._db()
        session.stage._db().execute("UPDATE mods SET name=name || ' changed'")
        session.stage._db().commit()
        db.execute("CREATE TEMP TRIGGER reject_rebuild BEFORE INSERT ON mods BEGIN SELECT RAISE(ABORT,'injected'); END")
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            session.commit()
        assert mm.get_mods()[0].mod_id == original.mod_id
        assert mm.get_mods()[0].info.tags == ["Blue", "Red"]
        assert not db.in_transaction
        db.execute("DROP TRIGGER reject_rebuild")
        session.close()
        session = RebuildSession(mm, "game_a")
        with ThreadPoolExecutor(1) as pool:
            pool.submit(session.prepare).result()
        while not session.stage_batch():
            pass
        assert session.commit().registered == 1
        assert mm.get_mods()[0].info.tags == ["Blue", "Red"]
    finally:
        session.close()


def test_rebuild_preserves_other_games_and_their_tag_relations(mm):

    _make_deployable_mod(mm._get_library_root("game_a"), "OtherGame")
    from tests.helpers import make_mod

    other = make_mod(mm._index, "Other", game_id="other")
    other.info.tags = ["OtherTag"]
    mm._index.game_id = "other"
    assert mm._index.add_mod(other)
    mm._index.game_id = "game_a"
    db = mm._index._db()
    before = tuple(db.execute('SELECT * FROM mods WHERE game_id="other"').fetchone())
    tags = [tuple(row) for row in db.execute("SELECT * FROM mod_tags WHERE mod_id=?", (other.mod_id,))]
    session = session_with_mod(mm)
    try:
        while not session.stage_batch():
            pass
        session.commit()
        assert tuple(db.execute('SELECT * FROM mods WHERE game_id="other"').fetchone()) == before
        assert [tuple(row) for row in db.execute("SELECT * FROM mod_tags WHERE mod_id=?", (other.mod_id,))] == tags
    finally:
        session.close()


def test_external_sqlite_write_invalidates_staged_rebuild(mm):
    import sqlite3

    session = session_with_mod(mm)
    try:
        while not session.stage_batch():
            pass
        with sqlite3.connect(mm._index._db_path) as other:
            other.execute("INSERT INTO game_deployment_state VALUES ('other','','')")
        session.commit()
        assert len(mm.get_mods()) == 1
        assert mm._index._db().execute("SELECT 1 FROM game_deployment_state WHERE game_id='other'").fetchone()
    finally:
        session.close()


def test_invalid_description_sidecar_degrades_like_synchronous_rebuild(mm):
    from src.core.rebuild_session import RebuildSession

    body = _make_deployable_mod(mm._get_library_root("game_a"), "BrokenDescription")
    (body.parent / ".d3dxmeta" / "info.json").write_text("{broken", encoding="utf8")
    session = RebuildSession(mm, "game_a")
    try:
        with ThreadPoolExecutor(1) as pool:
            pool.submit(session.prepare).result()
        while not session.stage_batch():
            pass
        with ThreadPoolExecutor(1) as pool:
            pool.submit(session.validate).result()
        assert session.commit().registered == 1
        assert mm.get_mods()[0].name == "BrokenDescription"
    finally:
        session.close()


def test_overlapping_session_cannot_release_active_cache_ids(mm):
    from src.core.rebuild_session import RebuildSession

    session = session_with_mod(mm)
    protected = set(mm._rebuild_inflight_ids)
    try:
        with pytest.raises(RuntimeError, match="正在进行"):
            RebuildSession(mm, "game_a")
        assert mm._rebuild_inflight_ids == protected
        with pytest.raises(RuntimeError, match="正在进行"):
            mm.rebuild_index_from_filesystem("game_a")
        assert mm._rebuild_inflight_ids == protected
    finally:
        session.close()
    later = RebuildSession(mm, "game_a")
    # 过期关闭操作不能释放当前所有者的保护。
    mm.register_inflight_mod_id("probe")
    mm._rebuild_inflight_ids.add("probe")
    session.close()
    assert "probe" in mm._rebuild_inflight_ids
    later.close()


def test_rebuild_refuses_preexisting_physical_mutation(mm):
    from src.core.rebuild_session import RebuildSession

    mm._storage._library_mutations += 1
    try:
        with pytest.raises(RuntimeError, match="正在进行"):
            RebuildSession(mm, "game_a")
        assert mm._active_rebuild_session is None
    finally:
        mm._storage._library_mutations -= 1


def test_deferred_sync_runs_original_cold_reconciliation_in_worker(mm, tmp_path):
    import os
    import time
    from pathlib import Path

    from src.core.mod_package import override_path_for
    from src.core.rebuild_session import RebuildSession

    body = _make_deployable_mod(mm._get_library_root("game_a"), "Enabled")
    mm.rebuild_index_from_filesystem("game_a")
    mm.set_deploy_dir(str(tmp_path / "deploy"))
    old = mm.get_mods()[0]
    assert mm.toggle_mod(old.mod_id)
    deployed = mm.get_mods()[0]
    mm.stop_ini_sync()
    ini = next(Path(deployed.deployed_path).rglob("*.ini"))
    relative = ini.relative_to(deployed.deployed_path).as_posix()
    override = override_path_for(body, relative)
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("[Constants]\nglobal $changed = 1\n", encoding="utf8")
    ini.unlink()
    ini.write_text("[Constants]\nglobal $changed = 2\n", encoding="utf8")
    os.utime(ini, (time.time() + 2, time.time() + 2))
    session = RebuildSession(mm, "game_a")
    try:
        with ThreadPoolExecutor(1) as pool:
            pool.submit(session.prepare).result()
        while not session.stage_batch():
            pass
        report = session.commit(defer_sync=True)
        assert report.registered == 1
        with ThreadPoolExecutor(1) as pool:
            pool.submit(session.sync_work.run).result()
        session.finish()
        assert override.read_bytes() == ini.read_bytes()
        assert mm._ini_sync.snapshot_mod(deployed.mod_id)
        assert mm._ini_sync_refs[deployed.mod_id] == (mm.get_mods()[0].mod_id,)
    finally:
        session.close()
        mm.stop_ini_sync()


def test_publication_preserves_tag_preferences_and_advances_last_used(mm):
    import json

    from src.core.rebuild_session import RebuildSession

    body = _make_deployable_mod(mm._get_library_root("game_a"), "Tagged")
    path = body.parent / ".d3dxmeta" / "info.json"
    data = json.loads(path.read_text(encoding="utf8"))
    data["info"]["tags"] = ["Blue"]
    path.write_text(json.dumps(data), encoding="utf8")
    mm.rebuild_index_from_filesystem("game_a")
    db = mm._index._db()
    db.execute("UPDATE tag_definitions SET name='BLUE',color='red',last_used_at='2000-01-01' WHERE game_id='game_a'")
    db.commit()
    session = RebuildSession(mm, "game_a")
    try:
        with ThreadPoolExecutor(1) as pool:
            pool.submit(session.prepare).result()
        while not session.stage_batch():
            pass
        expected = tuple(
            session.stage._db()
            .execute("SELECT id,name,color,last_used_at FROM tag_definitions WHERE game_id='game_a'")
            .fetchone()
        )
        assert expected[3] == "2000-01-01"
        session.commit()
        actual = tuple(
            db.execute("SELECT id,name,color,last_used_at FROM tag_definitions WHERE game_id='game_a'").fetchone()
        )
        assert actual == expected
        assert actual[1:3] == ("BLUE", "red")
    finally:
        session.close()
