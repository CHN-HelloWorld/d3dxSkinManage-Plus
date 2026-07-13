from __future__ import annotations

from pathlib import Path

from src.core.mod_index import ModIndex
from tests.helpers import make_mod
from tests.helpers import registered_games as registered_games


def test_content_lookup_uses_nonunique_game_content_index(temp_data_dir):
    index = ModIndex("first")
    try:
        for number in range(3):
            assert index.add_mod(make_mod(index, f"first-{number}"))
        index.game_id = "second"
        assert index.add_mod(make_mod(index, "second"))
        plans = (
            index._db()
            .execute(
                "EXPLAIN QUERY PLAN SELECT * FROM mods WHERE content_hash = ? AND game_id = ? LIMIT 1",
                ("a" * 40, "first"),
            )
            .fetchall()
        )
        assert any("idx_mods_game_content" in row[3] for row in plans)
        indexes = {row[1]: row[2] for row in index._db().execute("PRAGMA index_list(mods)")}
        assert indexes["idx_mods_game_content"] == 0
        assert index.find_by_content_hash("a" * 40, "first").name.startswith("first-")
        assert index.find_by_content_hash("a" * 40, "second").name == "second"
        assert index.find_by_content_hash("f" * 40, "first") is None
        assert index._db().execute("SELECT COUNT(*) FROM mods").fetchone()[0] == 4
    finally:
        index.close()


def test_missing_optional_content_index_does_not_rewrite_rows(temp_data_dir):
    index = ModIndex("first")
    kept = make_mod(index, "keep")
    assert index.add_mod(kept)
    db_path = Path(index._db().execute("PRAGMA database_list").fetchone()[2])
    index._db().execute("DROP INDEX idx_mods_game_content")
    index._db().commit()
    index.close()
    before = db_path.read_bytes()
    index = ModIndex("first")
    assert not index.schema_error
    assert index.get_mod(kept.mod_id) is not None
    assert index._db().execute("SELECT name FROM sqlite_master WHERE name='idx_mods_game_content'").fetchone() is None
    index.close()
    assert db_path.read_bytes() == before
