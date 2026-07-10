from __future__ import annotations

import sqlite3
import uuid

import pytest

from src.core import mod_identity
from src.core.mod_identity import (
    IdentityConflict,
    new_mod_id,
    read_deployment_identity,
    scan_deployment_identities,
    validate_mod_id,
    write_deployment_identity,
)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "shared",
        pytest.param(uuid.uuid4().hex.upper(), id="uppercase_uuid4"),
        pytest.param(str(uuid.uuid4()), id="hyphenated_uuid4"),
        pytest.param(uuid.uuid1().hex, id="uuid1"),
        None,
    ],
)
def test_only_canonical_uuid4_is_a_persisted_identity(value):
    with pytest.raises(IdentityConflict):
        validate_mod_id(value)


def test_collision_retries_until_unoccupied_uuid(monkeypatch):
    occupied, available = uuid.uuid4(), uuid.uuid4()
    sequence = iter([occupied, occupied, available])
    monkeypatch.setattr(mod_identity.uuid, "uuid4", lambda: next(sequence))
    assert new_mod_id({occupied.hex}) == available.hex


def test_scan_ignores_unpublished_copies_and_unmanaged_folders(tmp_path):
    identity = uuid.uuid4().hex
    deployed = tmp_path / "Renamed"
    write_deployment_identity(deployed, identity, "hash")
    for name in ("DISABLED_.update_job.partial", "DISABLED_mod.partial", ".hidden"):
        write_deployment_identity(tmp_path / name, identity, "hash")
    (tmp_path / "Unmanaged").mkdir()
    locations = scan_deployment_identities([tmp_path, tmp_path])
    assert list(locations) == [identity]
    assert locations[identity].path == deployed
    assert locations[identity].active


def test_bad_marker_is_not_treated_as_absent(tmp_path):
    write_deployment_identity(tmp_path, uuid.uuid4().hex)
    (tmp_path / ".d3dxmod").write_text("{")
    with pytest.raises(IdentityConflict):
        read_deployment_identity(tmp_path)


def test_database_rejects_identity_shared_by_distinct_mods(temp_data_dir, tmp_path):
    from src.core.mod_index import ModIndex
    from tests.helpers import make_mod, register_games

    register_games("game")
    index = ModIndex("game")
    identity = uuid.uuid4().hex
    first = make_mod(index, "One", mod_id=identity, content_hash="a" * 40)
    assert index.add_mod(first)
    with pytest.raises(sqlite3.IntegrityError):
        index._db().execute(
            "INSERT INTO mods(mod_id,game_id,folder_name,path,imported_at) VALUES(?,?,?,?,?)",
            (identity, "game", "Two", "body.zip", first.imported_at),
        )
    index._db().rollback()
    assert index.get_mod(identity).mod_id == first.mod_id
    assert index.get_mod_ids() == {identity}
    index.close()
