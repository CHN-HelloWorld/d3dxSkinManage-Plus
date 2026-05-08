from __future__ import annotations

import json
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core import mod_identity
from src.core.mod_identity import IdentityConflict, read_source_mod_id
from src.core.mod_intake import ModIntakeService
from src.core.mod_manager import ModManager
from src.core.mod_package import META_DIR, build_local_manifest, write_container_marker
from src.models.mod import Mod
from tests.helpers import registered_games as registered_games

FIRST = "11111111111141118111111111111111"
SECOND = "22222222222242228222222222222222"
THIRD = "33333333333343338333333333333333"
MISSING = object()


@pytest.fixture
def manager(temp_data_dir):
    result = ModManager()
    result.set_game("game_a")
    return result


def _source(parent: Path, payload: str = "one") -> Path:
    source = parent / "Same Skin"
    source.mkdir(parents=True)
    (source / "mod.ini").write_text(f"[Constants]\nglobal $skin = {payload}\n", encoding="utf-8")
    return source


def _managed(parent: Path, identity=FIRST, *, payload: str = "one") -> Path:
    from src.utils.paths import get_data_dir

    container = get_data_dir() / "game_a" / "mods" / (parent.name + " Managed Skin")
    body = _source(container, payload)
    assert write_container_marker(container, body.name, Mod.compute_sha1(body), imported_at="2026-10-05T10:00:00")
    local = {"classification": ["Original"], "status_hint": "disabled"}
    marker = container / ".d3dxmod"
    data = json.loads(marker.read_text(encoding="utf-8"))
    data.pop("mod_id")
    if identity is not MISSING:
        data["mod_id"] = identity
    marker.write_text(json.dumps(data), encoding="utf-8")
    meta = container / META_DIR
    meta.mkdir()
    (meta / "info.json").write_text(
        json.dumps(build_local_manifest({"name": "Original name", "info": {"author": "Author"}}, local)),
        encoding="utf-8",
    )
    return body


def _request(service, manager, source, *, character=None, adopt=False):
    character = None if adopt else character or manager.add_character(name="Epsilon", node_type="character")
    return service.plan_request(
        source_path=source,
        game_id="game_a",
        node_id=character.id if character else "",
        target_format="zip",
        context="rediscovery" if adopt else "external",
        planning_snapshot=service.planning_snapshot("game_a"),
    )


def _uuid_sequence(monkeypatch, *identities):
    values = iter(identities)
    monkeypatch.setattr(
        mod_identity,
        "uuid",
        SimpleNamespace(UUID=uuid.UUID, uuid4=lambda: uuid.UUID(next(values))),
    )


@pytest.mark.parametrize("source_kind", ["folder", "zip", "container"])
@pytest.mark.parametrize("incoming_identity", [FIRST, "invalid-external-identity"])
def test_user_import_discards_external_identity_and_persists_fresh_one(
    manager,
    tmp_path,
    source_kind,
    incoming_identity,
):
    body = _managed(tmp_path / "incoming", incoming_identity)
    if source_kind == "container":
        source = body.parent
    else:
        source = _source(tmp_path / "incoming-body")
        meta = source / META_DIR
        meta.mkdir()
        (meta / "info.json").write_bytes((body.parent / META_DIR / "info.json").read_bytes())
        if source_kind == "zip":
            archive = tmp_path / "incoming.zip"
            with zipfile.ZipFile(archive, "w") as output:
                for path in source.rglob("*"):
                    if path.is_file():
                        output.write(path, path.relative_to(source.parent))
            source = archive
    original_meta = (body.parent / META_DIR / "info.json").read_bytes()
    service = ModIntakeService(manager)
    prepared = service.prepare(_request(service, manager, source))
    try:
        identity = prepared.mod.mod_id
        assert uuid.UUID(identity).version == 4
        assert uuid.UUID(identity).hex == identity
        assert identity != incoming_identity
        assert read_source_mod_id(prepared.body_path) == identity
        mod = service.commit(prepared)
        assert manager.get_mod(mod.mod_id).mod_id == identity
        assert (body.parent / META_DIR / "info.json").read_bytes() == original_meta
    finally:
        service.rollback(prepared)


def test_parallel_same_name_imports_retry_global_and_inflight_collisions(
    manager,
    tmp_path,
    monkeypatch,
):
    known = Mod(
        name="Same Skin",
        folder_name="Same Skin",
        path=str(manager._index.paths.roots["other"] / "Same Skin" / "body.zip"),
        game_id="other",
        content_hash="a" * 40,
        mod_id=FIRST,
    )
    manager._index.game_id = "other"
    assert manager._index.add_mod(known)
    manager._index.game_id = "game_a"
    first_service, second_service = ModIntakeService(manager), ModIntakeService(manager)
    first_char = manager.add_character(name="First", node_type="character")
    second_char = manager.add_character(name="Second", node_type="character")
    requests = [
        _request(first_service, manager, _source(tmp_path / "first", "first"), character=first_char),
        _request(second_service, manager, _source(tmp_path / "second", "second"), character=second_char),
    ]
    _uuid_sequence(monkeypatch, FIRST, SECOND, SECOND, THIRD)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(service.prepare, request) for service, request in zip((first_service, second_service), requests)
        ]
        prepared = [future.result(timeout=15) for future in futures]
    try:
        assert {item.mod.mod_id for item in prepared} == {SECOND, THIRD}
        for service, item in zip((first_service, second_service), prepared):
            service.commit(item)
        assert manager._index.get_mod_ids() == {FIRST, SECOND, THIRD}
    finally:
        for service, item in zip((first_service, second_service), prepared):
            service.rollback(item)


