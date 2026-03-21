from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def library(tmp_path):
    from src.core.library_paths import LibraryPaths
    from src.core.library_schema import create_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_schema(conn)
    conn.executemany(
        "INSERT INTO nodes(id,game_id,directory_name,parent_id) VALUES(?,?,?,?)",
        [
            ("a", "game", "A", None),
            ("b", "game", "B", "a"),
            ("c", "game", "C", None),
            ("other", "other", "Other", None),
        ],
    )
    conn.executemany(
        "INSERT INTO mods(mod_id,game_id,node_id,folder_name,path,imported_at) VALUES(?,?,?,?,?,?)",
        [
            ("one", "game", "b", "Box", "inside/body.7z", "2026-09-01"),
            ("two", "game", "c", "Box", "body.zip", "2026-09-02"),
        ],
    )
    conn.commit()
    paths = LibraryPaths(conn, {"game": tmp_path / "game", "other": tmp_path / "other"})
    yield conn, paths, tmp_path
    conn.close()


def test_move_changes_only_node_and_resolves_descendant_lazily(library):
    conn, paths, root = library
    before = [tuple(r) for r in conn.execute("SELECT * FROM mods ORDER BY mod_id")]
    old_version = paths.version("one")
    assert paths.body_path("one") == root / "game/A/B/Box/inside/body.7z"
    sql = []
    conn.set_trace_callback(sql.append)
    paths.move_node("b", "c", "Renamed")
    assert paths.body_path("one") == root / "game/C/Renamed/Box/inside/body.7z"
    assert [tuple(r) for r in conn.execute("SELECT * FROM mods ORDER BY mod_id")] == before
    assert not any(s.lstrip().upper().startswith("UPDATE MODS") for s in sql)
    assert paths.version("one") != old_version
    assert paths.locate(str(root / "game/C/Renamed/Box/.d3dxmod")) == ("mod", "one")


def test_case_and_relative_equivalents_and_other_parent_same_name(library):
    _, paths, root = library
    assert paths.locate(str(root / "game/a/B/Box/inside/../.d3dxmod")) == ("mod", "one")
    assert paths.locate(str(root / "game/C/bOX/body.zip")) == ("mod", "two")
    assert paths.locate(str(root / "game/A/B")) == ("node", "b")
    assert paths.locate(str(root / "game/../../escape")) is None


def test_root_occupancy_and_unicode_normalization(library):
    conn, paths, root = library
    conn.execute(
        "INSERT INTO mods(mod_id,game_id,folder_name,path,imported_at) VALUES('root','game','ÄBC','body','2026')"
    )
    assert paths.locate(str(root / "game/äbc/body")) == ("mod", "root")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO mods(mod_id,game_id,folder_name,path,imported_at) "
            "VALUES('collision','game','äbc','body','2026')"
        )


@pytest.mark.parametrize("node,parent", [("a", "b"), ("a", "a"), ("b", "other")])
def test_cycles_and_cross_game_parent_changes_rejected(library, node, parent):
    conn, paths, _ = library
    before = [tuple(r) for r in conn.execute("SELECT * FROM nodes")]
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        paths.move_node(node, parent)
    assert [tuple(r) for r in conn.execute("SELECT * FROM nodes")] == before


def test_relocation_only_changes_configured_root(library):
    conn, paths, root = library
    from src.core.library_paths import LibraryPaths

    moved = LibraryPaths(conn, {"game": root / "moved", "other": root / "other"})
    assert moved.body_path("one") == root / "moved/A/B/Box/inside/body.7z"
    assert paths.resolve_image("one", {"scope": "container", "path": ".d3dxmeta/p.png"}) == (
        root / "game/A/B/Box/.d3dxmeta/p.png"
    )
    with pytest.raises(ValueError):
        paths.resolve_image("one", {"scope": "container", "path": "../escape.png"})


def test_locate_sql_work_does_not_grow_with_unrelated_mods(library):
    conn, paths, root = library
    counts = []
    for count in (500, 5000):
        conn.executemany(
            "INSERT OR IGNORE INTO mods(mod_id,game_id,folder_name,path,imported_at) VALUES(?,?,?,?,?)",
            ((f"x{i}", "game", f"x{i}", "body", "2026") for i in range(count)),
        )
        queries = []
        conn.set_trace_callback(queries.append)
        assert paths.locate(str(root / "game/A/B/Box/.d3dxmod")) == ("mod", "one")
        conn.set_trace_callback(None)
        counts.append(len(queries))
    assert counts[0] == counts[1]
    assert counts[1] <= 8
