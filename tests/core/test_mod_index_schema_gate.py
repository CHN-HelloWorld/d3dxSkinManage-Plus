import sqlite3

import pytest

from src.core.mod_index import CURRENT_TABLES, DB_SCHEMA_VERSION, ModIndex
from tests.helpers import make_mod
from tests.helpers import registered_games as registered_games


def _current():
    index = ModIndex("game_a")
    mod = make_mod(index)
    assert index.add_mod(mod)
    path = index._db_path
    index.close()
    return path, mod.mod_id


def test_new_and_existing_current_database(temp_data_dir):
    path, identity = _current()
    reopened = ModIndex("game_a")
    assert not reopened.schema_error
    assert reopened.get_mod(identity).mod_id == identity
    assert reopened._db().execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
    tables = {r[0] for r in reopened._db().execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == CURRENT_TABLES
    reopened.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "PRAGMA user_version=0",
        "PRAGMA user_version=1",
        "PRAGMA user_version=99",
        "DROP TABLE game_deployment_state",
        "DROP TABLE category_import_journal",
        "ALTER TABLE category_import_journal ADD COLUMN unexpected TEXT",
        "ALTER TABLE mods DROP COLUMN imported_at",
        "ALTER TABLE nodes ADD COLUMN legacy_icon TEXT NOT NULL DEFAULT ''",
    ],
)
def test_mismatched_schema_is_refused_without_any_source_write(temp_data_dir, mutation):
    path, identity = _current()
    with sqlite3.connect(path) as raw:
        raw.execute(mutation)
    before = path.read_bytes()
    index = ModIndex("game_a")
    assert index.schema_error
    assert index._conn is None
    index.close()
    assert path.read_bytes() == before


def test_legacy_dual_identity_schema_requires_explicit_conversion(temp_data_dir):
    path, _ = _current()
    path.unlink()
    with sqlite3.connect(path) as raw:
        raw.execute("CREATE TABLE mods(id TEXT PRIMARY KEY,deployment_id TEXT,path TEXT)")
        raw.execute("INSERT INTO mods VALUES('old','durable','original')")
        raw.execute("PRAGMA user_version=1")
    before = path.read_bytes()
    index = ModIndex("game_a")
    assert index.schema_error
    assert path.read_bytes() == before


def test_refused_schema_cannot_write_user_category(temp_data_dir):
    from src.core.mod_manager import ModManager

    path, _ = _current()
    with sqlite3.connect(path) as raw:
        raw.execute("PRAGMA user_version=1")
    before = path.read_bytes()
    manager = ModManager()
    manager.set_game("game_a")
    assert not manager.index_healthy
    with pytest.raises(sqlite3.Error):
        manager.add_character(name="Must not be created", node_type="character")
    assert path.read_bytes() == before
