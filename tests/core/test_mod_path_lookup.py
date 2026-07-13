import os
import sqlite3
from pathlib import Path

import pytest

from src.core.mod_index import ModIndex
from tests.helpers import make_mod
from tests.helpers import registered_games as registered_games


@pytest.fixture
def index(temp_data_dir):
    value = ModIndex("first")
    yield value
    value.close()


def test_path_lookup_normalizes_aliases_and_keeps_games_separate(index, monkeypatch):
    first = make_mod(index)
    assert index.add_mod(first)
    index.game_id = "second"
    second = make_mod(index)
    assert index.add_mod(second)
    index.paths.roots["first"].mkdir(parents=True)
    monkeypatch.chdir(index.paths.roots["first"])
    assert index.find_mod_id_by_path("Skin/../Skin/body.zip", "first") == first.mod_id
    assert index.find_mod_id_by_path(second.path, "second") == second.mod_id
    assert index.find_mod_id_by_path(first.path, "second") is None
    if os.name == "nt":
        assert index.find_mod_id_by_path(first.path.upper(), "first") == first.mod_id


def test_same_physical_location_cannot_have_two_identities(index):
    first = make_mod(index)
    assert index.add_mod(first)
    assert not index.add_mod(make_mod(index))
    assert index.find_mod_id_by_path(first.path, "first") == first.mod_id
    assert index.get_mod_ids() == {first.mod_id}


def test_body_reference_changes_atomically_with_update_and_rollback(index):
    mod = make_mod(index)
    assert index.add_mod(mod)
    original = mod.path
    mod.path = str(Path(original).with_name("new.zip"))
    assert index.update_mod(mod)
    moved = mod.path
    assert index.find_mod_id_by_path(original, "first") == mod.mod_id
    with pytest.raises(RuntimeError):
        with index.transaction():
            mod.path = str(Path(original).with_name("failed.zip"))
            assert index.add_mod(mod, commit=False)
            raise RuntimeError("rollback")
    assert index.get_mod(mod.mod_id).path == moved
    assert index.find_mod_id_by_path(mod.path, "first") == mod.mod_id


def test_path_query_does_not_hide_database_failure(index):
    index.close()
    with pytest.raises(sqlite3.Error):
        index.find_mod_id_by_path(str(index.paths.roots["first"] / "Skin" / "body.zip"), "first")


def test_storage_has_only_container_relative_body(index):
    mod = make_mod(index)
    assert index.add_mod(mod)
    row = index._db().execute("SELECT * FROM mods").fetchone()
    assert row["path"] == "body.zip"
    assert "path_key" not in row.keys()
    assert not index._db().execute("SELECT 1 FROM sqlite_master WHERE name='path_key_context'").fetchone()


def test_delete_releases_location_for_new_identity(index):
    original = make_mod(index)
    assert index.add_mod(original)
    assert index.delete_mod(original.mod_id)
    replacement = make_mod(index)
    assert index.add_mod(replacement)
    assert index.find_mod_id_by_path(replacement.path, "first") == replacement.mod_id


def test_reopen_does_not_rewrite_paths(temp_data_dir):
    index = ModIndex("first")
    mod = make_mod(index)
    assert index.add_mod(mod)
    index.close()
    reopened = ModIndex("first")
    assert reopened._db().total_changes == 0
    assert reopened.get_mod(mod.mod_id).path == mod.path
    reopened.close()