def test_older_plan_retries_identity_committed_after_planning(manager, tmp_path, monkeypatch):
    service, other_service = ModIntakeService(manager), ModIntakeService(manager)
    first_request = _request(service, manager, _source(tmp_path / "first", "first"))
    second_request = _request(other_service, manager, _source(tmp_path / "second", "second"))
    _uuid_sequence(monkeypatch, FIRST, FIRST, SECOND)
    first = service.prepare(first_request)
    service.commit(first)
    second = other_service.prepare(second_request)
    try:
        assert first.mod.mod_id == FIRST
        assert second.mod.mod_id == SECOND
    finally:
        other_service.rollback(second)


def test_planned_identity_allocation_never_reads_sqlite_in_worker(manager, tmp_path, monkeypatch):
    service = ModIntakeService(manager)
    request = _request(service, manager, _source(tmp_path))

    def forbidden():
        raise AssertionError("worker read the main-thread SQLite connection")

    monkeypatch.setattr(manager._index, "_db", forbidden)
    with ThreadPoolExecutor(max_workers=1) as pool:
        prepared = pool.submit(service.prepare, request).result(timeout=15)
    try:
        assert read_source_mod_id(prepared.body_path) == prepared.mod.mod_id
    finally:
        service.rollback(prepared)


def test_managed_adoption_preserves_identity_and_rollback_releases_reservation(manager, tmp_path):
    body = _managed(tmp_path)
    metadata = body.parent / META_DIR / "info.json"
    original = metadata.read_bytes()
    service = ModIntakeService(manager)
    request = _request(service, manager, body, adopt=True)
    first = service.prepare(request)
    service.rollback(first)
    second = ModIntakeService(manager).prepare(request)
    try:
        assert first.mod.mod_id == second.mod.mod_id == FIRST
        assert second.mod.info.author == "Author"
        assert metadata.read_bytes() == original
        service.commit(second)
        assert manager.get_mod(second.mod.mod_id).mod_id == FIRST
    finally:
        service.rollback(second)


@pytest.mark.parametrize(
    "identity",
    [
        MISSING,
        None,
        "",
        "not-an-identity",
        "ABCDEFABCDEF4DEF8DEFABCDEFABCDEF",
        "11111111-1111-4111-8111-111111111111",
        "11111111111111118111111111111111",
    ],
)
def test_managed_adoption_repairs_missing_or_invalid_identity(manager, tmp_path, identity):
    body = _managed(tmp_path, identity)
    metadata = body.parent / META_DIR / "info.json"
    original = metadata.read_bytes()
    service = ModIntakeService(manager)
    prepared = service.prepare(_request(service, manager, body, adopt=True))
    assert uuid.UUID(prepared.mod.mod_id).version == 4
    assert prepared.mod.mod_id != identity
    service.rollback(prepared)
    assert metadata.read_bytes() == original
    assert manager.get_mods() == []


@pytest.mark.parametrize("metadata_state", ["missing", "unreadable"])
def test_managed_adoption_does_not_require_descriptive_metadata(manager, tmp_path, metadata_state):
    body = _managed(tmp_path)
    metadata = body.parent / META_DIR / "info.json"
    if metadata_state == "missing":
        metadata.unlink()
    else:
        metadata.write_text("{broken", encoding="utf-8")
    service = ModIntakeService(manager)
    prepared = service.prepare(_request(service, manager, body, adopt=True))
    try:
        assert prepared.mod.mod_id == FIRST
        assert read_source_mod_id(body) == FIRST
        assert metadata.exists() == (metadata_state == "unreadable")
    finally:
        service.rollback(prepared)


@pytest.mark.parametrize("owner_state", ["indexed", "inflight"])
def test_managed_adoption_rejects_identity_owned_by_another_mod(manager, tmp_path, owner_state):
    first_body = _managed(tmp_path / "first", payload="first")
    second_body = _managed(tmp_path / "second", payload="second")
    original = (second_body.parent / META_DIR / "info.json").read_bytes()
    service = ModIntakeService(manager)
    first = service.prepare(_request(service, manager, first_body, adopt=True))
    if owner_state == "indexed":
        service.commit(first)
    try:
        with pytest.raises(IdentityConflict, match="mod_id|身份"):
            other = ModIntakeService(manager)
            other.prepare(_request(other, manager, second_body, adopt=True))
        assert (second_body.parent / META_DIR / "info.json").read_bytes() == original
    finally:
        service.rollback(first)


def test_adoption_failure_releases_identity_for_retry(manager, tmp_path, monkeypatch):
    body = _managed(tmp_path)
    service = ModIntakeService(manager)
    request = _request(service, manager, body, adopt=True)
    find_previews = manager._find_previews

    def fail(*args, **kwargs):
        raise OSError("preview read failed")

    monkeypatch.setattr(manager, "_find_previews", fail)
    with pytest.raises(OSError, match="preview"):
        service.prepare(request)
    monkeypatch.setattr(manager, "_find_previews", find_previews)
    retry = service.prepare(request)
    try:
        assert retry.mod.mod_id == FIRST
    finally:
        service.rollback(retry)


def test_commit_rejects_changed_source_identity(manager, tmp_path):
    service = ModIntakeService(manager)
    prepared = service.prepare(_request(service, manager, _source(tmp_path)))
    metadata = prepared.container_path / ".d3dxmod"
    manifest = json.loads(metadata.read_text(encoding="utf-8"))
    manifest["mod_id"] = SECOND if prepared.mod.mod_id == FIRST else FIRST
    metadata.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(IdentityConflict, match="mod_id|身份"):
        service.commit(prepared)
    assert manager.get_mods() == []
    assert not prepared.container_path.exists()
