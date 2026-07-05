import pytest

from tests.core.test_library_reconcile import _import_mod
from tests.core.test_library_reconcile import mm as mm


def test_intake_batch_reuses_lightweight_hash_and_identity_snapshot(mm, tmp_path, monkeypatch):
    from src.core.mod_intake import ModIntakeService

    char = mm.add_character(name="Planning", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "existing")
    reads = []
    original = mm._index.get_mod_ids

    def identities():
        reads.append("identities")
        return original()

    monkeypatch.setattr(mm._index, "get_mod_ids", identities)
    monkeypatch.setattr(mm._index, "get_all_mods", lambda *a: pytest.fail("full Mod materialization"))
    statements = []
    mm._index._db().set_trace_callback(statements.append)
    service = ModIntakeService(mm)
    snapshot = service.planning_snapshot("game_a")
    requests = [
        service.plan_request(
            source_path=tmp_path / str(i), game_id="game_a", node_id=char.id, planning_snapshot=snapshot
        )
        for i in range(3)
    ]
    assert reads == ["identities"]
    assert sum("SELECT DISTINCT content_hash" in query for query in statements) == 1
    assert all(request.existing_hashes is snapshot.existing_hashes for request in requests)
    assert mod.content_hash in snapshot.existing_hashes
    assert mod.mod_id in snapshot.existing_mod_ids
    mm._index._db().set_trace_callback(None)


def test_changed_index_rejects_old_planning_snapshot(mm, tmp_path):
    from src.core.mod_intake import IntakeError, ModIntakeService

    char = mm.add_character(name="Planning", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "existing")
    service = ModIntakeService(mm)
    snapshot = service.planning_snapshot("game_a")
    mod.name = "Edited"
    mm._index.update_mod(mod)
    with pytest.raises(IntakeError, match="no longer current"):
        service.plan_request(
            source_path=tmp_path / "incoming", game_id="game_a", node_id=char.id, planning_snapshot=snapshot
        )


@pytest.mark.parametrize("commit_after", ["hashes", "identities"])
def test_external_commit_between_snapshot_reads_is_rejected(mm, tmp_path, monkeypatch, commit_after):
    import sqlite3

    from src.core.mod_intake import IntakeError, ModIntakeService

    char = mm.add_character(name="Concurrent", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "existing")
    index = mm._index
    connection = index._db()
    other = sqlite3.connect(index._db_path)
    from src.core.library_schema import register_connection

    register_connection(other)
    committed = []

    def commit():
        other.execute("UPDATE mods SET content_hash=? WHERE mod_id=?", ("changed-hash", mod.mod_id))
        other.commit()
        committed.append(True)

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            rows = self.cursor.fetchall()
            commit()
            return rows

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    class Connection:
        def execute(self, sql, *args):
            cursor = connection.execute(sql, *args)
            if commit_after == "hashes" and "SELECT DISTINCT content_hash" in sql:
                return Cursor(cursor)
            return cursor

        def __getattr__(self, name):
            return getattr(connection, name)

    proxy = Connection()
    monkeypatch.setattr(index, "_db", lambda: proxy)
    original = index.get_mod_ids

    def identities():
        values = original()
        if commit_after == "identities":
            commit()
        return values

    monkeypatch.setattr(index, "get_mod_ids", identities)
    try:
        with pytest.raises(IntakeError, match="changed during planning"):
            ModIntakeService(mm).planning_snapshot("game_a")
        assert committed == [True]
    finally:
        other.close()


def test_uncommitted_snapshot_cannot_outlive_rollback(mm, tmp_path):
    from src.core.mod_intake import IntakeError, ModIntakeService

    char = mm.add_character(name="Transaction", node_type="character")
    mod = _import_mod(mm, char.id, tmp_path, "existing")
    service = ModIntakeService(mm)
    connection = mm._index._db()
    connection.execute("BEGIN")
    connection.execute("UPDATE mods SET content_hash=? WHERE mod_id=?", ("rolled-back-hash", mod.mod_id))
    try:
        with pytest.raises(IntakeError, match="transaction"):
            service.planning_snapshot("game_a")
    finally:
        connection.rollback()
    assert service.planning_snapshot("game_a").existing_hashes == frozenset({mod.content_hash})


def test_planned_snapshot_cannot_be_reused_in_transaction(mm, tmp_path):
    from src.core.mod_intake import IntakeError, ModIntakeService

    char = mm.add_character(name="Transaction", node_type="character")
    service = ModIntakeService(mm)
    snapshot = service.planning_snapshot("game_a")
    connection = mm._index._db()
    connection.execute("BEGIN")
    try:
        with pytest.raises(IntakeError, match="transaction"):
            service.plan_request(
                source_path=tmp_path / "incoming", game_id="game_a", node_id=char.id, planning_snapshot=snapshot
            )
    finally:
        connection.rollback()
