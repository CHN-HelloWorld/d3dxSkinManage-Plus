from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.mod_identity import (
    IdentityConflict,
    read_deployment_identity,
    read_source_mod_id,
    scan_deployment_identities,
    write_deployment_identity,
)
from src.core.mod_manager import ModManager
from src.core.mod_package import (
    CONTAINER_KIND,
    build_local_manifest,
    is_mod_container,
    read_sidecar,
    write_container_marker,
    write_sidecar,
)
from tests.helpers import registered_games as registered_games

IDENTITY = "11111111111141118111111111111111"
OTHER = "22222222222242228222222222222222"


def source(tmp_path):
    body = tmp_path / "Managed" / "Body"
    body.mkdir(parents=True)
    (body / "mod.ini").write_text("[Constants]\nx=1\n", encoding="utf-8")
    (body.parent / ".d3dxmod").write_text(
        json.dumps(
            {
                "schema": 1,
                "kind": CONTAINER_KIND,
                "body": body.name,
                "body_type": "folder",
                "content_hash": "a" * 40,
                "mod_id": IDENTITY,
                "imported_at": "2026-01-01T00:00:00",
            }
        ),
        encoding="utf-8",
    )
    return body


def test_deployment_uses_root_marker_only(tmp_path):
    write_deployment_identity(tmp_path, IDENTITY, "hash")
    marker = tmp_path / ".d3dxmod"
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "kind": "d3dxSkinManage-Plus.deployment",
        "mod_id": IDENTITY,
        "content_hash": "hash",
    }
    assert not (tmp_path / ".d3dxmeta").exists()


def test_source_identity_survives_missing_or_corrupt_descriptions(tmp_path):
    body = source(tmp_path)
    assert read_source_mod_id(body) == IDENTITY
    meta = body.parent / ".d3dxmeta"
    meta.mkdir()
    (meta / "info.json").write_text("{broken", encoding="utf-8")
    assert read_source_mod_id(body) == IDENTITY


def test_metadata_write_does_not_touch_marker_or_persist_identity(tmp_path):
    body = source(tmp_path)
    marker = body.parent / ".d3dxmod"
    before = marker.read_bytes()
    assert write_sidecar(
        body,
        build_local_manifest(
            {"name": "Renamed", "info": {}},
            {"mod_id": OTHER},
        ),
    )
    assert "mod_id" not in read_sidecar(body).get("local", {})
    assert marker.read_bytes() == before


def test_marker_rewrite_after_body_rename_preserves_identity(tmp_path):
    body = source(tmp_path)
    body.rename(body.with_name("Renamed"))
    assert write_container_marker(body.parent, "Renamed", "b" * 40, imported_at="2026-01-01T00:00:00")
    assert read_source_mod_id(body.with_name("Renamed")) == IDENTITY


def test_marker_rewrite_refuses_identity_replacement(tmp_path):
    body = source(tmp_path)
    before = (body.parent / ".d3dxmod").read_bytes()
    with pytest.raises(IdentityConflict):
        write_container_marker(body.parent, body.name, "a" * 40, mod_id=OTHER, imported_at="2026-01-01T00:00:00")
    assert (body.parent / ".d3dxmod").read_bytes() == before


def test_failed_marker_write_keeps_original_bytes(tmp_path, monkeypatch):
    from src.core import mod_package

    body = source(tmp_path)
    marker = body.parent / ".d3dxmod"
    before = marker.read_bytes()

    def fail_write(path, *_args, **_kwargs):
        Path(path).write_text("{partial", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(mod_package.fs, "write_text", fail_write)
    with pytest.raises(OSError, match="disk full"):
        write_container_marker(body.parent, body.name, "b" * 40, imported_at="2026-01-01T00:00:00")
    assert marker.read_bytes() == before
    assert list(body.parent.glob(".marker-*")) == []


def test_failed_deployment_marker_replace_keeps_original_bytes(tmp_path, monkeypatch):
    from src.core import mod_identity

    write_deployment_identity(tmp_path, IDENTITY, "hash")
    before = (tmp_path / ".d3dxmod").read_bytes()

    def fail_replace(*_args):
        raise PermissionError("marker locked")

    monkeypatch.setattr(mod_identity.os, "replace", fail_replace)
    with pytest.raises(PermissionError):
        write_deployment_identity(tmp_path, OTHER, "updated")
    assert (tmp_path / ".d3dxmod").read_bytes() == before
    assert list(tmp_path.glob(".identity-*")) == []


def test_source_and_deployment_kinds_are_distinct(tmp_path):
    body = source(tmp_path)
    deployment = tmp_path / "Deployment"
    write_deployment_identity(deployment, OTHER, "hash")
    assert not is_mod_container(deployment)
    assert read_deployment_identity(body.parent) is None
    assert set(scan_deployment_identities([tmp_path])) == {OTHER}
    with pytest.raises(IdentityConflict):
        read_source_mod_id(deployment)


def test_old_identity_locations_are_not_read(tmp_path):
    body = source(tmp_path)
    marker = body.parent / ".d3dxmod"
    data = json.loads(marker.read_text(encoding="utf-8"))
    data.pop("mod_id")
    marker.write_text(json.dumps(data), encoding="utf-8")
    meta = body.parent / ".d3dxmeta"
    meta.mkdir()
    legacy = build_local_manifest({"name": "Old", "info": {}})
    legacy["local"] = {"mod_id": IDENTITY}
    (meta / "info.json").write_text(json.dumps(legacy), encoding="utf-8")
    (meta / "deployment_identity.json").write_text(
        json.dumps(
            {
                "kind": "d3dxSkinManage-Plus.deployment",
                "mod_id": IDENTITY,
                "content_hash": "hash",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(IdentityConflict):
        read_source_mod_id(body)
    marker.unlink()
    assert read_deployment_identity(body.parent) is None


@pytest.mark.parametrize("empty_index", [False, True])
def test_rebuild_does_not_rewrite_source_marker(temp_data_dir, tmp_path, empty_index):
    manager = ModManager()
    manager.set_game("game_a")
    char = manager.add_character(name="Character", node_type="character")
    incoming = tmp_path / "Input"
    incoming.mkdir()
    (incoming / "mod.ini").write_text("[Constants]\nx=1\n", encoding="utf-8")
    mod = manager.import_mod(source_path=str(incoming), node_id=char.id)
    assert mod is not None
    marker = Path(mod.path).parent / ".d3dxmod"
    before = marker.read_bytes()
    assert json.loads(before)["mod_id"] == mod.mod_id
    (marker.parent / ".d3dxmeta" / "info.json").unlink()
    if empty_index:
        manager._index.clear_mods_for_game("game_a")
    for _ in range(2):
        assert manager.rebuild_index_from_filesystem("game_a").registered == 1
        assert marker.read_bytes() == before
        assert manager.get_mods()[0].mod_id == mod.mod_id


def test_importing_deployment_imports_payload_with_fresh_identity(temp_data_dir, tmp_path):
    manager = ModManager()
    manager.set_game("game_a")
    char = manager.add_character(name="Character", node_type="character")
    incoming = tmp_path / "Deployed"
    write_deployment_identity(incoming, IDENTITY, "hash")
    (incoming / "mod.ini").write_text("[Constants]\nx=1\n", encoding="utf-8")
    before = (incoming / ".d3dxmod").read_bytes()
    mod = manager.import_mod(source_path=str(incoming), node_id=char.id)
    assert mod is not None
    assert read_source_mod_id(mod.path) == mod.mod_id != IDENTITY
    assert (incoming / ".d3dxmod").read_bytes() == before
